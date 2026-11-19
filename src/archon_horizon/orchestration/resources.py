"""Admission headroom for Lean workers on the local host."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path

import psutil

from archon_horizon.config.schema import SchedulerConfig

_GIB = 1024**3
WORKER_MEMORY_GIB = 2.0


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

    @property
    def worker_memory_gib(self) -> float:
        return min(WORKER_MEMORY_GIB, self.total_gib / 3) if self.total_gib else WORKER_MEMORY_GIB

    def memory_reserve(self, config: SchedulerConfig) -> float:
        if config.min_available_memory_gib is not None:
            return config.min_available_memory_gib
        return min(2., self.total_gib / 5) if self.total_gib else 2.

    def capacity(self, config: SchedulerConfig) -> int:
        """CPU-based concurrency ceiling; admission also checks current headroom."""
        slots = max(1, int(self.cpu_cores / 2))
        return min(slots, config.max_parallel_sessions or slots)

    def admissions(self, config: SchedulerConfig, active: int, reserved_gib: float = 0) -> int:
        if self.cpu_percent >= config.max_cpu_percent:
            return 0
        free_memory = self.available_gib - reserved_gib - self.memory_reserve(config)
        memory_slots = max(0, int(free_memory / self.worker_memory_gib))
        idle_cores = self.cpu_cores * (1 - self.cpu_percent / 100)
        return max(0, min(self.capacity(config) - active, memory_slots, max(1, int(idle_cores / 2))))


def sample_resources() -> Resources:
    groups = _cgroups()
    quotas = {p: usage for p in groups if (usage := _quota_usage(p))}
    affinity = os.sched_getaffinity(0) if hasattr(os, "sched_getaffinity") else None
    started = time.monotonic()
    utilization = psutil.cpu_percent(interval=0.15, percpu=True)
    cpus = [v for i, v in enumerate(utilization) if affinity is None or i in affinity]
    cores = float(len(cpus) or 1)
    busy = sum(cpus) / cores
    elapsed_us = (time.monotonic() - started) * 1e6
    memory = psutil.virtual_memory()
    available, total = memory.available, memory.total
    for path in groups:
        maximum = _read(path / "memory.max")
        used = _read(path / "memory.current")
        if maximum.isdigit() and used.isdigit():
            available = min(available, max(0, int(maximum) - int(used)))
            total = min(total, int(maximum))
        if path in quotas and (now := _quota_usage(path)):
            limit, previous = quotas[path]
            cores = min(cores, limit)
            busy = max(busy, 100 * (now[1] - previous) / (elapsed_us * limit))
    return Resources(cores, min(100., busy), available / _GIB, total / _GIB)
