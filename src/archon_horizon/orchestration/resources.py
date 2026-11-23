"""Admission headroom for Lean workers on the local host."""

from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass, field
from math import inf
from pathlib import Path

import psutil

from archon_horizon.config.schema import SchedulerConfig
from .cgroup_memory import reclaimable_file_bytes

_GIB = 1024**3
WORKER_MEMORY_GIB = 2.0


@dataclass(frozen=True)
class ProcessUsage:
    cpu_seconds: float
    rss_gib: float


def process_usage(pid: int) -> ProcessUsage | None:
    """Sum current process-tree RSS and CPU time, including already reaped children."""
    cpu = 0.
    rss = 0
    try:
        parent = psutil.Process(pid)
        for proc in [parent, *parent.children(recursive=True)]:
            try:
                with proc.oneshot():
                    times = proc.cpu_times()
                    rss += proc.memory_info().rss
                    cpu += (times.user + times.system + getattr(times, "children_user", 0.)
                            + getattr(times, "children_system", 0.))
            except psutil.NoSuchProcess:
                continue
    except psutil.Error:
        return None
    return ProcessUsage(cpu, rss / _GIB) if rss else None


def exit_usage() -> dict:
    """Include short-lived waited children that may fit between two polls."""
    try:
        import resource
    except ImportError:
        return {}
    own = resource.getrusage(resource.RUSAGE_SELF)
    children = resource.getrusage(resource.RUSAGE_CHILDREN)
    rss_unit = 1 if sys.platform == "darwin" else 1024
    return {"cpu_seconds": own.ru_utime + own.ru_stime + children.ru_utime + children.ru_stime,
            "max_process_rss_gib": max(own.ru_maxrss, children.ru_maxrss) * rss_unit / _GIB}


@dataclass
class WorkerUsage:
    started: float = field(default_factory=time.monotonic)
    sampled_at: float | None = None
    observations: int = 0
    consecutive: int = 0
    cpu_seconds: float = 0.
    cpu_cores: float = 0.
    rss_gib: float = 0.
    peak_rss_gib: float = 0.

    def observe(self, usage: ProcessUsage | None, now: float) -> None:
        if usage is None:
            self.consecutive = 0
            self.sampled_at = None
            return
        if self.sampled_at is not None and now > self.sampled_at:
            # React promptly to increases and decay slowly across an idle poll.
            cores = max(0., usage.cpu_seconds - self.cpu_seconds) / (now - self.sampled_at)
            self.cpu_cores = max(cores, (self.cpu_cores + cores) / 2)
        self.sampled_at = now
        self.observations += 1
        self.consecutive += 1
        self.cpu_seconds = max(self.cpu_seconds, usage.cpu_seconds)
        self.rss_gib = usage.rss_gib
        self.peak_rss_gib = max(self.peak_rss_gib, usage.rss_gib)

    @property
    def measured(self) -> bool:
        # Three readings give two full intervals of CPU demand. Avoid treating a
        # just-spawned interpreter as a worker waiting on its model.
        return (self.consecutive >= 3 and self.sampled_at is not None
                and self.sampled_at - self.started >= 1.)

    def summary(self, now: float) -> dict:
        return {"elapsed_seconds": round(now - self.started, 3), "samples": self.observations,
                "cpu_seconds": round(self.cpu_seconds, 4), "cpu_cores": round(self.cpu_cores, 3),
                "rss_gib": round(self.rss_gib, 5), "peak_tree_rss_gib": round(self.peak_rss_gib, 5)}


@dataclass(frozen=True)
class AgentBudget:
    cpu_cores: float
    memory_gib: float
    reserved_gib: float = 0.
    reserved_cores: float = 0.
    measured: bool = False


def agent_budget(resources: Resources, workers: list[WorkerUsage], recent_peaks: list[float], *,
                 cold_gib: float | None = None, capacity_gib: float = inf, compiles: dict | None = None) -> AgentBudget:
    measured = bool(workers) and all(w.measured for w in workers)
    # Compilers have a separate budget. One core is enough to start a Python
    # worker; once its cohort settles, estimate agent demand from actual CPU.
    cores = max(.25, 1.25 * sum(w.cpu_cores for w in workers) / len(workers)) if measured else 1.
    # A workspace can resume under a smaller quota or on another host.
    capacity_gib = min(capacity_gib, resources.total_gib or inf)
    feasible = [p for p in recent_peaks if p < capacity_gib]
    peak = max([*feasible, *(w.peak_rss_gib for w in workers)], default=0.)
    cold = resources.worker_memory_gib if cold_gib is None else cold_gib
    memory = max(min(.25, cold) if measured else cold, 1.25 * peak)
    reserved = sum(max(0., max(memory, 1.25 * w.peak_rss_gib) - w.rss_gib) for w in workers)
    # While only model work is running, keep room for at least one compiler.
    # Once a compiler is running its live footprint already reduces free memory.
    compiles = compiles or {}
    if not compiles.get("active"):
        peak_compile = compiles.get("recent_peak_tree_rss_gib", 0.)
        expected = 1.25 * peak_compile if peak_compile < capacity_gib else cold
        reserved += min(capacity_gib, max(cold, expected))
    pending = sum(max(0., min(1., resources.cpu_cores) - w.cpu_cores) for w in workers if not w.measured)
    return AgentBudget(cores, memory, reserved, pending, measured)


@dataclass
class AdmissionController:
    last_headroom: float | None = None
    last_group_cpu: int | None = None
    last_sample: float | None = None
    last_pressure_events: int = 0
    recovering: bool = False

    def poll(self, resources: Resources, config: SchedulerConfig, workers: list[WorkerUsage],
             external_count: int, recent_peaks: list[float], envelope: dict, now: float, compiles: dict | None = None
             ) -> tuple[int, AgentBudget, str]:
        budget = agent_budget(resources, workers, recent_peaks,
                              cold_gib=resources.worker_memory_for(envelope, config),
                              capacity_gib=resources.memory_capacity(config, envelope), compiles=compiles)
        # Allow for another poll's memory growth. Holding launches for one clean
        # poll after pressure avoids a burst whenever workers briefly yield CPU.
        headroom = resources.memory_headroom(config, envelope)
        falling = max(0., self.last_headroom - headroom) if self.last_headroom is not None else 0.
        self.last_headroom = headroom
        cpu = sum(w.cpu_cores for w in workers)
        counter = envelope.get("cpu_usage_usec")
        if counter is not None and self.last_group_cpu is not None and self.last_sample is not None and now > self.last_sample:
            cpu = max(cpu, (counter - self.last_group_cpu) / 1e6 / (now - self.last_sample))
        self.last_group_cpu, self.last_sample = counter, now
        events = envelope.get("memory_events") or {}
        pressure_events = sum(events.get(kind, 0) for kind in ("high", "max", "oom", "oom_kill"))
        group_pressure = pressure_events > self.last_pressure_events
        self.last_pressure_events = pressure_events
        slots = resources.admissions(config, len(workers) + external_count, falling, budget=budget,
                                     envelope=envelope, worker_cpu_cores=cpu)
        reason = "at capacity"
        if resources.cpu_percent >= config.max_cpu_percent or (
                envelope.get("cpu_quota_cores") is not None and cpu >= envelope["cpu_quota_cores"]):
            reason = "waiting for CPU"
        elif group_pressure or headroom - falling - budget.reserved_gib < budget.memory_gib:
            reason = "waiting for memory"
        if group_pressure or (self.recovering and workers):
            slots = 0
        self.recovering = reason != "at capacity"
        return min(2, slots), budget, reason


def _cgroups() -> list[Path]:
    """Visible cgroup v2 ancestors, including a container's mount root."""
    mount = Path("/sys/fs/cgroup")
    if not (mount / "cgroup.controllers").exists():
        return []
    for line in _read(Path("/proc/self/cgroup")).splitlines():
        if line.startswith("0::"):
            member = (mount / line[3:].lstrip("/")).resolve()
            if member.is_relative_to(mount):
                return [p for p in (member, *member.parents) if p.is_relative_to(mount)]
    return [mount]


def _read(path: Path) -> str:
    try:
        return path.read_text().strip()
    except OSError:
        return ""


def _quota_usage(path: Path) -> tuple[float, int] | None:
    quota = _read(path / "cpu.max").split()
    if len(quota) != 2 or not quota[0].isdigit():
        return None
    for line in _read(path / "cpu.stat").splitlines():
        key, _, value = line.partition(" ")
        if key == "usage_usec":
            return int(quota[0]) / int(quota[1]), int(value)
    return None


@dataclass(frozen=True)
class Resources:
    cpu_cores: float
    cpu_percent: float
    available_gib: float
    total_gib: float | None = None
    memory_high_gib: float | None = None
    memory_high_remaining_gib: float | None = None

    @property
    def worker_memory_gib(self) -> float:
        return min(WORKER_MEMORY_GIB, self.total_gib / 3) if self.total_gib else WORKER_MEMORY_GIB

    def worker_memory_for(self, envelope: dict | None, config: SchedulerConfig | None = None) -> float:
        """Cold-start estimate inside both the operator's reserve and the delegated leaf."""
        memory = self.worker_memory_gib
        if config and config.min_available_memory_gib is not None and self.total_gib:
            usable = self.total_gib - config.min_available_memory_gib
            if usable > 0:
                memory = min(memory, usable / 3)
        if self.memory_high_gib is not None and self.memory_high_gib > 0:
            memory = min(memory, self.memory_high_gib / 3)
        for key in ("memory_high_bytes", "memory_max_bytes"):
            if limit := (envelope or {}).get(key):
                memory = min(memory, limit / _GIB / 3)
        return memory

    def memory_reserve(self, config: SchedulerConfig) -> float:
        if config.min_available_memory_gib is not None:
            return config.min_available_memory_gib
        return min(2., self.total_gib / 5) if self.total_gib else 2.

    def _ancestor_reserve(self, config: SchedulerConfig, envelope: dict) -> float:
        reserve = self.memory_reserve(config)
        if config.min_available_memory_gib is None and (cap := envelope.get("ancestor_memory_max_bytes")):
            reserve = min(reserve, cap / _GIB / 5)
        return reserve

    def memory_headroom(self, config: SchedulerConfig, envelope: dict | None) -> float:
        free = self.available_gib - self.memory_reserve(config)
        if self.memory_high_remaining_gib is not None:
            free = min(free, self.memory_high_remaining_gib)
        if envelope and envelope.get("mode") in {"cgroup-v2", "planned"}:
            current = envelope.get("memory_current_bytes", 0)
            for key in ("memory_high_bytes", "memory_max_bytes"):
                if (limit := envelope.get(key)) is not None and current is not None:
                    used = max(0, current - envelope.get("memory_reclaimable_file_bytes", 0))
                    free = min(free, (limit - used) / _GIB)
            if (outer := envelope.get("ancestor_memory_available_bytes")) is not None:
                free = min(free, outer / _GIB - self._ancestor_reserve(config, envelope))
            if (soft := envelope.get("ancestor_memory_high_available_bytes")) is not None:
                free = min(free, soft / _GIB)
        return free

    def memory_capacity(self, config: SchedulerConfig, envelope: dict | None) -> float:
        capacity = self.total_gib - self.memory_reserve(config) if self.total_gib else inf
        if self.memory_high_gib is not None:
            capacity = min(capacity, self.memory_high_gib)
        for key in ("memory_high_bytes", "memory_max_bytes"):
            if (limit := (envelope or {}).get(key)) is not None:
                capacity = min(capacity, limit / _GIB)
        if envelope and (outer := envelope.get("ancestor_memory_max_bytes")) is not None:
            capacity = min(capacity, outer / _GIB - self._ancestor_reserve(config, envelope))
        return max(0., capacity)

    def capacity(self, config: SchedulerConfig, cpu_per_worker: float = 1., envelope: dict | None = None) -> int:
        """CPU-based concurrency ceiling; admission also checks current headroom."""
        cores = min(self.cpu_cores, (envelope or {}).get("cpu_quota_cores", self.cpu_cores))
        slots = max(1, int(cores / cpu_per_worker))
        return min(slots, config.max_parallel_sessions or slots)

    def admissions(self, config: SchedulerConfig, active: int, reserved_gib: float = 0,
                   *, budget: AgentBudget | None = None, envelope: dict | None = None,
                   worker_cpu_cores: float = 0.) -> int:
        if self.cpu_percent >= config.max_cpu_percent:
            return 0
        budget = budget or AgentBudget(1., self.worker_memory_for(envelope, config))
        free_memory = self.memory_headroom(config, envelope)
        spare_cores = self.cpu_cores * (config.max_cpu_percent - self.cpu_percent) / 100
        first_job = min(.5, self.cpu_cores * config.max_cpu_percent / 200)
        if envelope and envelope.get("mode") in {"cgroup-v2", "planned"}:
            if (quota := envelope.get("cpu_quota_cores")) is not None:
                spare_cores = min(spare_cores, quota - worker_cpu_cores)
                first_job = min(first_job, quota / 2)
        memory_slots = int((free_memory - reserved_gib - budget.reserved_gib) / budget.memory_gib)
        idle = spare_cores - budget.reserved_cores
        cpu_slots = int(idle / budget.cpu_cores)
        # Small or fractionally-quota'd hosts can still make forward progress
        # with a single job while at least half their target CPU budget is free.
        if not active and idle >= first_job:
            cpu_slots = max(1, cpu_slots)
            # Historical peaks can be from a different task. If there is room
            # for a cold start, try a single worker without launching a second
            # expensive job alongside it.
            if free_memory - reserved_gib >= self.worker_memory_for(envelope, config):
                memory_slots = max(1, memory_slots)
        return max(0, min(self.capacity(config, budget.cpu_cores, envelope) - active, memory_slots, cpu_slots))


def _sample_cpu(groups: list[Path]) -> tuple[float, float]:
    quotas = {p: usage for p in groups if (usage := _quota_usage(p))}
    affinity = os.sched_getaffinity(0) if hasattr(os, "sched_getaffinity") else None
    started = time.monotonic()
    utilization = psutil.cpu_percent(interval=0.15, percpu=True)
    cpus = [v for i, v in enumerate(utilization) if affinity is None or i in affinity]
    cores = float(len(cpus) or 1)
    busy = sum(cpus) / cores
    elapsed_us = (time.monotonic() - started) * 1e6
    for path, (limit, previous) in quotas.items():
        if now := _quota_usage(path):
            cores = min(cores, limit)
            busy = max(busy, 100 * (now[1] - previous) / (elapsed_us * limit))
    return cores, min(100., busy)


def sample_resources(*, exclude_cgroup: Path | None = None, cached_cpu: tuple[float, float] | None = None,
                     reclaim_file_cache: bool = True) -> Resources:
    # A nested supervisor inherits the enclosing leaf. Its dedicated counters
    # are applied by admissions, and ancestors still constrain the host sample.
    if exclude_cgroup is None and reclaim_file_cache and (inherited := os.environ.get("ARCHON_HORIZON_WORKER_CGROUP")):
        exclude_cgroup = Path(inherited)
    groups = [p for p in _cgroups() if p != exclude_cgroup]
    cores, busy = cached_cpu if cached_cpu is not None else _sample_cpu(groups)
    memory = psutil.virtual_memory()
    available, total = memory.available, memory.total
    soft_limit = soft_remaining = inf
    for path in groups:
        maximum = _read(path / "memory.max")
        high = _read(path / "memory.high")
        used = _read(path / "memory.current")
        if used.isdigit() and (maximum.isdigit() or high.isdigit()):
            reclaimable = reclaimable_file_bytes(path) if reclaim_file_cache else 0
            charged = max(0, int(used) - reclaimable)
            if maximum.isdigit():
                available = min(available, max(0, int(maximum) - charged))
                total = min(total, int(maximum))
            if high.isdigit():
                soft_limit = min(soft_limit, int(high))
                soft_remaining = min(soft_remaining, max(0, int(high) - charged))
    return Resources(cores, busy, available / _GIB, total / _GIB,
                     soft_limit / _GIB if soft_limit != inf else None,
                     soft_remaining / _GIB if soft_remaining != inf else None)
