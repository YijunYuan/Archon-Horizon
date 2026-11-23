import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from archon_horizon.config.schema import ConfigError, SchedulerConfig
from archon_horizon.orchestration import worker_cgroup as cg
from archon_horizon.orchestration.resources import Resources


def test_sampled_mode_and_invalid_delegation(tmp_path, monkeypatch):
    monkeypatch.delenv("ARCHON_HORIZON_WORKER_CGROUP", raising=False)
    command = ["lean", "Theorem.lean"]
    with cg.worker_envelope(SchedulerConfig(), tmp_path):
        assert cg.limit_command(command) is command
        assert cg.envelope_status() == {"mode": "sampled"}
    for raw in (str(tmp_path), "/sys/fs/cgroup", "relative"):
        with pytest.raises(ConfigError):
            with cg.worker_envelope(SchedulerConfig(cgroup_parent=raw), tmp_path):
                pytest.fail("started work after invalid delegation")


@pytest.mark.skipif(sys.platform != "linux", reason="Linux cgroup v2 accounting")
def test_limits_respect_effective_target_ancestors_and_reserve(tmp_path, monkeypatch):
    monkeypatch.setattr(cg, "_MOUNT", tmp_path)
    parent = tmp_path / "scope" / "jobs"
    parent.mkdir(parents=True)
    (parent.parent / "cpu.max").write_text("50000 100000")
    (parent / "cpu.max").write_text("max 100000")
    (parent.parent / "memory.max").write_text(str(2 * 1024**3))
    (parent.parent / "memory.current").write_text(str(1024**3))
    (parent.parent / "memory.high").write_text("max")
    (parent / "memory.max").write_text("max")
    (parent / "memory.current").write_text("0")
    (parent / "memory.high").write_text("max")
    config = SchedulerConfig(max_cpu_percent=80, min_available_memory_gib=0.25)
    quota, high, hard = cg._limits(parent, Resources(16, 1, 12, 32), config)
    assert quota == 40000
    assert hard == 768 * 1024**2
    assert 0.899 < high / hard <= 0.9
    (parent.parent / "memory.high").write_text(str(1536 * 1024**2))
    assert cg._limits(parent, Resources(16, 1, 12, 32), config)[1] == 512 * 1024**2
    for conf in (replace(config, min_available_memory_gib=1.0), replace(config, max_cpu_percent=0.01)):
        with pytest.raises(ConfigError):
            cg._limits(parent, Resources(16, 1, 12, 32), conf)
    (parent.parent / "memory.max").write_text("max")
    assert cg._limits(parent, Resources(16, 1, 12, 32), SchedulerConfig())[1] == 512 * 1024**2


def test_clean_inactive_credit_is_conservative_and_applied_per_ancestor(tmp_path, monkeypatch):
    monkeypatch.setattr(cg, "_MOUNT", tmp_path)
    outer = tmp_path / "parent"
    inner = outer / "child"
    inner.mkdir(parents=True)
    for path in (inner, outer):
        (path / "memory.current").write_text("1000")
        (path / "memory.max").write_text("1050")
        (path / "memory.high").write_text("max")
    # The same physical cache can be charged in every ancestor's hierarchical stats.
    # Apply the credit per bound, without adding it across the hierarchy.
    for path in (inner, outer):
        (path / "memory.stat").write_text(
            "inactive_file 900\nfile 1000\nshmem 700\nfile_dirty 50\nfile_writeback 50\n"
        )
    assert cg.reclaimable_file_bytes(inner) == 100
    assert cg._ancestor_memory(inner) == (50, None, 1050)  # kernel hard-limit creation budget
    assert cg._ancestor_memory(inner, reclaim_file_cache=True) == (150, None, 1050)
    (outer / "memory.stat").unlink()
    assert cg._ancestor_memory(inner, reclaim_file_cache=True) == (50, None, 1050)
    (inner / "memory.stat").write_text("inactive_file 10\nfile 10\nshmem 0\nfile_dirty 10\nfile_writeback 10\n")
    assert cg.reclaimable_file_bytes(inner) == 0


def test_real_delegation_inheritance_and_cleanup(tmp_path):
    raw = os.environ.get("HORIZON_TEST_CGROUP_PARENT")
    if not raw:
        pytest.skip("set HORIZON_TEST_CGROUP_PARENT to a writable, delegated cgroup v2 test subtree")
    conf = SchedulerConfig(cgroup_parent=raw, min_available_memory_gib=0.01)
    with cg.worker_envelope(conf, tmp_path, dry_run=True):
        planned = cg.envelope_status()
        assert planned["mode"] == "planned"
        leaf = Path(planned["path"])
        assert not leaf.exists()
    parent_membership = Path("/proc/self/cgroup").read_text()
    script = (
        "import json, subprocess; "
        "from archon_horizon.orchestration.worker_cgroup import envelope_status; "
        "print(json.dumps([envelope_status(), "
        "subprocess.check_output(['cat', '/proc/self/cgroup'], text=True)]))"
    )
    with cg.worker_envelope(conf, tmp_path):
        status = cg.envelope_status()
        observed, child_membership = json.loads(subprocess.check_output(cg.limit_command([sys.executable, "-c", script])))
        assert observed["path"] == status["path"] == str(leaf)
        assert leaf.name in child_membership and leaf.name not in parent_membership
        assert status["memory_high_bytes"] < status["memory_max_bytes"]
        assert status["cpu_quota_cores"] > 0 and "oom_kill" in status["memory_events"]
        nested = subprocess.run(cg.limit_command([sys.executable, "-c", """
from pathlib import Path
import sys
from archon_horizon.config.schema import SchedulerConfig
from archon_horizon.orchestration.worker_cgroup import worker_envelope
with worker_envelope(SchedulerConfig(cgroup_parent=sys.argv[1]), Path(sys.argv[2])):
    pass
""", raw, str(tmp_path / "nested-workspace")]), capture_output=True, text=True)
        assert nested.returncode and "outside their inherited worker cgroup" in nested.stderr
    assert cg.envelope_status() == {"mode": "sampled"} and not leaf.exists()

    # A surviving descendant keeps the old limits across a manager exit. The next
    # invocation may reuse the workspace name only after that process has exited.
    survivor = None
    try:
        with cg.worker_envelope(conf, tmp_path):
            survivor = subprocess.Popen(cg.limit_command(["sh", "-c", "echo entered; exec sleep 30"]),
                                        stdout=subprocess.PIPE, text=True)
            assert survivor.stdout.readline().strip() == "entered"
        assert leaf.exists()
        with pytest.raises(ConfigError):
            with cg.worker_envelope(conf, tmp_path):
                pytest.fail("reused a live group")
    finally:
        if survivor is not None:
            survivor.kill()
            survivor.wait()
            survivor.stdout.close()
    with cg.worker_envelope(conf, tmp_path):
        assert cg.envelope_status()["path"] == str(leaf)
    assert not leaf.exists()
