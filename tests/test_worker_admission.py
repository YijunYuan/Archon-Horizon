import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from archon_horizon.config.schema import SchedulerConfig
from archon_horizon.orchestration.resources import (
    AdmissionController, ProcessUsage, Resources, WorkerUsage, agent_budget, process_usage,
)


def measured(cpu=0.05, rss=0.2):
    worker = WorkerUsage(started=0)
    for t in (0, 1, 2):
        worker.observe(ProcessUsage(t * cpu, rss), t)
    return worker


def test_waiting_workers_can_exceed_old_cpu_ceiling_after_observation():
    host = Resources(8, 20, 20, 32)
    workers = [measured() for _ in range(4)]
    def new_jobs(workers, config=SchedulerConfig(), external=0):
        slots, budget, _ = AdmissionController().poll(host, config, workers, external, [], {}, 2)
        return slots, budget
    assert new_jobs(workers)[0] == 2
    assert new_jobs(workers, SchedulerConfig(max_parallel_sessions=6), external=1)[0] == 1
    assert new_jobs(workers, SchedulerConfig(max_parallel_sessions=4))[0] == 0
    warmup = [*workers[:3], WorkerUsage(started=1)]
    assert not new_jobs(warmup)[1].measured
    assert new_jobs(warmup * 2)[0] == 0
    workers[0].observe(None, 3)
    assert not new_jobs(workers)[1].measured
    workers[0].observe(ProcessUsage(.1, .2), 4)
    assert not new_jobs(workers)[1].measured
    assert new_jobs([measured(1.9) for _ in range(4)])[0] == 0


def test_tightening_memory_budget_obeys_peaks_and_backs_off_then_recovers():
    host = Resources(8, 20, 12, 16)
    workers = [measured()]
    cfg = SchedulerConfig()
    policy = AdmissionController()
    assert policy.poll(host, cfg, workers, 0, [], {}, 2)[0] > 0
    # A drop of 6 GiB in a single interval leaves 6 GiB free for now.
    low = Resources(8, 20, 6, 16)
    assert policy.poll(low, cfg, workers, 0, [], {}, 3)[0] == 0
    assert policy.poll(low, cfg, workers, 0, [], {}, 4)[0] == 0
    assert policy.poll(low, cfg, workers, 0, [], {}, 5)[0] > 0
    tracked = measured(rss=3.)
    tracked.observe(ProcessUsage(.1, .1), 3)
    charged = agent_budget(host, [tracked], [])
    assert charged.memory_gib >= 3. and charged.reserved_gib >= 2.9
    assert agent_budget(host, workers, [3.]).memory_gib >= 3.
    assert agent_budget(host, [], [100.]).memory_gib == host.worker_memory_gib
    # Existing estimates should also inform the very first batch of a new invocation.
    assert AdmissionController().poll(host, cfg, [], 0, [8.], {}, 6)[0] == 1


def test_group_headroom_and_pressure_are_independent_of_host_reserve():
    host = Resources(8, 10, 20, 32)
    cfg = SchedulerConfig()
    workers = [measured()]
    group = {"mode": "cgroup-v2", "cpu_quota_cores": 2., "memory_high_bytes": 5 * 1024**3,
             "memory_current_bytes": 5 * 1024**3, "cpu_usage_usec": 0}
    policy = AdmissionController()
    assert policy.poll(host, cfg, workers, 0, [], group, 2)[0] == 0
    group.update(memory_current_bytes=0, cpu_usage_usec=2_000_000)
    assert policy.poll(host, cfg, workers, 0, [], group, 3)[0] == 0
    group.update(memory_events={"high": 1})
    assert policy.poll(host, cfg, workers, 0, [], group, 4)[0] == 0
    assert policy.poll(host, cfg, workers, 0, [], group, 5)[0] == 0
    assert policy.poll(host, cfg, workers, 0, [], group, 6)[0] > 0
    group["cpu_quota_cores"] = .25
    assert policy.poll(host, cfg, [], 0, [], group, 7)[0] == 1


def test_small_delegation_admits_an_agent_and_preserves_larger_compile_budget():
    host = Resources(8, 10, 20, 32)
    group = {"mode": "cgroup-v2", "memory_high_bytes": 1024**3, "memory_current_bytes": 0}
    cfg = SchedulerConfig()
    assert AdmissionController().poll(host, cfg, [], 0, [], group, 0)[0] == 2
    group["ancestor_memory_high_available_bytes"] = 50 * 1024**2
    assert AdmissionController().poll(host, cfg, [], 0, [], group, 0)[0] == 0
    worker = measured()
    history = {"recent_peak_tree_rss_gib": 4., "active": 0}
    waiting = agent_budget(host, [worker], [], compiles=history)
    compiling = agent_budget(host, [worker], [], compiles={**history, "active": 1})
    assert waiting.reserved_gib - compiling.reserved_gib == 5.
    _, smaller, _ = AdmissionController().poll(host, cfg, [worker], 0, [30.], group, 0,
                                                {"recent_peak_tree_rss_gib": 40.})
    assert smaller.memory_gib < .5 and smaller.reserved_gib < .5


def test_usage_samples_an_actual_child_and_accounts_for_its_exit(tmp_path: Path):
    code = (
        "import sys; allocation = bytearray(24 * 1024**2); print('ready', flush=True); sys.stdin.read()"
    )
    before = process_usage(os.getpid())
    with subprocess.Popen([sys.executable, "-c", code], cwd=tmp_path, stdin=subprocess.PIPE,
                          stdout=subprocess.PIPE, text=True) as child:
        try:
            assert child.stdout.readline().strip() == "ready"
            during = process_usage(os.getpid())
            assert during.rss_gib - before.rss_gib >= .02
            assert during.cpu_seconds >= before.cpu_seconds
        finally:
            child.stdin.close()
    after = process_usage(os.getpid())
    assert during.rss_gib - after.rss_gib >= .02


def test_live_headroom_honors_a_soft_ancestor_limit_and_refreshes_memory_with_cached_cpu(tmp_path, monkeypatch):
    from archon_horizon.orchestration import resources

    gib = 1024**3
    (tmp_path / "memory.high").write_text(str(gib))
    (tmp_path / "memory.max").write_text("max")
    (tmp_path / "memory.current").write_text(str(gib // 4))
    monkeypatch.setattr(resources, "_cgroups", lambda: [tmp_path])
    monkeypatch.setattr(resources.psutil, "virtual_memory", lambda: SimpleNamespace(available=10 * gib, total=16 * gib))
    sampled = resources.sample_resources(cached_cpu=(4., 35.))
    assert sampled.memory_headroom(SchedulerConfig(), {}) == .75
    assert sampled.worker_memory_for({}) == 1 / 3
    (tmp_path / "memory.current").write_text(str(gib))
    assert resources.sample_resources(cached_cpu=(4., 35.)).memory_headroom(SchedulerConfig(), {}) == 0.
    monkeypatch.setenv("ARCHON_HORIZON_WORKER_CGROUP", str(tmp_path))
    assert resources.sample_resources(cached_cpu=(4., 35.)).memory_high_gib is None
