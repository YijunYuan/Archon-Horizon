"""Per-session change summaries: deterministic diff + sorry delta from the ledger."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from archon_horizon.core.workspace import Project, Workspace
from archon_horizon.server.changes_api import session_change_summary
from archon_horizon.vcs.git import WorkspaceGit, git_available

pytestmark = pytest.mark.skipif(not git_available(), reason="git not installed")


def _identity() -> None:
    os.environ.setdefault("GIT_AUTHOR_NAME", "test")
    os.environ.setdefault("GIT_AUTHOR_EMAIL", "test@example.com")
    os.environ.setdefault("GIT_COMMITTER_NAME", "test")
    os.environ.setdefault("GIT_COMMITTER_EMAIL", "test@example.com")


def _workspace(tmp_path: Path) -> Workspace:
    (tmp_path / "config.yaml").write_text("workspace: {name: ws}\n", "utf-8")
    return Workspace(name="ws", root=tmp_path, projects={"p": Project(name="p", path=Path("projects/p"))})


def _snapshot(ws: Workspace) -> str:
    # Historical workspaces stored a session snapshot in this out-of-tree repo.
    ledger = WorkspaceGit(ws.root)
    ledger.init()
    sha = ledger.commit("session snapshot", paths=["projects/p"])
    assert sha
    return sha


def test_session_change_summary_reports_sorry_delta(tmp_path: Path) -> None:
    _identity()
    ws = _workspace(tmp_path)
    proj = tmp_path / "projects" / "p"
    proj.mkdir(parents=True)
    lean = proj / "Foo.lean"

    # Session 1: introduce a file with two sorries.
    lean.write_text("theorem a : True := by sorry\ntheorem b : True := by sorry\n", "utf-8")
    s1 = _snapshot(ws)

    # Session 2: discharge one sorry.
    lean.write_text("theorem a : True := trivial\ntheorem b : True := by sorry\n", "utf-8")
    s2 = _snapshot(ws)

    projpath = (proj.relative_to(tmp_path)).as_posix()
    # Diff session 2 against session 1's commit (the previous session), as the
    # service does — not the raw git parent.
    summary = session_change_summary(tmp_path, s2, (projpath,), base=s1)

    assert summary["available"] is True
    assert summary["sorry_delta"] == -1
    assert summary["lean"]["sorry_delta"] == -1
    row = next(r for r in summary["files"] if r["path"].endswith("Foo.lean"))
    assert row["category"] == "lean"
    assert row["sorry_before"] == 2 and row["sorry_after"] == 1 and row["sorry_delta"] == -1
    assert row["loc_before"] == 2 and row["loc_after"] == 2  # two theorem lines
    assert row["decl_after"] == {"theorem": 2}
    assert summary["lean"]["decl_after"] == {"theorem": 2}
    # The run's first session (base=None) sees the file as newly added, 2 sorries.
    first = session_change_summary(tmp_path, s1, (projpath,), base=None)
    assert first["available"] and first["sorry_delta"] == 2
    assert next(r for r in first["files"] if r["path"].endswith("Foo.lean"))["added"] is True


def test_session_change_summary_reports_declaration_deltas(tmp_path: Path) -> None:
    _identity()
    ws = _workspace(tmp_path)
    proj = tmp_path / "projects" / "p"
    proj.mkdir(parents=True)
    lean = proj / "Foo.lean"
    tex = proj / "blueprint" / "chapter.tex"
    tex.parent.mkdir()
    projpath = proj.relative_to(tmp_path).as_posix()

    lean.write_text("def f : Nat := 0\ntheorem a : True := trivial\n", "utf-8")
    tex.write_text("\\begin{theorem}\\label{a} A.\\end{theorem}\n", "utf-8")
    s1 = _snapshot(ws)

    lean.write_text(
        "def f : Nat := 0\n"
        "theorem a : True := trivial\n"
        "lemma b : True := trivial\n",
        "utf-8",
    )
    tex.write_text(
        "\\begin{theorem}\\label{a} A.\\end{theorem}\n"
        "\\begin{lemma}\\label{b} B.\\end{lemma}\n"
        "\\begin{definition}\\label{c} C.\\end{definition}\n",
        "utf-8",
    )
    s2 = _snapshot(ws)

    summary = session_change_summary(tmp_path, s2, (projpath,), base=s1)
    lean_row = next(r for r in summary["files"] if r["path"].endswith("Foo.lean"))
    tex_row = next(r for r in summary["files"] if r["path"].endswith("chapter.tex"))

    assert lean_row["decl_delta"] == {"lemma": 1}
    assert summary["lean"]["decl_delta"] == {"lemma": 1}
    assert tex_row["decl_delta"] == {"definition": 1, "lemma": 1}
    assert summary["blueprint"]["decl_delta"] == {"definition": 1, "lemma": 1}


def test_first_session_falls_back_to_git_parent(tmp_path: Path) -> None:
    # With a run-start system commit, the first agent session has no explicit
    # previous-session base, but its git parent (the system snapshot) is used —
    # so it shows a real diff, not "new file" on everything.
    _identity()
    ws = _workspace(tmp_path)
    proj = tmp_path / "projects" / "p"
    proj.mkdir(parents=True)
    lean = proj / "Foo.lean"
    projpath = proj.relative_to(tmp_path).as_posix()

    # System snapshot at run start (2 sorries), then the first agent session (1).
    lean.write_text("theorem a : True := by sorry\ntheorem b : True := by sorry\n", "utf-8")
    _snapshot(ws)
    lean.write_text("theorem a : True := trivial\ntheorem b : True := by sorry\n", "utf-8")
    s2 = _snapshot(ws)

    # base=None → falls back to the parent (the system snapshot): −1 sorry, not initial.
    summary = session_change_summary(tmp_path, s2, (projpath,), base=None)
    assert summary["available"] and summary["initial"] is False
    assert summary["sorry_delta"] == -1


def test_diffs_against_git_parent_not_far_same_run_ancestor(tmp_path: Path) -> None:
    # The shared ledger interleaves runs: a session's commit parent may belong to
    # another run. The change summary (base=None) must diff against that git
    # parent — what this commit actually changed — not against a far same-run
    # ancestor, which would report every interleaved file.
    _identity()
    ws = _workspace(tmp_path)
    proj = tmp_path / "projects" / "p"
    proj.mkdir(parents=True)
    projpath = proj.relative_to(tmp_path).as_posix()

    # Commit 1 (run A): Foo exists.
    (proj / "Foo.lean").write_text("theorem a : True := by sorry\n", "utf-8")
    _snapshot(ws)
    # Commit 2 (another run B) lands in between, adding many unrelated files.
    for i in range(5):
        (proj / f"Other{i}.lean").write_text("def x := 0\n" * 50, "utf-8")
    _snapshot(ws)
    # Commit 3 (run A again): only touches Foo. Its git parent is B's commit.
    (proj / "Foo.lean").write_text("theorem a : True := trivial\n", "utf-8")
    s3 = _snapshot(ws)

    summary = session_change_summary(tmp_path, s3, (projpath,), base=None)
    changed = {r["path"] for r in summary["files"]}
    # Only Foo.lean — the 5 Other*.lean from run B are in the parent, not this diff.
    assert changed == {f"{projpath}/Foo.lean"}
    assert summary["sorry_delta"] == -1
