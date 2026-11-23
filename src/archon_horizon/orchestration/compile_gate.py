"""Coordinate foreground Lean compilations across supervised runs in one workspace."""
from __future__ import annotations

from collections.abc import Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import sys
from threading import Event, Thread
import time
from uuid import uuid4

import psutil

from archon_horizon.core.atomic import write_text_atomic
from archon_horizon.core.locks import workspace_lock
from .resources import process_usage, sample_resources

_scope: ContextVar[dict[str, str] | None] = ContextVar("compile_environment", default=None)
_ENV_ROOT = "HORIZON_COMPILE_ROOT"
_ENV_DIR = "HORIZON_COMPILE_DIR"
_ENV_LEASE = "HORIZON_COMPILE_LEASE"


def _tool_path(path: str) -> str:
    """Strip Horizon shims from every workspace, including symlinked PATH entries."""
    return os.pathsep.join(p for p in path.split(os.pathsep) if not (Path(p) / ".horizon-compile-bin").is_file())


def activate_compile_path(directory: str, path: str) -> str:
    return directory + os.pathsep + _tool_path(path)


def compile_environment(root: Path, state: Path, path: str | None = None) -> dict[str, str]:
    """Paths are absolute so a login shell can invoke HORIZON_LAKE or HORIZON_LEAN directly."""
    directory = state.resolve() / "compiles"
    interpreter = hashlib.sha256(os.fsencode(sys.executable)).hexdigest()[:12]
    bin_dir = directory / "bin" / interpreter
    bin_dir.mkdir(parents=True, exist_ok=True)
    marker = bin_dir / ".horizon-compile-bin"
    if not marker.exists():
        marker.touch()
    for name in ("lean", "lake", "lake-env-command"):
        program = bin_dir / name
        if name == "lake-env-command":
            script = '#!/bin/sh\nexport PATH="${HORIZON_LAKE%/*}:$PATH"\nexec "$@"\n'
        else:
            script = (f'#!/bin/sh\nexec {shlex.quote(sys.executable)} -m '
                      f'archon_horizon.orchestration.compile_gate {name} "$@"\n')
        if program.is_file() and program.read_text() == script and os.access(program, os.X_OK):
            continue
        temp = bin_dir / uuid4().hex
        try:
            temp.write_text(script)
            temp.chmod(0o755)
            os.replace(temp, program)
        finally:
            temp.unlink(missing_ok=True)
    return {_ENV_ROOT: str(root.resolve()), _ENV_DIR: str(directory),
            "HORIZON_LAKE": str(bin_dir / "lake"), "HORIZON_LEAN": str(bin_dir / "lean"),
            "PATH": activate_compile_path(str(bin_dir), path if path is not None else os.environ.get("PATH", os.defpath))}


@contextmanager
def compile_environment_scope(root: Path, state: Path):
    """Use the workspace gate for controller-driven checks without editing the controller's PATH."""
    token = _scope.set(compile_environment(root, state))
    try:
        yield
    finally:
        _scope.reset(token)


def _lake_command(args: list[str]) -> int:
    with_value = {"-d", "-f", "-K", "--dir", "--file", "--packages", "--log-level", "--fail-level"}
    i = 0
    while i < len(args) and args[i].startswith(("-", "+")):
        if args[i] in ("--version", "--help", "-h"):
            return len(args)
        i += 2 if args[i] in with_value else 1
    return i


def _lean_compiles(args: list[str]) -> bool:
    # --run marks the boundary between Lean flags and the program's own arguments.
    flags = args[:args.index("--run")] if "--run" in args else args
    return bool(args) and not set(flags).intersection({
        "--server", "--worker", "--version", "-v", "-V", "--short-version", "-h", "--help",
        "--features", "-g", "--githash", "--print-prefix", "--print-libdir",
    })


def _command_kind(tool: str, args: list[str]) -> str | None:
    if tool == "lean":
        return "lean" if _lean_compiles(args) else None
    i = _lake_command(args)
    if i >= len(args):
        return None
    command = args[i]
    if command == "env" and i + 1 < len(args):
        nested = Path(args[i + 1]).name
        return _command_kind(nested, args[i+2:]) if nested in ("lake", "lean") else None
    if command == "lean":
        return "lean"
    if command in ("build", "query", "test", "lint", "check", "exe", "samply", "shake", "run"):
        return command
    if command == "script" and args[i+1:i+2] == ["run"]:
        return "run"
    return None


@dataclass
class Demand:
    memory: float
    cores: float


def _records(directory: Path, suffix: str) -> dict[Path, dict]:
    """Called under the queue lock. Reap files abandoned by a killed owner."""
    import fcntl

    records = {}
    for path in directory.glob("*." + suffix):
        with path.open("r") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                row = json.load(handle)
                if suffix == "active":
                    try:
                        row["pid"] = int(path.with_suffix(".pid").read_text())
                    except FileNotFoundError:
                        row["pid"] = None
                records[path] = row
            else:
                path.unlink()
                if suffix == "active":
                    path.with_suffix(".pid").unlink(missing_ok=True)
                else:
                    path.with_suffix(".bypassed").unlink(missing_ok=True)
    return records


def _history(path: Path) -> list[dict]:
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return []


def _summary(directory: Path) -> dict:
    try:
        return json.loads((directory / "summary.json").read_text())
    except FileNotFoundError:
        return {"finished": 0, "peak_tree_rss_gib": 0., "recent_peak_tree_rss_gib": 0.,
                "peak_cpu_cores": 0., "total_wait_seconds": 0.}


def compile_status(state: Path) -> dict:
    directory = state / "compiles"
    if not directory.is_dir():
        return {"active": 0, "waiting": 0, **_summary(directory)}
    with workspace_lock(directory, "queue"):
        summary = _summary(directory)
        summary.pop("recent", None)
        return {"active": len(_records(directory, "active")), "waiting": len(_records(directory, "waiting")),
                **summary}


def _headroom(directory, active):
    from .worker_cgroup import envelope_status
    group = envelope_status()
    delegated = Path(group["path"]) if group.get("mode") == "cgroup-v2" else None
    # Successive serial checks can finish within a CPU sampling interval. Reuse CPU for up to
    # one second in the exact same affinity/cgroup view. Always measure present memory.
    cache_path = directory / "cpu.json"
    affinity = sorted(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else None
    cgroup_file = Path("/proc/self/cgroup")
    key = [str(delegated), affinity, cgroup_file.read_text() if cgroup_file.exists() else None]
    started = time.monotonic()
    try:
        cache = json.loads(cache_path.read_text())
    except FileNotFoundError:
        cache = {}
    if (not active and cache.get("key") == key and 0. <= started - cache["at"] < 1.):
        resources = sample_resources(exclude_cgroup=delegated, cached_cpu=tuple(cache["cpu"]))
        group["busy_cores"] = cache.get("group_cores", 0.)
        return resources, group, {}
    before = {row["pid"]: process_usage(row["pid"]) for row in active.values() if row["pid"]}
    resources = sample_resources(exclude_cgroup=delegated)
    elapsed = max(.001, time.monotonic() - started)
    demand = {}
    for pid, last in before.items():
        now = process_usage(pid)
        if now:
            cores = max(0., (now.cpu_seconds - last.cpu_seconds) / elapsed) if last else 0.
            demand[pid] = Demand(now.rss_gib, cores)
    if delegated:
        current = envelope_status()
        if current.get("path") == group["path"]:
            current["busy_cores"] = max(0., (current.get("cpu_usage_usec", 0) -
                                             group.get("cpu_usage_usec", 0)) / (1e6 * elapsed))
            group = current
    write_text_atomic(cache_path, json.dumps({"at": time.monotonic(), "key": key,
                                             "cpu": [resources.cpu_cores, resources.cpu_percent],
                                             "group_cores": group.get("busy_cores", 0.)}))
    return resources, group, demand


def _caller_floor(group: dict) -> float:
    """A Linux lower bound from the blocked caller chain; exclude shared and reclaimable pages."""
    if not sys.platform.startswith("linux"):
        return 0.
    try:
        caller = psutil.Process()
        pids = [caller.pid]
        worker = os.getenv("HORIZON_COMPILE_WORKER_PID", "")
        parents = [p.pid for p in caller.parents()]
        if worker.isdigit() and int(worker) in parents:
            pids += parents[:parents.index(int(worker)) + 1]
        swap = psutil.swap_memory().total > 0
        if group.get("mode") == "cgroup-v2":
            cg = Path(group["path"])
            members = {int(p) for p in (cg / "cgroup.procs").read_text().split()}
            pids = [p for p in pids if p in members]
            swap = swap and (cg / "memory.swap.max").read_text().strip() != "0"
        floor_kib = 0
        for pid in pids:
            lines = Path(f"/proc/{pid}/smaps_rollup").read_text().splitlines()[1:]
            kb = {key.rstrip(":"): int(value) for key, value, unit in (s.split() for s in lines) if unit == "kB"}
            shared = kb.get("Shared_Clean", 0) + kb.get("Shared_Dirty", 0)
            minimum = kb.get("Locked", 0)
            if not swap:
                minimum = max(minimum, kb.get("Anonymous", 0) - kb.get("LazyFree", 0))
            floor_kib += max(0, minimum - shared)
        return floor_kib / 1024**2
    except (OSError, ValueError, psutil.Error):
        return 0.


def _allowed(config, resources, group, hint: Demand, active: dict[Path, dict], measured) -> bool:
    total = resources.memory_capacity(config, group)
    available = resources.memory_headroom(config, group)
    cpu_limit = resources.cpu_cores * config.max_cpu_percent / 100
    idle_budget = resources.cpu_cores * (config.max_cpu_percent - resources.cpu_percent) / 100
    if group.get("mode") == "cgroup-v2":
        cpu_limit = min(cpu_limit, group["cpu_quota_cores"])
        idle_budget = min(idle_budget, group["cpu_quota_cores"] - group.get("busy_cores", 0.))
    if total <= 0 or hint.memory > total:
        raise OSError(f"Compile needs an estimated {hint.memory:.2f} GiB, "
                      f"but the memory budget after the reserve is {total:.2f} GiB")
    if config.max_parallel_compiles and len(active) >= config.max_parallel_compiles:
        return False
    if resources.cpu_percent >= config.max_cpu_percent:
        return False
    for row in active.values():
        usage = measured.get(row["pid"], Demand(0., 0.))
        available -= max(0., row["memory"] - usage.memory)
        idle_budget -= max(0., row["cores"] - usage.cores)
    needed = min(hint.cores, cpu_limit) if active else min(.25, hint.cores, cpu_limit / 2)
    return available >= hint.memory and idle_budget >= needed


def _falling_memory(path: Path, free: float, cold: float, period: float) -> bool:
    now = time.monotonic()
    try:
        previous_time, previous_free = json.loads(path.read_text())
    except FileNotFoundError:
        previous_time, previous_free = now, free
    write_text_atomic(path, json.dumps((now, free)))
    elapsed = now - previous_time
    return (0. < elapsed < max(1., 2 * period) and
            previous_free - free > max(.0625, cold / 4) * max(1., elapsed))


class _Lease:
    def __init__(self, directory: Path, key: str):
        self.directory = directory
        self.history = directory / (hashlib.sha256(key.encode()).hexdigest() + ".samples")
        self.wait_path = directory / f"{time.time_ns():020d}-{uuid4().hex}.waiting"
        self.path = self.wait_path
        self.handle = None
        self.memory = 0.
        self.cores = 0.
        self.samples = 0
        self.requested_at = time.monotonic()
        self.wait_seconds = 0.
        self.done = Event()
        self.monitor = None

    def _write(self, row: dict) -> None:
        self.handle.seek(0)
        json.dump(row, self.handle)
        self.handle.truncate()
        self.handle.flush()

    def acquire(self, config) -> None:
        import fcntl

        with workspace_lock(self.directory, "queue"):
            self.handle = self.path.open("x+")
            fcntl.flock(self.handle, fcntl.LOCK_EX)
            self._write({"pid": os.getpid()})
        next_resident_check = 0.
        while True:
            with workspace_lock(self.directory, "queue"):
                waiting = _records(self.directory, "waiting")
                active = _records(self.directory, "active")
                first = min(waiting)
                # If the oldest cannot fit even with no current build, a smaller proof may still
                # complete, letting its worker release memory. Give the oldest first try again.
                bypass = first.with_suffix(".bypassed")
                turn = self.path == first or (not active and waiting[first].get("blocked") and not bypass.exists())
                if turn and (not config.max_parallel_compiles or len(active) < config.max_parallel_compiles):
                    resources, group, measured = _headroom(self.directory, active)
                    history = _history(self.history)
                    cold = resources.worker_memory_for(group, config)
                    # A launch-only sample misses almost all of a short build. Keep its cold budget.
                    useful = [r for r in history if r["samples"] >= 2]
                    hint = Demand(max(.125, 1.25 * max(r["memory"] for r in useful)) if useful else cold,
                                  max(.25, max(r["cores"] for r in useful)) if useful else min(2., resources.cpu_cores))
                    falling = _falling_memory(self.directory / "memory.json", resources.memory_headroom(config, group),
                                              cold, config.poll_seconds)
                    if _allowed(config, resources, group, hint, active, measured) and not falling:
                        self._write({"memory": hint.memory, "cores": hint.cores})
                        self.path = self.wait_path.rename(self.wait_path.with_suffix(".active"))
                        if self.wait_path == first:
                            bypass.unlink(missing_ok=True)
                        else:
                            bypass.touch()
                        self.wait_seconds = time.monotonic() - self.requested_at
                        return
                    if self.path == first and not waiting[first].get("blocked"):
                        self._write({"pid": os.getpid(), "blocked": True})
                    if (not active and resources.memory_headroom(config, group) < hint.memory
                            and time.monotonic() >= next_resident_check):
                        next_resident_check = time.monotonic() + max(5., config.poll_seconds)
                        if _caller_floor(group) + hint.memory > resources.memory_capacity(config, group):
                            raise OSError("The waiting worker's private resident memory and expected compiler "
                                          "peak exceed the budget even with all other builds idle")
            # More frequent than the supervisor loop, allowing short Lean commands to hand over a slot.
            time.sleep(min(.25, config.poll_seconds))

    def observe(self, pid: int) -> None:
        with workspace_lock(self.directory, "queue"):
            # Keep the lock inode immutable once passed to a child. Killing its launcher during
            # a metadata update must leave a readable active lease for all other workers.
            write_text_atomic(self.path.with_suffix(".pid"), str(pid))

        def sample():
            previous = None
            while True:
                usage = process_usage(pid)
                now = time.monotonic()
                if usage is None:
                    break
                self.memory = max(self.memory, usage.rss_gib)
                if previous:
                    delta = (usage.cpu_seconds - previous[1]) / (now - previous[0])
                    self.cores = max(self.cores, delta)
                self.samples += 1
                previous = (now, usage.cpu_seconds)
                if self.done.wait(.25):
                    break

        self.monitor = Thread(target=sample, daemon=True)
        self.monitor.start()

    def close(self) -> None:
        self.done.set()
        if self.monitor:
            self.monitor.join()
        if self.handle:
            with workspace_lock(self.directory, "queue"):
                try:
                    if self.samples:
                        history = _history(self.history)
                        history.append({"memory": self.memory, "cores": self.cores, "samples": self.samples})
                        write_text_atomic(self.history, json.dumps(history[-12:]))
                    if self.monitor:
                        summary = _summary(self.directory)
                        summary["finished"] += 1
                        summary["peak_tree_rss_gib"] = max(self.memory, summary["peak_tree_rss_gib"])
                        summary["peak_cpu_cores"] = max(self.cores, summary["peak_cpu_cores"])
                        if self.memory > 0:
                            recent = [*summary.get("recent", []), self.memory][-8:]
                            summary.update(recent=recent, recent_peak_tree_rss_gib=max(recent))
                        summary["total_wait_seconds"] += self.wait_seconds
                        write_text_atomic(self.directory / "summary.json", json.dumps(summary))
                finally:
                    self.handle.close()
                # A child may still own the lock if its launcher was interrupted.
                _records(self.directory, self.path.suffix[1:])


@dataclass
class Launch:
    argv: list[str]
    env: dict[str, str] | None = None
    pass_fds: tuple[int, ...] = ()
    lease: _Lease | None = None

    def observe(self, pid: int) -> None:
        if self.lease:
            self.lease.observe(pid)


@contextmanager
def admitted_command(command: Sequence[str], *, cwd: Path | None = None):
    """Admit before starting any compile timer. Nested Lake invocations share the outer lease."""
    overlay = _scope.get()
    env = {**os.environ, **(overlay or {})}
    tool = Path(command[0]).name
    launch = Launch(list(command), env if overlay else None)
    if not env.get(_ENV_DIR) or tool not in ("lean", "lake"):
        yield launch
        return
    directory = Path(env[_ENV_DIR])
    shim = Path(env["HORIZON_LAKE"]).parent
    path = _tool_path(env.get("PATH", os.defpath))
    if command[0] == tool or (Path(command[0]).absolute().parent / ".horizon-compile-bin").is_file():
        actual = shutil.which(tool, path=path)
        if not actual:
            raise FileNotFoundError(f"{tool} not found outside the Horizon compile wrappers")
        launch.argv[0] = actual
    args = list(command[1:])
    kind = _command_kind(tool, args)
    if tool == "lake":
        i = _lake_command(args)
        if (not kind and args[i:i+1] == ["env"] and i + 1 < len(args) and
                Path(args[i + 1]).name != "lean"):
            # Lake prepends the toolchain bin directory. Restore lookup of the wrappers *after*
            # entering the project environment, including for 'lake env sh -c "lake build"'.
            launch.argv.insert(i + 2, str(shim / "lake-env-command"))
    launch.env = env
    nested = env.get(_ENV_LEASE)
    if kind is None:
        yield launch
        return
    if nested and Path(nested).parent == directory:
        # A detached descendant can outlive its outer compiler. A stale path must reacquire.
        with workspace_lock(directory, "queue"):
            still_nested = Path(nested) in _records(directory, "active")
        if still_nested:
            yield launch
            return
    from archon_horizon.config.loader import load_config

    config = load_config(Path(env[_ENV_ROOT])).scheduler
    project = str((cwd or Path.cwd()).resolve())
    lease = _Lease(directory, project + "\0" + kind)
    try:
        lease.acquire(config)
        launch.env = {**env, _ENV_LEASE: str(lease.path)}
        launch.pass_fds = (lease.handle.fileno(),)
        launch.lease = lease
        yield launch
    finally:
        lease.close()


def main(args: list[str]) -> int:
    tool, *argv = args
    try:
        with admitted_command([tool, *argv]) as launch:
            if not launch.lease:
                os.execvpe(launch.argv[0], launch.argv, launch.env or os.environ)
            proc = subprocess.Popen(launch.argv, env=launch.env, pass_fds=launch.pass_fds)
            launch.observe(proc.pid)
            for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
                signal.signal(sig, lambda received, _frame: proc.send_signal(received))
            code = proc.wait()
            return code if code >= 0 else 128 - code
    except FileNotFoundError as exc:
        print(f"Horizon: {exc}", file=sys.stderr)
        return 127
    except OSError as exc:
        print(f"Horizon: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
