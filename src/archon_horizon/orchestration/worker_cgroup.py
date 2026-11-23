"""Aggregate kernel limits for the processes a supervisor starts in a delegated cgroup."""

from __future__ import annotations

import errno
import hashlib
import os
import subprocess
import sys
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from archon_horizon.config.schema import ConfigError, SchedulerConfig
from archon_horizon.log import log

from .cgroup_memory import reclaimable_file_bytes
from .resources import Resources, sample_resources

_MOUNT = Path("/sys/fs/cgroup")
_GIB = 1024**3
_ENV = "ARCHON_HORIZON_WORKER_CGROUP"
_active: ContextVar[Path | None] = ContextVar("horizon_worker_cgroup", default=None)
_preview: ContextVar[dict | None] = ContextVar("horizon_planned_cgroup", default=None)


def _delegated_parent(raw: str) -> Path:
    parent = Path(raw)
    if sys.platform != "linux" or not parent.is_absolute():
        raise ConfigError("scheduler.cgroup_parent requires an absolute delegated Linux cgroup v2 path")
    parent = parent.resolve(strict=True)
    if not parent.is_relative_to(_MOUNT) or parent.samefile(_MOUNT):
        raise ConfigError("scheduler.cgroup_parent must be a delegated directory below /sys/fs/cgroup")
    # Reject ordinary filesystems, including a user-created lookalike directory.
    dev = parent.stat().st_dev
    device = f"{os.major(dev)}:{os.minor(dev)}"
    if not any(line.split()[2] == device and " - cgroup2 " in line
               for line in Path("/proc/self/mountinfo").read_text().splitlines()):
        raise ConfigError(f"{parent} is not on a cgroup v2 filesystem")
    controllers = set((parent / "cgroup.subtree_control").read_text().split())
    if not {"cpu", "memory"} <= controllers or (parent / "cgroup.procs").read_text().strip():
        raise ConfigError(f"{parent} needs an unpopulated parent with cpu and memory enabled in cgroup.subtree_control")
    return parent


def _domains(parent: Path):
    for path in (parent, *parent.parents):
        if path == _MOUNT:
            break
        yield path


def _ancestor_memory(parent: Path, *, reclaim_file_cache: bool = False) -> tuple[int | None, int | None, int | None]:
    """Available bytes under hard/soft bounds and the effective ancestor hard limit."""
    hard_free, soft_free, totals = [], [], []
    for path in _domains(parent):
        maximum = path / "memory.max"
        if maximum.exists():
            cap = maximum.read_text().strip()
            used = int((path / "memory.current").read_text())
            if reclaim_file_cache:
                used = max(0, used - reclaimable_file_bytes(path))
            if cap != "max":
                totals.append(int(cap))
                hard_free.append(int(cap) - used)
            high_cap = (path / "memory.high").read_text().strip()
            if high_cap != "max":
                soft_free.append(int(high_cap) - used)
    return min(hard_free, default=None), min(soft_free, default=None), min(totals, default=None)


def _limits(parent: Path, resources: Resources, config: SchedulerConfig) -> tuple[int, int, int]:
    cores = resources.cpu_cores
    # The manager and the delegated parent can have different ancestor limits.
    for path in _domains(parent):
        cpu = path / "cpu.max"
        if cpu.exists():
            quota, period = cpu.read_text().split()
            if quota != "max":
                cores = min(cores, int(quota) / int(period))
    hard_free, soft_free, total = _ancestor_memory(parent)
    available = int(resources.available_gib * _GIB)
    available = min(available, hard_free) if hard_free is not None else available
    host_total = int((resources.total_gib or resources.available_gib) * _GIB)
    total = min(host_total, total) if total is not None else host_total
    reserve = config.min_available_memory_gib
    reserve_bytes = int(reserve * _GIB) if reserve is not None else min(2 * _GIB, total // 5)
    page = os.sysconf("SC_PAGE_SIZE")
    hard = (available - reserve_bytes) // page * page
    high_limit = hard * 9 // 10
    high = min(high_limit, soft_free if soft_free is not None else high_limit) // page * page
    quota_us = int(100_000 * cores * config.max_cpu_percent / 100)
    if high < page or quota_us < 1000:
        raise ConfigError("Insufficient CPU or memory after the configured reserve to create the worker cgroup")
    return quota_us, high, hard


def _status(path: Path) -> dict:
    cpu_quota, cpu_period = (path / "cpu.max").read_text().split()
    status = {
        "mode": "cgroup-v2", "path": str(path),
        "cpu_quota_cores": int(cpu_quota) / int(cpu_period),
        "memory_high_bytes": int((path / "memory.high").read_text()),
        "memory_max_bytes": int((path / "memory.max").read_text()),
        "memory_current_bytes": int((path / "memory.current").read_text()),
        "memory_reclaimable_file_bytes": reclaimable_file_bytes(path),
        "memory_events": {k: int(v) for k, v in (line.split() for line in
                           (path / "memory.events").read_text().splitlines())},
    }
    peak = path / "memory.peak"
    if peak.exists():
        status["memory_peak_bytes"] = int(peak.read_text())
    stats = dict(line.split() for line in (path / "cpu.stat").read_text().splitlines())
    for name in ("usage_usec", "throttled_usec", "nr_throttled"):
        if name in stats:
            status[f"cpu_{name}"] = int(stats[name])
    hard_free, soft_free, total = _ancestor_memory(path.parent, reclaim_file_cache=True)
    status["ancestor_memory_available_bytes"] = hard_free
    status["ancestor_memory_high_available_bytes"] = soft_free
    status["ancestor_memory_max_bytes"] = total
    return status


def envelope_status() -> dict:
    """Return the current supervisor's aggregate budget and kernel counters."""
    path = _active.get()
    if path is not None:
        return _status(path)
    if preview := _preview.get():
        return preview
    # A supervisor launched from an agent in another workspace keeps the
    # enclosing aggregate limits, even when its own config requests sampling.
    inherited = os.environ.get(_ENV)
    return _status(Path(inherited)) if inherited else {"mode": "sampled"}


def limit_command(command: list[str]) -> list[str]:
    """Enter the group before exec; all descendants inherit that membership."""
    path = _active.get()
    if path is None:
        return command
    return ["/bin/sh", "-c", 'printf "0\\n" > "$1/cgroup.procs" || exit 125\n'
            f'export {_ENV}="$1"\nshift\nexec "$@"', "horizon-cgroup", str(path), *command]


def _remove_empty(path: Path) -> None:
    try:
        path.rmdir()
    except OSError as error:
        # Descendants that outlive their worker keep these limits. A later
        # invocation can reclaim the same group once it has drained.
        if error.errno in (errno.EBUSY, errno.ENOTEMPTY):
            log.warn(f"Worker cgroup is still populated or has subgroups: {path}")
        else:
            raise


@contextmanager
def worker_envelope(config: SchedulerConfig, workspace_root: Path, *, dry_run: bool = False):
    """Create one child per workspace under the operator's delegated parent."""
    if config.cgroup_parent is None:
        yield
        return
    try:
        parent = _delegated_parent(config.cgroup_parent)
        inherited = os.environ.get(_ENV)
        if inherited and not parent.is_relative_to(Path(inherited).resolve()):
            raise ConfigError("The requested parent would move this nested supervisor's workers "
                              "outside their inherited worker cgroup")
        key = hashlib.sha256(os.fsencode(workspace_root.resolve())).hexdigest()[:24]
        path = parent / f"horizon-{key}"
        quota, high, hard = _limits(parent, sample_resources(reclaim_file_cache=False), config)
    except (OSError, ValueError) as exc:
        raise ConfigError(f"Cannot prepare the requested cgroup at {config.cgroup_parent}: {exc}") from exc
    if dry_run:
        token = _preview.set({"mode": "planned", "path": str(path), "cpu_quota_cores": quota / 100_000,
                              "memory_high_bytes": high, "memory_max_bytes": hard})
        try:
            yield
        finally:
            _preview.reset(token)
        return
    try:
        if path.exists():
            # Under the workspace supervisor lock, an empty leaf is a prior
            # invocation's leftover. Never relax limits on a populated group.
            path.rmdir()
        path.mkdir()
    except OSError as exc:
        raise ConfigError(f"Cannot create the dedicated worker group {path}: {exc}") from exc
    token = _active.set(path)
    try:
        try:
            (path / "cpu.max").write_text(f"{quota} 100000")
            (path / "memory.max").write_text(str(hard))
            (path / "memory.high").write_text(str(high))
            # A writable leaf alone does not authorize migration across subtrees.
            # Verify kernel membership permission before recording any worker run.
            subprocess.run(limit_command(["/bin/true"]), check=True, capture_output=True, text=True)
        except (OSError, subprocess.CalledProcessError) as exc:
            detail = getattr(exc, "stderr", "") or str(exc)
            raise ConfigError(f"Cannot enforce worker limits in {path}: {detail.strip()}") from exc
        yield
    finally:
        _active.reset(token)
        _remove_empty(path)
