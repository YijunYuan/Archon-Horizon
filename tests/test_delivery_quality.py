import json
import os
from dataclasses import replace
import subprocess
import sys

import pytest
import yaml

from archon_horizon.cli import main
from archon_horizon.server.service import WorkspaceService
from archon_horizon.config.loader import build_orchestrator, load_config
from archon_horizon.config.schema import ConfigError, ProjectConfig
from archon_horizon.commands.agent_hook import hook_response
from archon_horizon.core.tasks import HorizonTask, TaskStatus
from archon_horizon.inboxes.filesystem import FilesystemInboxProvider
from archon_horizon.lean import delivery
from archon_horizon.lean.quality import protected_settings
from archon_horizon.orchestration import supervisor
from archon_horizon.orchestration.quality_review import QualityReview
from archon_horizon.orchestration.resources import Resources
from archon_horizon.vcs.project_git import session_git_environment

HEADER = "/- Copyright (c) test. -/\nmodule\n/-!\n# Test\n-/\n"


def git(root, *args, env=None):
    return subprocess.run(["git", "-C", str(root), *args], text=True, capture_output=True, env=env)


def fixture(tmp_path, monkeypatch, worker=None):
    member = tmp_path / "member"
    member.mkdir()
    doc = {"projects": {"m": {"path": "member"}}, "workspace": {"name": "test", "rounds": 1,
           "scheduler": {"poll_seconds": 0.1}, "horizon_agent": {"harness": "worker"}},
           "harnesses": {"worker": {"kind": "command", "command": sys.executable, "args": [str(worker)]}
                         if worker else {"kind": "null"}}}
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(doc))
    (member / "lakefile.toml").write_text('name = "M"\n')
    git(member, "init", "-q").check_returncode()
    git(member, "config", "user.name", "test").check_returncode()
    git(member, "config", "user.email", "fixture@local").check_returncode()
    (member / "M.lean").write_text(HEADER + "public theorem example_ok : True := by trivial\n")
    git(member, "add", ".").check_returncode()
    git(member, "commit", "-qm", "baseline").check_returncode()
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "lake").write_text('#!/bin/sh\necho "$@" >> "$PWD/../lake.calls"\n')
    (fake / "lake").chmod(0o755)
    monkeypatch.setenv("PATH", str(fake) + os.pathsep + os.environ["PATH"])
    return build_orchestrator(tmp_path), member


def test_legacy_decision_then_independent_repair_and_delivery(tmp_path, monkeypatch):
    script = tmp_path / "agent.py"
    script.write_text("""
import json, os, subprocess, sys
from pathlib import Path
env = os.environ
assert env['ARCHON_HORIZON_TASK'].startswith('review-')
root = Path(env['ARCHON_HORIZON_ROOT'])
if (root/'repair-authorized').exists():
    file = Path('M.lean')
    file.write_text(file.read_text().replace('-- Horizon run 0001 checkpoint.\\n', ''))
    subprocess.run([env['HORIZON_GIT'], 'commit', '-m', 'doc: remove session history', '--', 'M.lean'], check=True)
# An agent's assertion alone cannot pass the system gate.
subprocess.run([str(Path(sys.executable).with_name('horizon')), '--root', str(root), 'task', 'set',
                env['ARCHON_HORIZON_TASK'], '--status', 'done'], check=True)
print('Checked the public theorem and file layout.')
""")
    orch, member = fixture(tmp_path, monkeypatch, script)
    p = member / "M.lean"
    p.write_text(p.read_text() + "-- Horizon run 0001 checkpoint.\n")
    bp = member / "blueprint/src"
    bp.mkdir(parents=True)
    (bp / "content.tex").write_text(r"\begin{theorem}\label{t}\lean{example_ok}\leanok True.\end{theorem}")
    git(member, "add", ".").check_returncode()
    git(member, "commit", "-qm", "initial project").check_returncode()
    orch.task_store.put(HorizonTask("paper", "m", "Prove the statement", status=TaskStatus.DONE))
    monkeypatch.setattr(supervisor, "sample_resources", lambda: Resources(8, 1, 20))
    cfg = load_config(tmp_path).scheduler
    def run():
        return supervisor.supervise(orch, cfg, {"m"}, set(), rounds=1, dry_run=False)
    assert run()["exit_code"] == 3
    q, = delivery.questions(orch.workspace, "m")
    assert q["reason"] == "run history in source comments" and not q["reply"]
    assert not run()["runs"]
    assert "Horizon run" in p.read_text()
    env = {**os.environ, **session_git_environment(orch.workspace, ("m",), None)}
    p.write_text(p.read_text().replace("-- Horizon run 0001 checkpoint.\n", ""))
    assert git(member, "commit", "-m", "doc: tidy the header", "--", "M.lean", env=env).returncode
    git(member, "restore", "M.lean").check_returncode()
    box = FilesystemInboxProvider(orch.workspace.state_path / "inbox/local")
    box.add_comment(q["inbox"], "Remove the checkpoint comment.", author="human")
    (tmp_path / "repair-authorized").touch()
    outcome = run()
    assert outcome["exit_code"] == 0 and len(outcome["runs"]) == 1
    assert "Horizon run" not in p.read_text()
    assert json.loads((orch.workspace.state_path / "supervisor.json").read_text())["delivery"] == "delivered"
    assert not run()["runs"]
    assert "lean M.lean -- -o" in (tmp_path / "lake.calls").read_text()


def test_agent_hooks_check_actual_commit_and_preserve_native_hooks(tmp_path, monkeypatch):
    orch, member = fixture(tmp_path, monkeypatch)
    hooks = member / "custom-hooks"
    hooks.mkdir()
    native = hooks / "commit-msg"
    native.write_text("#!/bin/sh\ntouch native-hook-ran\n")
    native.chmod(0o755)
    git(member, "config", "core.hooksPath", "custom-hooks").check_returncode()
    env = {**os.environ, **session_git_environment(orch.workspace, ("m",), None)}
    p = member / "M.lean"
    original = p.read_text()
    for args, src in [
        (["-m", "doc: test"], "set_option synthInstance.maxHeartbeats 0 in\n"),
        (["--no-verify", "-m", "feat: test"], "set_option maxHeartbeats 1_000\n"),
        (["-m", "Bad title"], ""),
    ]:
        p.write_text(original + src + "\n")
        result = git(member, "commit", *args, "--", "M.lean", env=env)
        assert result.returncode != 0, result.stderr
    p.write_text(original + "set_option synthInstance.maxSize 256\n")
    git(member, "commit", "-m", "test: exercise the unregulated option", "--", "M.lean", env=env).check_returncode()
    assert (member / "native-hook-ran").exists()
    assert git(member, "config", "core.hooksPath").stdout.strip() == "custom-hooks"
    assert not protected_settings("M.lean", '/- /-nested-/ set_option maxHeartbeats 0 -/\n#check "set_option maxHeartbeats 0"')
    assert protected_settings("lakefile.toml", 'moreLeanArgs = ["-DmaxHeartbeats=0"]')
    assert protected_settings("lakefile.lean", 'leanOptions := #[⟨`synthInstance.maxHeartbeats, .ofNat 10⟩]')


def test_proposed_edits_and_compiler_invocations_cannot_set_heartbeats(tmp_path):
    def denied(tool, data):
        payload = {"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": data, "cwd": str(tmp_path)}
        response = hook_response(tmp_path, payload)
        return response and response["hookSpecificOutput"]["permissionDecision"] == "deny"
    (tmp_path / "T.lean").write_text("set_option maxHeartbeats 100\n")
    assert denied("Edit", {"file_path": "T.lean", "old_string": "100", "new_string": "101"})
    assert denied("Write", {"file_path": "T.lean", "content": "module\n"})
    assert denied("functions.apply_patch", "*** Begin Patch\n*** Add File: N.lean\n+set_option maxHeartbeats 0xFFFF\n*** End Patch")
    assert denied("Bash", {"command": "lake env lean -DsynthInstance.maxHeartbeats=80 T.lean"})
    assert denied("Bash", {"command": "lean --timeout=100 T.lean"})
    assert not denied("Bash", {"command": "lake env lean -DsynthInstance.maxSize=80 T.lean"})


def test_limit_is_per_file_and_only_inherited_failures_ask(tmp_path, monkeypatch):
    orch, member = fixture(tmp_path, monkeypatch)
    delivery.baseline(orch.workspace, "m")
    (member / "New.lean").write_text(HEADER)
    calls = []
    def measured(command, root, timeout=None):
        calls.append((command, timeout))
        if timeout is not None:
            return False, timeout + 0.01, "compile time exceeded"
        return True, 1.0, ""
    monkeypatch.setattr(delivery, "_run", measured)
    report = delivery.check_project(orch.workspace, "m")
    issues = {i["file"]: i for i in report["issues"]}
    assert "inbox" in issues["M.lean"] and "inbox" not in issues["New.lean"]
    assert report["limit_seconds"] == 30 and len(report["timings"]) == 2
    assert len([r for r in calls if r[1] == 30]) == 2
    for bad in (0, -1, "inf", None, True):
        with pytest.raises(ConfigError):
            ProjectConfig.from_raw("m", {"path": "m", "quality": {"max_file_seconds": bad}})
    assert ProjectConfig.from_raw("m", {"path": "m", "quality": {"max_file_seconds": 45}}).max_file_seconds == 45


def test_human_reply_reopens_semantic_review_even_if_machine_checks_pass(tmp_path, monkeypatch):
    orch, _ = fixture(tmp_path, monkeypatch)
    ws = orch.workspace
    orch.task_store.put(HorizonTask("proof", "m", "Prove the statement", status=TaskStatus.DONE))
    dag = {"m": {"nodes": [{"id": "T", "state": "closed"}]}}
    review = QualityReview(orch, {"m"}, set(), False)
    task = review.next_task(dag)
    orch.task_store.put(task)
    assert main(["--root", str(tmp_path), "task", "set", task.id, "--status", "done"]) == 0
    issue = delivery.ask(ws, "m", "M.lean", "The module docs need a summary")
    assert review.next_task(dag) is None and review.status == "awaiting user"
    box = FilesystemInboxProvider(ws.state_path / "inbox/local")
    box.add_comment(issue, "Add a summary of the main theorem.", author="human")
    continued = review.next_task(dag)
    assert continued.id == task.id and "Add a summary" in continued.objective
    orch.task_store.put(continued)
    assert main(["--root", str(tmp_path), "task", "set", continued.id, "--status", "done"]) == 0
    assert review.next_task(dag) is None and review.status == "delivered"


def test_retention_is_specific_to_an_inherited_finding_and_never_waives_timing(tmp_path, monkeypatch):
    orch, member = fixture(tmp_path, monkeypatch)
    ws = orch.workspace
    file = member / "M.lean"
    file.write_text(file.read_text() + "set_option maxHeartbeats 100\n")
    delivery.prepare_baseline(ws, "m")
    q, = delivery.questions(ws, "m")
    box = FilesystemInboxProvider(ws.state_path / "inbox/local")
    box.add_comment(q["inbox"], "keep M.lean", author="human")
    report = delivery.check_project(ws, "m")
    assert report["ok"] and [r["reason"] for r in report["retained"]] == ["protected setting maxHeartbeats=100"]
    file.write_text(file.read_text() + "set_option maxHeartbeats 100\n")
    assert not delivery.check_project(ws, "m")["ok"]
    file.write_text(file.read_text().removesuffix("set_option maxHeartbeats 100\n"))
    def measured(command, root, timeout=None):
        return (False, timeout + 0.1, "compile time exceeded") if timeout else (True, 1.0, "")
    monkeypatch.setattr(delivery, "_run", measured)
    issue, = delivery.check_project(ws, "m")["issues"]
    box.add_comment(issue["inbox"], "keep M.lean", author="human")
    assert not delivery.check_project(ws, "m")["ok"]


@pytest.mark.parametrize("action", ["status", "edit"])
def test_delivery_requires_the_reviewed_revision_of_every_member(tmp_path, monkeypatch, action):
    _, member = fixture(tmp_path, monkeypatch)
    other = tmp_path / "second"
    git(tmp_path, "clone", "-q", str(member), str(other)).check_returncode()
    config_path = tmp_path / "config.yaml"
    config = yaml.safe_load(config_path.read_text())
    config["projects"]["second"] = {"path": "second"}
    config_path.write_text(yaml.safe_dump(config))
    orch = build_orchestrator(tmp_path)
    orch.task_store.put(HorizonTask("proof", "m", "Prove the statement", status=TaskStatus.DONE))
    dag = {name: {"nodes": [{"id": "T", "state": "closed"}]} for name in ("m", "second")}
    review = QualityReview(orch, set(dag), set(), False)
    task = review.next_task(dag)
    orch.task_store.put(task)

    cli = ["--root", str(tmp_path), "task", "set", task.id, "--status", "done"]
    assert main(cli) == 0
    first_heads = {name: delivery.head(orch.workspace.project_path(name)) for name in dag}
    assert orch.task_store.get(task.id).metadata["reviewed_heads"] == first_heads
    source = other / "M.lean"
    source.write_text(source.read_text().replace("True := by trivial", "1 = 1 := rfl"))
    git(other, "-c", "user.name=test", "-c", "user.email=test@local", "commit", "-qam", "change statement").check_returncode()
    assert main(cli) == 0
    assert orch.task_store.get(task.id).metadata["reviewed_heads"] == first_heads

    resumed = QualityReview(orch, set(dag), set(), False)
    reopened = resumed.next_task(dag)
    assert reopened is not None and resumed.status == "quality review"
    assert delivery.head(other) in reopened.objective
    assert "reviewed_heads" not in reopened.metadata
    orch.task_store.put(reopened)
    dashboard = WorkspaceService(tmp_path)
    dashboard.edit_task(action, id=reopened.id, status="done")
    assert resumed.next_task(dag, measure=False) is None and resumed.status == "verifying delivery"
    assert resumed.next_task(dag) is None and resumed.status == "delivered"
    assert resumed.record["heads"] == {"m": first_heads["m"], "second": delivery.head(other)}


def test_legacy_receipt_requires_semantic_review_and_dirty_sources_cannot_be_signed_off(tmp_path, monkeypatch):
    orch, member = fixture(tmp_path, monkeypatch)
    orch.task_store.put(HorizonTask("proof", "m", "Prove the statement", status=TaskStatus.DONE))
    dag = {"m": {"nodes": [{"id": "T", "state": "closed"}]}}
    review = QualityReview(orch, {"m"}, set(), False)
    task = review.next_task(dag)
    orch.task_store.put(replace(task, status=TaskStatus.DONE))
    review.record["heads"] = {"m": delivery.head(member)}
    review.publish("delivered", "Previously verified")

    resumed = QualityReview(orch, {"m"}, set(), False)
    reopened = resumed.next_task(dag)
    assert reopened is not None and resumed.status == "quality review"
    orch.task_store.put(reopened)
    (member / "M.lean").write_text(HEADER)
    cli = ["--root", str(tmp_path), "task", "set", reopened.id, "--status", "done"]
    assert main(cli) != 0
    assert orch.task_store.get(reopened.id).status == TaskStatus.QUEUED
    git(member, "restore", "M.lean").check_returncode()
    assert main(cli) == 0
    assert resumed.next_task(dag) is None and resumed.status == "delivered"
