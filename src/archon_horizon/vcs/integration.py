"""Record project revisions at run boundaries without staging working trees."""

from __future__ import annotations

from archon_horizon.core.workspace import Workspace
from .git import WorkspaceGit, ensure_project_git, git_available, user_repo_git_dir


def project_revisions(
    workspace: Workspace, projects: tuple[str, ...], *, initialize: bool = False,
) -> dict[str, str | None]:
    revisions = {}
    if git_available():
        for name in dict.fromkeys(projects):
            if name not in workspace.projects:
                continue
            root = workspace.project_path(name)
            if initialize:
                ensure_project_git(root)
            native = user_repo_git_dir(root)
            if native:
                revisions[name] = WorkspaceGit(root, native).current_sha()
    return revisions
