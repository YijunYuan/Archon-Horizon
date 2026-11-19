"""Process-owned advisory locks for operations on shared workspace files."""

from __future__ import annotations

import hashlib
from contextlib import ExitStack, contextmanager
from pathlib import Path

from .tasks import HorizonTask, WriteSet
from .workspace import Workspace


@contextmanager
def workspace_lock(state: Path, key: str, *, shared: bool = False, wait: bool = True):
    import fcntl

    directory = state / "locks"
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / hashlib.sha256(key.encode()).hexdigest()).open("a") as handle:
        flags = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
        try:
            fcntl.flock(handle, flags if wait else flags | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
        else:
            yield True


def task_claim(workspace: Workspace, task: HorizonTask) -> WriteSet:
    """Canonical checkout and file identities shared by planning and dispatch."""
    claim = task.write_set
    names = set(claim.projects) | set(task.projects) | ({task.project} if task.project else set())
    if claim.workspace or not names or not names.issubset(workspace.projects):
        return WriteSet(workspace=True)
    roots = sorted({workspace.project_path(n).resolve() for n in names})
    files = []
    member_roots = {workspace.project_path(n).resolve() for n in workspace.projects}
    for raw in claim.files:
        path = (workspace.root / raw).resolve()
        if not any(path.is_relative_to(root) for root in roots):
            if len(roots) != 1 or any(path.is_relative_to(member) for member in member_roots):
                return WriteSet(workspace=True)
            path = (roots[0] / raw).resolve()
        if not any(path.is_relative_to(root) for root in roots):
            return WriteSet(workspace=True)
        if any(c in raw for c in "*?[") or path.is_dir():
            files = []
            break
        files.append(str(path))
    if any(not any(Path(f).is_relative_to(root) for f in files) for root in roots):
        files = []
    return WriteSet(projects=tuple(map(str, roots)), files=tuple(files),
                    declarations=claim.declarations, blueprint_nodes=claim.blueprint_nodes)


@contextmanager
def task_lease(workspace: Workspace, task: HorizonTask):
    claim = task_claim(workspace, task)
    locks = [("workspace", not claim.workspace), (f"task:{task.id}", False)]
    locks.extend((f"project:{root}", bool(claim.files)) for root in claim.projects)
    locks.extend((f"file:{path}", False) for path in sorted(set(claim.files)))
    for project in claim.projects:
        locks.extend((f"node:{project}:{node}", False) for node in set(claim.blueprint_nodes))
        locks.extend((f"decl:{project}:{decl}", False) for decl in set(claim.declarations))
    with ExitStack() as stack:
        acquired = all(stack.enter_context(workspace_lock(workspace.state_path, key, shared=shared, wait=False))
                       for key, shared in locks)
        yield acquired
