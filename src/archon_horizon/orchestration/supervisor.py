"""Adaptive local supervisor. Each admitted assignment owns a normal, resumable run."""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
import threading
from contextlib import contextmanager
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import asdict, dataclass, replace
from pathlib import Path

import psutil
import yaml

from archon_horizon.blueprint.workspace import project_dag
from archon_horizon.commands.ps import live_runs, terminate_local_runs
from archon_horizon.config.schema import ConfigError, SchedulerConfig
from archon_horizon.core.clock import utc_now
from archon_horizon.core.locks import task_claim, workspace_lock
from archon_horizon.core.sessions import Focus, RunRecord
from archon_horizon.core.tasks import HorizonTask, TaskStatus
from archon_horizon.log import log
from archon_horizon.hgraph.graph import HGraphError
from archon_horizon.core.atomic import write_text_atomic

from .orchestrator import Orchestrator
from .planning import assignments
from .quality_review import QualityReview
from .resources import Resources, sample_resources
from .scheduler import write_sets_conflict


@dataclass
class Worker:
    task: HorizonTask
    process: subprocess.Popen

    def resident_gib(self) -> float:
        total = 0
        try:
            parent = psutil.Process(self.process.pid)
            for p in [parent, *parent.children(recursive=True)]:
                try:
                    total += p.memory_info().rss
                except psutil.NoSuchProcess:
                    continue
        except psutil.Error:
            pass
        return total / 1024**3


def _start(orch: Orchestrator, task: HorizonTask, resources: Resources) -> tuple[str, Worker]:
    run = orch.run_logs.allocate()
    assignment = {"source": task.metadata.get("supervisor_source") or ("roadmap" if task.roadmap_refs else "queue"),
                  "files": list(task.write_set.files), "blueprint_nodes": list(task.write_set.blueprint_nodes),
                  "roadmap_refs": list(task.roadmap_refs), "resources": asdict(resources)}
    orch.run_store.put(RunRecord(id=run.id, focus=Focus(tasks=(task.id,)),
                                 metadata={"supervised": True, "assignment": assignment}))
    with (run.path / "worker.log").open("w") as output:
        proc = subprocess.Popen([sys.executable, "-m", "archon_horizon.orchestration.supervisor",
                                 str(orch.workspace.root.resolve()), run.id], stdout=output,
                                stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)
    log.step(f"Run {run.id} → {task.project}: {task.title or task.id}")
    return run.id, Worker(task, proc)


def _stop_workers(workers: dict[str, Worker]) -> None:
    terminate_local_runs([w.process.pid for w in workers.values()], timeout=3)
    for worker in workers.values():
        worker.process.wait()


def _external_claims(orch, workers):
    result = []
    for row in live_runs(orch.workspace.state_path / "runs", reap=False):
        if row["run"] in workers:
            continue
        try:
            task = orch.task_store.get(row["task"]) if row.get("task") else None
        except FileNotFoundError:
            task = None
        if task is None:
            names = row.get("projects") or ()
            names = (names,) if isinstance(names, str) else tuple(names)
            task = HorizonTask(id=row["run"], project="", projects=names, objective="")
        result.append(task_claim(orch.workspace, task))
    return result


def _refresh_dags(orch, projects, preview=False):
    result = {}
    for name in sorted(projects):
        if orch.workspace.project(name).type != "lean" or orch.freeze.project_rule(name):
            continue
        # None means a blueprint needs authoring. A parse failure is a distinct
        # condition; do not send an orientation agent to replace a broken graph.
        try:
            result[name] = project_dag(orch.workspace, name, preview=preview)
        except (OSError, ValueError, RuntimeError, HGraphError, yaml.YAMLError) as exc:
            log.warn(f"Could not plan from the blueprint for {name}: {exc}")
    return result


def supervise(orch: Orchestrator, config: SchedulerConfig, projects: set[str], ids: set[str],
              *, rounds: int, dry_run: bool, create_tasks: bool = True) -> dict:
    if rounds < 1:
        raise ConfigError("Supervisor rounds must be positive")
    state = orch.workspace.state_path
    with workspace_lock(state, "supervisor", wait=False) as acquired:
        if not acquired:
            raise ConfigError("A supervisor is already running in this workspace")
        return _drive(orch, config, projects, ids, rounds, dry_run, create_tasks)


def _snapshot(status, resources, config, workers, slots=0):
    return {"status": status, "sampled_at": utc_now().isoformat(), **asdict(resources),
            "capacity": resources.capacity(config), "admissions": slots,
            "active": [{"run": run, "task": w.task.id, "project": w.task.project, "title": w.task.title}
                       for run, w in workers.items()],
            "pid": os.getpid(), "host": socket.gethostname()}


@contextmanager
def _monitor_delivery(path, config, enabled):
    """Keep sampling while the main thread owns interruptible Lake processes."""
    if not enabled:
        yield
        return
    stop = threading.Event()

    def sample():
        while not stop.wait(config.poll_seconds):
            data = _snapshot("verifying delivery", sample_resources(), config, {})
            write_text_atomic(path, json.dumps(data))

    thread = threading.Thread(target=sample, daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join()


def _choose(orch, options, attempts, paused_tasks, occupied, rounds, limit):
    selected = []
    for task in sorted(options, key=lambda t: attempts[t.id]):
        allowance = max(2, rounds) if task.metadata.get("supervisor_source") == "quality" else rounds
        if attempts[task.id] >= allowance or task.id in paused_tasks:
            continue
        claim = task_claim(orch.workspace, task)
        if any(write_sets_conflict(claim, other) for other in occupied):
            continue
        occupied.append(claim)
        selected.append(task)
        if len(selected) >= limit:
            break
    return selected


def _admit(orch, task, resources):
    try:
        current = orch.task_store.get(task.id)
    except FileNotFoundError:
        current = None
    if current and task.metadata.get("supervisor_source") == "quality":
        task = replace(task, created_at=current.created_at)
        orch.task_store.put(task)
    elif current and current.status is not TaskStatus.QUEUED:
        return None
    if current is None or (task.metadata.get("supervisor_source") and
                           (current.write_set != task.write_set or current.objective != task.objective)):
        orch.task_store.put(task)
    return _start(orch, task, resources)


def _drive(orch, config, projects, ids, rounds, dry_run, create_tasks):
    workers: dict[str, Worker] = {}
    started = []
    attempts: Counter[str] = Counter()
    paused_tasks = set()
    exit_code = 0
    halt = False
    state = orch.workspace.state_path
    status_file = state / "supervisor.json"
    next_sync = 0.
    previous_status = ""
    stop_reason = ""
    snapshot = {}
    scanned = projects | {t.project for t in orch.task_store.list() if t.id in ids and t.project in orch.workspace.projects}
    review = QualityReview(orch, projects, ids, dry_run)
    scanned.update(review.names)
    delivery_scope = None
    if create_tasks and review.names:
        scope = HorizonTask("delivery", review.names[0], "", projects=tuple(review.names))
        delivery_scope = task_claim(orch.workspace, scope)
    delivery_busy = False
    old_term = signal.getsignal(signal.SIGTERM)

    def interrupt(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupt)
    try:
        while True:
            for run_id, worker in list(workers.items()):
                code = worker.process.poll()
                if code is None:
                    continue
                del workers[run_id]
                next_sync = 0.
                if code:
                    exit_code = code if code > 0 else 1
                    paused_tasks.add(worker.task.id)
                    marker = orch.run_logs.get(run_id).path / "paused.json"
                    reason = json.loads(marker.read_text())["reason"] if marker.exists() else "worker failure"
                    halt |= reason != "session_budget"
                    log.warn(f"Run {run_id} stopped ({reason}); see runs/{run_id}/worker.log")

            resources = sample_resources()
            if time.monotonic() >= next_sync and not halt:
                # On a large paper a sync can outlast multiple telemetry intervals.
                # Keep sampling while the graph is refreshed, before admitting workers.
                with ThreadPoolExecutor(max_workers=1) as pool:
                    future = pool.submit(_refresh_dags, orch, scanned, dry_run)
                    while not wait([future], timeout=config.poll_seconds).done:
                        resources = sample_resources()
                        if not dry_run:
                            snapshot = _snapshot("reading blueprint", resources, config, workers)
                            write_text_atomic(status_file, json.dumps(snapshot))
                    dags = future.result()
                next_sync = time.monotonic() + max(30., config.poll_seconds)
            external = _external_claims(orch, workers)
            was_busy = delivery_busy
            delivery_busy = delivery_scope is not None and any(write_sets_conflict(delivery_scope, c) for c in external)
            if was_busy and not delivery_busy and not halt:
                # The other run may have opened or closed nodes since our last sync.
                next_sync = 0.
                continue
            reserved = sum(max(0., resources.worker_memory_gib - w.resident_gib()) for w in workers.values())
            slots = min(resources.admissions(config, len(workers) + len(external), reserved),
                        max(2, len(workers)))
            occupied = external + [task_claim(orch.workspace, w.task) for w in workers.values()]
            candidates = []
            options = []
            waiting_for_check = False
            if not halt:
                options = assignments(orch, projects, ids, dags, create=create_tasks,
                                      active=[w.task for w in workers.values()])
                proof_options = options
                if review.reviewing:
                    # A temporarily open node during review belongs to the reviewer.
                    # A new explicitly queued proof task can still restart proof work.
                    options = [t for t in options if not t.metadata.get("supervisor_source")]
                limit = max(1, slots)
                candidates = _choose(orch, options, attempts, paused_tasks, occupied, rounds, limit)

                if not options and not workers and delivery_scope is not None and not delivery_busy:
                    with _monitor_delivery(status_file, config, not dry_run and slots > 0):
                        review_task = review.next_task(dags, measure=slots > 0)
                    waiting_for_check = slots == 0 and review.status == "verifying delivery"
                    if review_task and not dry_run:
                        resources = sample_resources()
                        external = _external_claims(orch, workers)
                        occupied = list(external)
                        delivery_busy = any(write_sets_conflict(delivery_scope, c) for c in external)
                        slots = min(slots, resources.admissions(config, len(external)))
                    if review_task:
                        options = [review_task]
                        candidates = _choose(orch, options, attempts, paused_tasks, occupied, rounds, 1)
                    elif proof_options and not review.reviewing:
                        options = proof_options
                        candidates = _choose(orch, options, attempts, paused_tasks, occupied, rounds, limit)

            if dry_run:
                return {"dry_run": True, "resources": asdict(resources), "admissions": slots,
                        "planned": [{"task": t.id, "title": t.title, "projects": t.projects}
                                    for t in candidates[:slots]], "exit_code": 0}

            for task in candidates[:slots]:
                admitted = _admit(orch, task, resources)
                if not admitted:
                    continue
                run_id, worker = admitted
                workers[run_id] = worker
                if task.metadata.get("supervisor_source") == "quality":
                    review.record["run"] = run_id
                    review.publish("quality review", review.detail)
                attempts[task.id] += 1
                started.append({"run": run_id, "task": task.id, "title": task.title, "project": task.project})

            mode = review.status or "running"
            if any(w.task.metadata.get("supervisor_source") != "quality" for w in workers.values()):
                mode = "running"
            if candidates and slots == 0:
                if resources.cpu_percent >= config.max_cpu_percent:
                    mode = "waiting for CPU"
                elif resources.available_gib - reserved < resources.memory_reserve(config) + resources.worker_memory_gib:
                    mode = "waiting for memory"
                else:
                    mode = "at capacity"
            if halt:
                mode = "draining"
            if mode != previous_status:
                log.info(f"Supervisor {mode} ({resources.cpu_percent:.0f}% CPU, {resources.available_gib:.1f} GiB available)")
                previous_status = mode
            snapshot = _snapshot(mode, resources, config, workers, slots)
            if review.status:
                snapshot.update(delivery=review.status, detail=review.detail)
            write_text_atomic(status_file, json.dumps(snapshot))
            externally_blocked = any(attempts[t.id] < rounds and t.id not in paused_tasks and
                                     any(write_sets_conflict(task_claim(orch.workspace, t), c) for c in external)
                                     for t in options)
            waiting = externally_blocked or delivery_busy or waiting_for_check
            if not workers and not candidates and (halt or not waiting):
                if options and not exit_code:
                    # No live claim can free these remaining assignments; this invocation
                    # has used their attempts. Keep the tasks queued for the next run.
                    exit_code = 3
                    stop_reason = "task attempts exhausted"
                    log.warn(stop_reason)
                elif not halt and review.status in ("quality review", "awaiting user", "proofs pending"):
                    exit_code = 3
                    log.warn(review.detail or "Quality review requires follow-up")
                break
            time.sleep(config.poll_seconds)
    finally:
        signal.signal(signal.SIGTERM, old_term)
        if not dry_run:
            interrupted = bool(workers) or sys.exc_info()[0] is not None
            if workers:
                _stop_workers(workers)
            status = "interrupted" if interrupted else {0: "finished", 3: "paused"}.get(exit_code, "failed")
            snapshot.update(status=status, active=[], admissions=0, sampled_at=utc_now().isoformat(),
                            reason=stop_reason or review.detail or ("no ready work" if status == "finished" else status))
            write_text_atomic(status_file, json.dumps(snapshot))
    return {"dry_run": False, "runs": started, "reason": snapshot["reason"], "exit_code": exit_code}


def _worker(root: Path, run_id: str) -> int:
    from archon_horizon.commands.shared import inbox_providers, load_workspace
    from archon_horizon.config.loader import build_orchestrator

    cfg, ws = load_workspace(root)
    _, providers = inbox_providers(cfg, ws)
    orch = build_orchestrator(root, inbox_providers=providers)
    orch.run(orch.run_store.get(run_id))
    return 3 if orch.last_paused else 0


if __name__ == "__main__":
    sys.exit(_worker(Path(sys.argv[1]), sys.argv[2]))
