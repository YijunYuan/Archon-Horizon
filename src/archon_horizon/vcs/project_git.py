"""Route session Git commands to registered checkouts without changing their configuration."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

from archon_horizon.core.workspace import Workspace
from archon_horizon.vcs.git import WorkspaceGit, install_ledger_git_wrapper, user_repo_git_dir


def session_git_environment(
    workspace: Workspace, projects: tuple[str, ...], session_dir: Path | None,
) -> dict[str, str]:
    env = {
        "HORIZON_PROJECT_ROOTS": "[]",
        "HORIZON_GIT_SESSION_ID": "",
    }
    wrapper = install_ledger_git_wrapper(workspace.state_path)
    if wrapper is not None:
        env["HORIZON_GIT"] = str(wrapper.resolve())
    members = [name for name in projects or workspace.projects if name in workspace.projects]
    env["HORIZON_PROJECT_ROOTS"] = json.dumps([str(workspace.project_path(n).resolve()) for n in members])
    if session_dir is not None:
        # Numeric run/session names can also exist in a different workspace's clone.
        key = uuid.uuid4().hex
        session_dir.mkdir(parents=True, exist_ok=True)
        (session_dir / "project-git.json").write_text(json.dumps({
            "id": key, "projects": members,
        }), encoding="utf-8")
        env["HORIZON_GIT_SESSION_ID"] = key
        env["GIT_REFLOG_ACTION"] = f"horizon-{key}"
    return env


def project_session_commits(
    workspace: Workspace, session_dir: Path,
) -> dict[str, list[str]]:
    """Exact commit refs by project, usable while an agent is still running."""
    try:
        manifest = json.loads((session_dir / "project-git.json").read_text("utf-8"))
    except (FileNotFoundError, ValueError):
        return {}
    if "commits" in manifest:
        return manifest["commits"]
    key = manifest["id"]
    result = {}
    for name in manifest["projects"]:
        if name not in workspace.projects:
            continue
        root = workspace.project_path(name)
        dot = user_repo_git_dir(root)
        if dot is None:
            continue
        repo = WorkspaceGit(root, dot)
        refs = repo.session_commit_refs(key)
        if refs:
            result[name] = refs
    return result


def finish_project_session(
    workspace: Workspace, session_dir: Path,
) -> dict[str, list[str]]:
    path = session_dir / "project-git.json"
    commits = project_session_commits(workspace, session_dir)
    if path.exists():
        manifest = json.loads(path.read_text("utf-8"))
        manifest["commits"] = commits
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(manifest), "utf-8")
        tmp.replace(path)
    return commits


def main() -> None:
    args = sys.argv[1:]
    # Honor the normal Git convention for selecting a project from the
    # workspace root, including repeated -C options and project subdirectories.
    while len(args) >= 2 and args[0] == "-C":
        if args[1]:
            os.chdir(args[1])
        args = args[2:]
    cwd = Path.cwd()
    roots = {Path(path) for path in json.loads(os.environ["HORIZON_PROJECT_ROOTS"])}
    project = next((path for path in (cwd, *cwd.parents) if path in roots), None)
    if project is None:
        sys.exit("Run $HORIZON_GIT -C <project-directory> to select a member repository.")
    dot = user_repo_git_dir(project)
    if dot is None:
        sys.exit(f"No project Git repository at {project}")
    if args[:1] == ["commit"]:
        # Insert before any '-- <paths>'. Native Git retains the project's
        # hooks, signing policy, remotes, worktree index, and author config.
        trailers = {
            "Archon-Run": os.environ.get("ARCHON_HORIZON_RUN"),
            "Archon-Session": os.environ.get("ARCHON_HORIZON_SESSION"),
            "Archon-Session-Id": os.environ.get("HORIZON_GIT_SESSION_ID"),
            "Archon-Role": os.environ.get("ARCHON_HORIZON_AGENT_ROLE"),
            "Archon-Task": os.environ.get("ARCHON_HORIZON_TASK"),
            "Archon-Round": os.environ.get("ARCHON_HORIZON_ROUND"),
            "Archon-Projects": os.environ.get("ARCHON_HORIZON_PROJECTS"),
            "Archon-Commit": "agent",
        }
        for role in ("AUTHOR", "COMMITTER"):
            identity = subprocess.run(["git", "var", f"GIT_{role}_IDENT"], capture_output=True)
            if identity.returncode:
                os.environ.setdefault(f"GIT_{role}_NAME", "Archon Horizon")
                os.environ.setdefault(f"GIT_{role}_EMAIL", "archon-horizon@local")
        args[1:1] = [f"--trailer={key}={value}" for key, value in trailers.items() if value]
    os.execvp("git", ["git", f"--git-dir={dot}", f"--work-tree={project}", *args])


if __name__ == "__main__":
    main()
