"""Exercise real subprocess leases, tool lookup and separate compile admission."""
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import signal
import subprocess
import time

import pytest

from archon_horizon.config.schema import SchedulerConfig
from archon_horizon.orchestration import compile_gate as gate
from archon_horizon.orchestration.resources import Resources


def workspace(tmp_path):
    (tmp_path / "config.yaml").write_text(
        "workspace:\n  name: gate\n  scheduler:\n    max_parallel_compiles: 1\n"
        "    max_cpu_percent: 100\n    min_available_memory_gib: 0\n    poll_seconds: 0.1\n"
    )
    tools = tmp_path / "tools"
    tools.mkdir()
    (tools / "lean").write_text(
        '#!/bin/sh\ncase "$1" in --version) echo "fake lean"; exit;; esac\n'
        'touch "$1.started"\nwhile ! test -e "$1.finish"; do sleep 0.05; done\n'
    )
    (tools / "lake").write_text(
        '#!/bin/sh\ncase "$1" in env) shift; PATH="$TEST_TOOLS:$PATH" exec "$@";;'
        ' build) exec lean "$2";; esac\n'
    )
    for tool in tools.iterdir():
        tool.chmod(0o755)
    state = tmp_path / "state"
    env = {**os.environ, **gate.compile_environment(tmp_path, state,
           str(tools) + os.pathsep + os.environ["PATH"]), "TEST_TOOLS": str(tools)}
    return state, env


def until(predicate, *, timeout=15):
    end = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > end:
            raise AssertionError("Timed out waiting for the test process")
        time.sleep(.025)


def test_builds_from_independent_shells_queue_and_nested_lean_shares_lease(tmp_path):
    state, env = workspace(tmp_path)
    def spawn(args):
        return subprocess.Popen(args, cwd=tmp_path, env=env, start_new_session=True)
    first = spawn(["lake", "build", "first"])
    second = None
    try:
        until((tmp_path / "first.started").exists)
        # Enter a Lake environment whose PATH points to its toolchain ahead of our shims.
        second = spawn(["lake", "env", "sh", "-c", "lake build second"])
        until(lambda: gate.compile_status(state)["waiting"] == 1)
        assert gate.compile_status(state)["active"] == 1
        assert not (tmp_path / "second.started").exists()
        quick = subprocess.run(["lake", "env", "lean", "--version"], cwd=tmp_path, env=env,
                               capture_output=True, text=True, timeout=10, check=True)
        assert quick.stdout.strip() == "fake lean"
        (tmp_path / "first.finish").touch()
        first.wait(timeout=10)
        until((tmp_path / "second.started").exists)
        (tmp_path / "second.finish").touch()
        assert second.wait(timeout=10) == 0
    finally:
        for proc in [first, second]:
            if proc and proc.poll() is None:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
    status = gate.compile_status(state)
    assert status["finished"] == 2 and status["peak_tree_rss_gib"] > 0
    assert status["active"] == status["waiting"] == 0


def test_cancelled_build_releases_slot_and_wait_is_excluded_from_kernel_time(tmp_path):
    from archon_horizon.lean.process import run_lean_command

    state, env = workspace(tmp_path)
    first = subprocess.Popen(["lean", "first"], cwd=tmp_path, env=env, start_new_session=True)
    try:
        until((tmp_path / "first.started").exists)
        def timed():
            from unittest.mock import patch
            with patch.dict(os.environ, env):
                return run_lean_command(["lean", "second"], tmp_path, timeout=.6)
        with ThreadPoolExecutor() as pool:
            future = pool.submit(timed)
            try:
                until(lambda: gate.compile_status(state)["waiting"] == 1)
                time.sleep(.7)
                assert not future.done()
            finally:
                (tmp_path / "second.finish").touch()
                if first.poll() is None:
                    os.killpg(first.pid, signal.SIGKILL)
                    first.wait(timeout=5)
            ok, elapsed, output = future.result(timeout=10)
            assert ok and elapsed < .6, (elapsed, output)
    finally:
        if first.poll() is None:
            os.killpg(first.pid, signal.SIGKILL)
            first.wait()
    assert gate.compile_status(state)["active"] == 0


def test_headroom_credits_usage_already_visible_and_protects_against_simultaneous_starts():
    cfg = SchedulerConfig()
    r = Resources(8, 55, 7., 32.)
    hint = gate.Demand(2., 2.)
    active = {Path("a"): {"pid": 1, "memory": 4., "cores": 2.}}
    # 2.8 cores and 5GiB left. A fresh active program can claim much of this headroom.
    assert not gate._allowed(cfg, r, {}, hint, active, {})
    assert gate._allowed(cfg, r, {}, hint, active, {1: gate.Demand(3., 2.)})
    assert not gate._allowed(cfg, Resources(8, 85, 20., 32.), {}, hint, active, {1: hint})
    with pytest.raises(OSError, match="after the reserve"):
        gate._allowed(SchedulerConfig(min_available_memory_gib=8.), Resources(8, 0, 8., 8.), {}, hint, {}, {})


def test_compile_gate_classifies_lake_options_and_keeps_servers_and_inspection_outside_slots():
    assert gate._command_kind("lake", ["+lean4:v4.36.0", "-d", "x", "-K", "k=v", "build", "A"]) == "build"
    assert gate._command_kind("lake", ["-d", "dir", "env", "lean", "A.lean"]) == "lean"
    assert gate._command_kind("lake", ["env", "bash", "-c", "lake build"]) is None
    assert gate._command_kind("lake", ["env", "lean", "--server"]) is None
    assert gate._command_kind("lake", ["serve"]) is None
    assert gate._command_kind("lean", ["--run", "A.lean", "--version"]) == "lean"


def test_tiny_delegated_budget_leaves_room_for_first_compile_and_uses_soft_limit():
    cfg = SchedulerConfig()
    r = Resources(8, 8., 20., 32.)
    group = {"mode": "cgroup-v2", "memory_high_bytes": 768 * 1024**2,
             "memory_current_bytes": 128 * 1024**2, "cpu_quota_cores": .2, "busy_cores": .01}
    hint = gate.Demand(r.worker_memory_for(group, cfg), .2)
    assert gate._allowed(cfg, r, group, hint, {}, {})
    assert not gate._allowed(cfg, r, {**group, "busy_cores": .2}, hint, {}, {})


def test_only_one_smaller_build_can_overtake_oldest_while_it_cannot_fit(tmp_path, monkeypatch):
    from threading import Event
    import json

    directory = tmp_path / "compiles"
    directory.mkdir()
    available = [1.]
    monkeypatch.setattr(gate, "_headroom", lambda *a: (Resources(8, 0., available[0], 16.), {}, {}))
    cfg = SchedulerConfig(poll_seconds=.1, min_available_memory_gib=0., max_parallel_compiles=1)
    big = gate._Lease(directory, "big")
    small = gate._Lease(directory, "small")
    later = gate._Lease(directory, "small")
    small.history.write_text(json.dumps([{"memory": .2, "cores": .25, "samples": 3}]))
    entered, release = [Event() for _ in range(3)], [Event() for _ in range(3)]
    def run(i, lease):
        try:
            lease.acquire(cfg)
            entered[i].set()
            release[i].wait(10)
        finally:
            lease.close()
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = []
        try:
            futures.append(pool.submit(run, 0, big))
            until(lambda: list(directory.glob("*.waiting")))
            futures.append(pool.submit(run, 1, small))
            assert entered[1].wait(5)
            release[1].set()
            futures[1].result(timeout=5)
            futures.append(pool.submit(run, 2, later))
            until(lambda: len(list(directory.glob("*.waiting"))) == 2)
            assert not entered[2].wait(.3)
            available[0] = 8.
            assert entered[0].wait(5)
            assert not entered[2].is_set()
        finally:
            available[0] = 8.
            for e in release:
                e.set()
        for f in futures:
            f.result(timeout=5)


def test_a_worker_that_cannot_fit_its_own_compiler_returns_without_waiting(tmp_path, monkeypatch):
    directory = tmp_path / "compiles"
    directory.mkdir()
    monkeypatch.setattr(gate, "_headroom", lambda *_: (Resources(4, 10, .4, 2.4), {}, {}))
    monkeypatch.setattr(gate, "_caller_floor", lambda _: 1.8)
    lease = gate._Lease(directory, "compile")
    try:
        with pytest.raises(OSError, match="waiting worker's private resident"):
            lease.acquire(SchedulerConfig(min_available_memory_gib=0.))
    finally:
        lease.close()
    assert gate.compile_status(tmp_path)["waiting"] == 0


def test_rapid_memory_fall_defers_heavy_work_until_headroom_settles(tmp_path, monkeypatch):
    clock = iter([1., 1.3, 1.6])
    monkeypatch.setattr(gate.time, "monotonic", lambda: next(clock))
    sample = tmp_path / "memory.json"
    assert not gate._falling_memory(sample, 5., 2., 5.)
    assert gate._falling_memory(sample, 4., 2., 5.)
    assert not gate._falling_memory(sample, 4., 2., 5.)


def test_nested_workspace_and_harness_override_resolve_underlying_tool_once(tmp_path, monkeypatch):
    outer = tmp_path / "outer"
    inner = tmp_path / "inner"
    outer.mkdir()
    inner.mkdir()
    _, outer_env = workspace(outer)
    inner_state, inner_env = workspace(inner)
    alias = tmp_path / "outer-alias"
    alias.symlink_to(Path(outer_env["HORIZON_LAKE"]).parent, target_is_directory=True)
    dirty_path = os.pathsep.join([outer_env["PATH"], str(alias), inner_env["PATH"]])
    merged = gate.compile_environment(inner, inner_state, dirty_path)
    assert str(alias) not in merged["PATH"]
    assert str(Path(outer_env["HORIZON_LAKE"]).parent) not in merged["PATH"]
    for k, v in merged.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("PATH", dirty_path)
    monkeypatch.setenv("HORIZON_COMPILE_LEASE", str(outer / "state" / "compiles" / "outer.active"))
    # Even an inherited absolute wrapper or harness-supplied stale PATH must launch a native tool.
    with gate.admitted_command([outer_env["HORIZON_LEAN"], "File.lean"]) as launch:
        assert launch.argv[0] == str(outer / "tools" / "lean")
        assert Path(launch.env["HORIZON_COMPILE_LEASE"]).parent == inner_state / "compiles"
        assert launch.lease is not None
