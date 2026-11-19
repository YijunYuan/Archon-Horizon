"""Session VCS policy: project checkpoints, workspace integration, persistent locks."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from archon_horizon.config import operations
from archon_horizon.config.loader import build_workspace, load_config
from archon_horizon.vcs.git import WorkspaceGit, git_available
from archon_horizon.vcs.integration import project_revisions
from archon_horizon.vcs.project_git import session_git_environment, finish_project_session
from archon_horizon.server.service import WorkspaceService


pytestmark = pytest.mark.skipif(not git_available(), reason="git not installed")


def _identity() -> None:
    os.environ.setdefault("GIT_AUTHOR_NAME", "test")
    os.environ.setdefault("GIT_AUTHOR_EMAIL", "test@example.com")
    os.environ.setdefault("GIT_COMMITTER_NAME", "test")
    os.environ.setdefault("GIT_COMMITTER_EMAIL", "test@example.com")


def _git(root: Path, *args: str, env=None) -> str:
    return subprocess.check_output(["git", "-C", str(root), *args], env=env, text=True).strip()


@pytest.mark.parametrize("layout", ["clone", "worktree", ".git.disabled", ".git-disabled"])
def test_project_keeps_checkout_and_staging(tmp_path: Path, layout: str) -> None:
    _identity()
    root = tmp_path / "ws"
    project = root / "projects" / "p"
    project.parent.mkdir(parents=True)
    original = tmp_path / "original"
    original.mkdir()
    _git(original, "init", "-q")
    _git(original, "commit", "--allow-empty", "-qm", "first")
    if layout == "worktree":
        _git(original, "worktree", "add", "-b", "proof", str(project))
    else:
        _git(root, "clone", "-q", str(original), str(project))
        _git(project, "checkout", "-qb", "proof")
    (project / "Notes").write_text("staged")
    _git(project, "add", "Notes")
    (project / "Notes").write_text("working")
    before = [_git(project, *args) for args in (("rev-parse", "HEAD"), ("remote", "-v"), ("status", "--porcelain"))]
    gitfile = (project / ".git").read_bytes() if layout == "worktree" else None
    if layout.startswith(".git"):
        (project / ".git").rename(project / layout)
    (root / "config.yaml").write_text("workspace: {name: ws}\nprojects: {}\n")
    operations.add_project(root, "p", "projects/p")
    workspace = build_workspace(load_config(root), root)
    assert project_revisions(workspace, ("p",), initialize=True) == {"p": before[0]}
    assert [_git(project, *args) for args in (("rev-parse", "HEAD"), ("remote", "-v"), ("status", "--porcelain"))] == before
    assert _git(project, "branch", "--show-current") == "proof"
    assert _git(project, "show", ":Notes") == "staged"
    if gitfile is not None:
        assert (project / ".git").read_bytes() == gitfile
    assert not (root / ".git").exists()
    assert not WorkspaceGit(root).is_repo()


def test_dashboard_tracks_live_and_finished_project_commits(tmp_path: Path) -> None:
    _identity()
    (tmp_path / "config.yaml").write_text("workspace: {name: ws}\nprojects: {}\n")
    for name in ("A", "B"):
        operations.add_project(tmp_path, name, name)
    service = WorkspaceService(tmp_path)
    run = service.stores.run_logs.allocate()
    session = run.new_session("horizon-T")
    session.write_meta({"status": "running", "projects": ["A", "B"]})
    env = {**os.environ, **session_git_environment(service.workspace, ("A", "B"), session.path),
           "ARCHON_HORIZON_RUN": run.id, "ARCHON_HORIZON_SESSION": session.name,
           "ARCHON_HORIZON_AGENT_ROLE": "horizon"}
    refs = {}
    for name in ("A", "B"):
        project = tmp_path / name
        (project / "证明.lean").write_text(f"def {name} := 1\n")
        (project / "private").write_text("human draft")
        _git(project, "add", "private")
        _git(project, "add", "证明.lean")
        if name == "A":
            # A normal git invocation inside the session also appears in Logs.
            _git(project, "commit", "-m", "prove A", "--", "证明.lean", env=env)
        else:
            subprocess.run([env["HORIZON_GIT"], "-C", name, "commit", "-m", "prove B", "--", "证明.lean"],
                           cwd=tmp_path, env=env, check=True)
        refs[name] = [_git(project, "rev-parse", "HEAD")]
        assert _git(project, "diff", "--cached", "--name-only") == "private"
    _git(tmp_path / "A", "commit", "--allow-empty", "-m", "human change")
    for live in (True, False):
        if not live:
            assert finish_project_session(service.workspace, session.path) == refs
            _git(tmp_path / "A", "reflog", "expire", "--expire=now", "--all")
        view = service.session_commits_view(run.id, session.name)
        assert {(c["project"], c["subject"]) for c in view["commits"]} == {("A", "prove A"), ("B", "prove B")}
        for row in view["commits"]:
            assert [f["path"] for f in row["files"]] == ["证明.lean"]
            diff = service.session_file_diff(run.id, session.name, "证明.lean", sha=row["sha"], project=row["project"])
            assert f"+def {row['project']} := 1" in diff["diff"]
    assert not WorkspaceGit(tmp_path).is_repo()


def _commit_leak(tmp_path: Path, body: str, *, env: dict | None = None):
    """Stage a source file with ``body`` and commit it through the guarded ledger git.
    Returns (CompletedProcess, committed_content)."""
    _identity()
    (tmp_path / "config.yaml").write_text("workspace: {name: ws}\n", "utf-8")
    ws_git_dir = tmp_path / ".archon-horizon" / "vcs" / "workspace.git"
    WorkspaceGit(tmp_path).init()
    # Use a normal source path: Horizon state is excluded from the ledger now.
    leak = tmp_path / "projects" / "p" / "leak.lean"
    leak.parent.mkdir(parents=True, exist_ok=True)
    leak.write_text(body, "utf-8")
    base = ["git", "--git-dir", str(ws_git_dir), "--work-tree", str(tmp_path)]
    subprocess.run(base + ["add", "--", "projects/p/leak.lean"], cwd=tmp_path, check=True)
    proc = subprocess.run(base + ["commit", "-m", "x"], cwd=tmp_path,
                          capture_output=True, text=True, env=env)
    committed = subprocess.run(base + ["show", "HEAD:projects/p/leak.lean"],
                               cwd=tmp_path, capture_output=True, text=True).stdout
    return proc, committed


def test_secret_guard_redacts_and_never_blocks(tmp_path: Path) -> None:
    """A credential is REDACTED to XXXX in the committed (and working-tree) content
    and the commit still succeeds — the guard never blocks."""
    secret = "ghp_AbCdEfGhIjKlMnOpQrStUvWxYz012345"
    proc, committed = _commit_leak(tmp_path, f'token = "{secret}"\n')
    assert proc.returncode == 0                     # never blocked
    assert "redacted" in proc.stderr.lower()
    assert secret not in committed and "XXXX" in committed
    # the working-tree file is redacted too, so the secret does not linger
    assert secret not in (tmp_path / "projects" / "p" / "leak.lean").read_text("utf-8")


def test_secret_guard_override_preserves_content(tmp_path: Path) -> None:
    """ARCHON_HORIZON_ALLOW_SECRETS=1 skips the scan: content is committed verbatim."""
    import os as _os

    secret = "ghp_AbCdEfGhIjKlMnOpQrStUvWxYz012345"
    proc, committed = _commit_leak(
        tmp_path, f'token = "{secret}"\n',
        env={**_os.environ, "ARCHON_HORIZON_ALLOW_SECRETS": "1"},
    )
    assert proc.returncode == 0
    assert secret in committed  # not redacted when explicitly allowed


def test_secret_guard_no_false_positive_case_sensitive(tmp_path: Path) -> None:
    """A lowercase camelCase identifier that a case-insensitive scan would flag
    (AKIA…) must be left untouched now that the scan is case-sensitive (I-0238)."""
    ident = "def akiaLongCamelCaseIdentifier01 := 1"
    proc, committed = _commit_leak(tmp_path, ident + "\n")
    assert proc.returncode == 0
    assert "XXXX" not in committed
    assert ident in committed


