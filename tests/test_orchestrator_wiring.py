"""Orchestrator wiring: agent freeze and audit telemetry."""

from __future__ import annotations

import json
from pathlib import Path

from archon_horizon.config.loader import build_orchestrator
from archon_horizon.core.roadmap import Roadmap, RoadmapItem, RoadmapStatus
from archon_horizon.core.sessions import Focus, RunRecord
from archon_horizon.core.tasks import HorizonTask, TaskStatus, WriteSet
from archon_horizon.harnesses.null import NullHarness
from archon_horizon.inboxes.filesystem import FilesystemInboxProvider
from archon_horizon.vcs.git import WorkspaceGit

BASE = """
workspace:
  name: w
  rounds: 1
  ground_agent: {{harness: inf}}
  horizon_agent: {{harness: hor}}
harnesses:
  inf: {{kind: "null"}}
  hor: {{kind: "null"}}
projects:
  ag-main: {{path: projects/ag-main{blueprint}}}
{extra}
"""


def _setup(tmp_path: Path, *, blueprint: bool = False, extra: str = "") -> Path:
    root = tmp_path / "ws"
    (root / "projects" / "ag-main").mkdir(parents=True)
    bp = ", blueprint: {path: projects/ag-main/blueprint}" if blueprint else ""
    (root / "config.yaml").write_text(BASE.format(blueprint=bp, extra=extra), "utf-8")
    return root


def test_frozen_horizon_agent_blocks_tasks(tmp_path: Path) -> None:
    root = _setup(tmp_path, extra="freeze:\n  agents: [horizon]\n")
    orch = build_orchestrator(root, harnesses={"inf": NullHarness(""), "hor": NullHarness("")})
    # A human task exists; the frozen Horizon agent blocks it instead of running it.
    orch.task_store.put(HorizonTask(
        id="R-1", project="ag-main", objective="x", title="x",
        projects=("ag-main",), status=TaskStatus.QUEUED, write_set=WriteSet(projects=("ag-main",)),
    ))
    reports = orch.run(RunRecord(id="", focus=Focus(tasks=("R-1",)), rounds_requested=1))

    assert reports[0].tasks_run == ()
    assert orch.task_store.get("R-1").status is TaskStatus.BLOCKED


def test_session_keeps_legacy_ledger_intact(tmp_path: Path) -> None:
    root = _setup(tmp_path)
    git = WorkspaceGit(root)
    git.init()
    git.commit("initial")
    sha = git.current_sha()

    orch = build_orchestrator(root, harnesses={"inf": NullHarness(""), "hor": NullHarness("")})
    orch.task_store.put(HorizonTask(
        id="T-1", project="ag-main", objective="x", title="x",
        projects=("ag-main",), status=TaskStatus.QUEUED, write_set=WriteSet(projects=("ag-main",)),
    ))
    orch.run(RunRecord(id="", focus=Focus(tasks=("T-1",)), rounds_requested=1))

    sessions_dir = root / ".archon-horizon" / "runs" / "0001" / "sessions"
    horizon_sessions = [d for d in sorted(sessions_dir.iterdir()) if "horizon" in d.name]
    assert horizon_sessions
    meta = horizon_sessions[0] / "meta.json"
    assert meta.exists()
    assert sha
    assert "workspace_sha" not in json.loads(meta.read_text())
    assert git.current_sha() == sha
    record = json.loads((meta.parent / "project-git.json").read_text())
    assert record["projects"] == ["ag-main"]
    assert record["commits"] == {}


def test_run_starts_with_project_revisions(tmp_path: Path) -> None:
    root = _setup(tmp_path)

    orch = build_orchestrator(root, harnesses={"inf": NullHarness(""), "hor": NullHarness("")})
    orch.run(RunRecord(id="", rounds_requested=1))

    events = [json.loads(line) for line in (root / ".archon-horizon/events.jsonl").read_text().splitlines()]
    baseline = next(e for e in events if e["type"] == "workspace.run_baseline")
    assert baseline["data"]["project_heads"] == {"ag-main": None}
    assert (root / "projects/ag-main/.git").is_dir()
    assert not WorkspaceGit(root).is_repo()
