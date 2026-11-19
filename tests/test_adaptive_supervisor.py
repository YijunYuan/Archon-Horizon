import json
import subprocess
import sys
from pathlib import Path

import yaml

from archon_horizon.config.loader import build_orchestrator, load_config
from archon_horizon.core.locks import task_lease
from archon_horizon.core.tasks import HorizonTask, WriteSet
from archon_horizon.orchestration import supervisor
from archon_horizon.orchestration.resources import Resources


def workspace(root, harness):
    project = root / "projects" / "math"
    project.mkdir(parents=True)
    (root / "config.yaml").write_text(yaml.safe_dump({
        "workspace": {"name": "demo", "rounds": 2, "horizon_agent": {"harness": "worker"},
                      "scheduler": {"poll_seconds": 0.1, "max_parallel_sessions": 2},
                      "budget": {"run_tokens_out": 1}},
        "harnesses": {"worker": harness}, "projects": {"math": {"path": "projects/math"}},
    }))
    return project


def test_parallel_proofs_unlock_a_new_run(tmp_path, monkeypatch):
    agent = tmp_path / "engine.py"
    agent.write_text("""
import os, time, subprocess
from pathlib import Path
env = os.environ
if env['ARCHON_HORIZON_TASK'].startswith('review-'):
    subprocess.run([str(Path(__import__('sys').executable).with_name('horizon')), 'task', 'set', env['ARCHON_HORIZON_TASK'], '--status', 'done'], check=True)
    raise SystemExit()
name = env['ARCHON_HORIZON_TASK_TITLE'].split(':')[-1]
root = Path(env['ARCHON_HORIZON_ROOT'])
(root / (name + '.start')).write_text(str(time.monotonic()))
time.sleep(0.7 if name == 'left' else 1.2)
p = Path(name + '.lean')
p.write_text(p.read_text().replace('sorry', 'trivial'))
subprocess.run([env['HORIZON_GIT'], 'add', p.name], check=True)
subprocess.run([env['HORIZON_GIT'], 'commit', '-m', 'feat: prove ' + name], check=True)
(root / (name + '.end')).write_text(str(time.monotonic()))
print('Verified ' + name)
""")
    project = workspace(tmp_path, {"kind": "command", "command": sys.executable, "args": [str(agent)]})
    # This harness tests scheduling; the real Lake protocol is exercised separately.
    import os
    lake = tmp_path / "lake"
    lake.write_text("#!/bin/sh\nexit 0\n")
    lake.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    (project / "lakefile.toml").write_text('name = "math"\n')
    blueprint = project / "blueprint" / "src"
    blueprint.mkdir(parents=True)
    names = ("left", "right", "conclusion")
    statements = []
    for name in names:
        (project / f"{name}.lean").write_text(f"/- Copyright (c) Test. -/\nmodule\n/-! # Test -/\npublic theorem {name} : True := by sorry\n")
        deps = r"\uses{thm:left, thm:right}" if name == "conclusion" else ""
        statements.append(r"\begin{theorem}\label{thm:" + name + r"}\lean{" + name + r"}\leanok " + deps
                          + r"True.\end{theorem}")
    (blueprint / "content.tex").write_text("\n".join(statements))
    subprocess.run(["git", "init", "-q", str(project)], check=True)
    subprocess.run(["git", "-C", str(project), "add", "."], check=True)
    subprocess.run(["git", "-C", str(project), "-c", "user.name=test", "-c", "user.email=test@example.org",
                    "commit", "-qm", "baseline"], check=True)
    # Another user's staged edit remains in the native index throughout the run.
    (project / "unrelated.txt").write_text("user work")
    subprocess.run(["git", "-C", str(project), "add", "unrelated.txt"], check=True)
    orch = build_orchestrator(tmp_path)
    cfg = load_config(tmp_path).scheduler
    samples = iter([Resources(8, 99, 8), Resources(8, 10, 1)])
    seen = []
    status = orch.workspace.state_path / "supervisor.json"

    def sample():
        if status.exists():
            seen.append(json.loads(status.read_text()))
        return next(samples, Resources(8, 10, 8))

    monkeypatch.setattr(supervisor, "sample_resources", sample)
    result = supervisor.supervise(orch, cfg, {"math"}, set(), rounds=1, dry_run=False)
    assert result["exit_code"] == 0
    assert len({r["run"] for r in result["runs"]}) == 4
    assert {s["status"] for s in seen} >= {"waiting for CPU", "waiting for memory"}
    assert all(not s["active"] for s in seen if s["status"].startswith("waiting"))
    assert any(len(s["active"]) == 2 for s in seen)
    starts = {k: float((tmp_path / (k + ".start")).read_text()) for k in names}
    ends = {k: float((tmp_path / (k + ".end")).read_text()) for k in names}
    assert max(starts["left"], starts["right"]) < min(ends["left"], ends["right"])
    assert starts["conclusion"] >= max(ends["left"], ends["right"])
    for run in result["runs"]:
        directory = orch.run_logs.get(run["run"]).path
        data, = directory.glob("sessions/*-horizon-*/project-git.json")
        commits = json.loads(data.read_text())["commits"].get("math", [])
        assert len(commits) == (0 if run["task"].startswith("review-") else 1)
    assert json.loads(status.read_text())["delivery"] == "delivered"
    staged = subprocess.check_output(["git", "-C", str(project), "diff", "--cached", "--name-only"], text=True)
    assert staged.strip() == "unrelated.txt"
    assert not (tmp_path / ".git").exists()


def test_unknown_scope_blocks_project_and_file_aliases_share_a_lease(tmp_path):
    workspace(tmp_path, {"kind": "null"})
    ws = build_orchestrator(tmp_path).workspace

    def task(name, *files):
        return HorizonTask(name, "math", "prove", write_set=WriteSet(files=files))
    with task_lease(ws, task("a", "A.lean")) as first:
        with task_lease(ws, task("b", "projects/math/A.lean")) as alias:
            with task_lease(ws, task("c", "B.lean")) as other:
                with task_lease(ws, task("d")) as whole:
                    assert first and other and not alias and not whole


def test_goal_plans_without_mutating_the_project_and_honors_milestone_deps(tmp_path, monkeypatch):
    from archon_horizon.cli import main
    from archon_horizon.orchestration.planning import assignments

    p = workspace(tmp_path, {"kind": "null"})
    source = p / "blueprint" / "src"
    source.mkdir(parents=True)
    (source / "content.tex").write_text(
        r"\begin{theorem}\label{thm:a}\lean{a}A.\end{theorem}" "\n"
        r"\begin{theorem}\label{thm:b}\uses{thm:a}B.\end{theorem}" "\n"
        r"\begin{theorem}\label{thm:c}\uses{thm:missing}C.\end{theorem}"
    )
    (p / "A.lean").write_text("theorem a : True := by trivial\n")
    orch = build_orchestrator(tmp_path)
    orch.task_store.put(HorizonTask("paper", "math", "Formalize the source paper"))
    orch.task_store.put(HorizonTask("other", "math", "Unrelated", write_set=WriteSet(files=("Other.lean",))))
    monkeypatch.setattr(supervisor, "sample_resources", lambda: Resources(6, 10, 12))
    plan = supervisor.supervise(orch, load_config(tmp_path).scheduler, set(), {"paper"}, rounds=1, dry_run=True)
    assert [row["title"] for row in plan["planned"]] == ["Verify blueprint correspondence for thm:a"]
    assert {t.id for t in orch.task_store.list()} == {"paper", "other"}
    assert not (p / "hgraph").exists()
    assert not orch.run_logs.ids()

    cmd = ["--root", str(tmp_path), "roadmap", "add", "--project", "math"]
    assert main([*cmd, "--id", "first", "--title", "First", "--file", "First.lean", "--blueprint-node", "a"]) == 0
    assert main([*cmd, "--id", "second", "--title", "Second", "--depends-on", "first"]) == 0
    ready = assignments(orch, {"math"}, set(), {})
    assert "second" not in [t.id for t in ready]
    first = next(t for t in ready if t.id == "first")
    assert first.write_set.files == ("First.lean",) and first.write_set.blueprint_nodes == ("a",)
    selected = assignments(orch, set(), {"second"}, {})
    assert [t.id for t in selected] == ["first"]


def test_independent_unmapped_nodes_request_one_file_plan(tmp_path):
    from archon_horizon.orchestration.planning import assignments

    workspace(tmp_path, {"kind": "null"})
    orch = build_orchestrator(tmp_path)
    dag = {"nodes": [{"id": label, "state": "ready"} for label in ("a", "b")]}
    plan, = assignments(orch, {"math"}, set(), {"math": dag})
    assert plan.metadata["supervisor_source"] == "plan"
    orch.task_store.put(plan)
    retry, = assignments(orch, {"math"}, set(), {"math": dag})
    assert retry.id == plan.id


def test_cli_on_existing_workspace_uses_headroom_with_legacy_width_one(tmp_path, monkeypatch, capsys):
    from archon_horizon.cli import main

    workspace(tmp_path, {"kind": "null"})
    cfg = tmp_path / "config.yaml"
    raw = yaml.safe_load(cfg.read_text())
    raw["workspace"]["scheduler"]["max_parallel_sessions"] = 1
    cfg.write_text(yaml.safe_dump(raw))
    orch = build_orchestrator(tmp_path)
    for name in ("a", "b"):
        orch.task_store.put(HorizonTask(name, "math", "prove", write_set=WriteSet(files=(name + ".lean",))))
    monkeypatch.setattr(supervisor, "sample_resources", lambda: Resources(8, 10, 10))
    assert main(["--root", str(tmp_path), "run", "--dry-run", "--no-dashboard", "--json"]) == 0
    assert {r["task"] for r in json.loads(capsys.readouterr().out)["planned"]} == {"a", "b"}


def test_cgroup_and_affinity_limit_host_headroom(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from archon_horizon.orchestration import resources

    (tmp_path / "memory.max").write_text(str(8 * 1024**3))
    (tmp_path / "memory.current").write_text(str(3 * 1024**3))
    (tmp_path / "cpu.max").write_text("150000 100000")
    (tmp_path / "cpu.stat").write_text("usage_usec 0")
    monkeypatch.setattr(resources, "_cgroups", lambda: [tmp_path])
    monkeypatch.setattr(resources.psutil, "virtual_memory", lambda: SimpleNamespace(available=64 * 1024**3, total=80 * 1024**3))
    monkeypatch.setattr(resources.os, "sched_getaffinity", lambda _: {1, 2})
    monkeypatch.setattr(resources.psutil, "cpu_percent", lambda **kw: [100, 10, 20, 100])
    sample = resources.sample_resources()
    assert sample == Resources(1.5, 15, 5, 8)
    config = resources.SchedulerConfig()
    assert sample.admissions(config, 0) == 1
    assert sample.admissions(config, 0, reserved_gib=2) == 0
