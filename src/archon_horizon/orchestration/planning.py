"""Derive runnable assignments from the current roadmap, tasks and Lean-backed DAGs."""

from __future__ import annotations

import hashlib
from collections import defaultdict
from dataclasses import replace

from archon_horizon.core.freeze import frozen_violations
from archon_horizon.core.roadmap import RoadmapStatus, item_parent
from archon_horizon.core.scope import ItemScope
from archon_horizon.core.tasks import HorizonTask, TaskStatus, WriteSet
from archon_horizon.core.workspace import Workspace

from .orchestrator import Orchestrator
from .scheduler import priority_rank


def _derived_id(project: str, stage: str, target: str) -> str:
    key = hashlib.sha256(f"{project}\0{stage}\0{target}".encode()).hexdigest()[:20]
    return f"proof-{key}"


def _proof_tasks(workspace: Workspace, project: str, dag: dict | None, in_progress: set[str]):
    blocked = {issue["node"] for issue in (dag or {}).get("dangling", ())}
    blocked.update(in_progress)
    children = defaultdict(list)
    for edge in (dag or {}).get("edges", ()):
        children[edge["source"]].append(edge["target"])
    pending = list(blocked)
    while pending:
        for child in children[pending.pop()]:
            if child not in blocked:
                blocked.add(child)
                pending.append(child)

    nodes = (dag or {}).get("nodes", ())
    for node in nodes or [None]:
        label = node["id"] if node else ""
        if node and (node["state"] != "ready" or label in blocked):
            continue
        certify = node and node.get("lean_status") == "linked"
        phase = "orientation" if not node else "certification" if certify else "blueprint"
        files = tuple(str(workspace.project(project).path / f) for f in node.get("lean_files", ())) if node and not certify else ()
        declarations = tuple(d.strip() for d in (node.get("lean_name") or "").split(",") if d.strip()) if node else ()
        labels = (label,) if node else ()
        targets = dict(projects=(project,), files=files, declarations=declarations, blueprint_nodes=labels)
        if certify:
            title = f"Verify blueprint correspondence for {label}"
            objective = (f"Check the Lean declaration(s) for {label} against its blueprint statement and proof, "
                         "including dependencies and absence of sorry. Run the project checks. Update the blueprint "
                         "completion marker only if justified; otherwise document the gap or blocker.")
        elif node:
            title = f"Formalize {label}"
            objective = f"Prove and check the blueprint statement {label} in {project}.\n{node.get('statement') or ''}"
        else:
            title = f"Establish the blueprint for {project}"
            objective = ("Read the assigned objectives and project sources. Write a source-facing blueprint "
                         "with labelled statements, proofs and dependencies. Prepare independent proof "
                         "assignments for the scheduler: give them separate Lean modules and file-scoped "
                         "roadmap items (`horizon roadmap add --file … --blueprint-node … --depends-on …`) or link "
                         "their declarations in the blueprint. Include required "
                         "interfaces and list dependencies. Leave substantial proofs to the assigned workers.")
        yield HorizonTask(id=_derived_id(project, phase, label), project=project, projects=(project,), title=title,
                          objective=objective, write_set=WriteSet(**targets), scope=ItemScope(**targets),
                          metadata={"supervisor_source": phase,
                                    "unlocks": node.get("unlocks", 0) if node else 0})


def _file_plan(project: str, proofs: list[HorizonTask]) -> HorizonTask:
    labels = sorted(label for t in proofs for label in t.write_set.blueprint_nodes)
    objective = (
        "The following independent blueprint targets have no assigned Lean files: "
        + ", ".join(labels) + ". Inspect the project layout and record file-scoped roadmap "
        "items using --file and --blueprint-node. Group statements that belong in the same "
        "module and record any dependencies. Leave the proofs to the assigned workers."
    )
    return HorizonTask(id=_derived_id(project, "plan", "\n".join(labels)), project=project, projects=(project,),
                       title=f"Assign modules to ready proofs in {project}", objective=objective,
                       write_set=WriteSet(projects=(project,)), scope=ItemScope(projects=(project,)),
                       metadata={"supervisor_source": "plan", "unlocks": len(labels)})


def assignments(orch: Orchestrator, projects: set[str], ids: set[str], dags: dict, *, create: bool = True, active=()):
    """Return proposed tasks. Nothing is stored until the controller admits a run."""
    tasks = {t.id: t for t in orch.task_store.list()}
    roadmap = {r.id: r for r in orch.roadmap_store.load().items}
    # An explicitly selected milestone includes its subtree and prerequisites.
    # Recompute it on refresh because the planning worker can add new items.
    wanted = set(ids)
    children = defaultdict(list)
    for item in roadmap.values():
        children[item_parent(item)].append(item.id)
    pending = list(ids.intersection(roadmap))
    visited = set()
    while pending:
        item = roadmap[pending.pop()]
        if item.id in visited:
            continue
        visited.add(item.id)
        for ref in (*children[item.id], *item.depends_on):
            if ref in roadmap and ref not in visited:
                wanted.add(ref)
                pending.append(ref)
        wanted.update(item.task_refs)
    unfinished_parents = {item_parent(r) for r in roadmap.values() if r.status not in (RoadmapStatus.DONE, RoadmapStatus.REJECTED)}
    working_refs = {ref for task in active for ref in task.roadmap_refs}
    ready_items = {r.id for r in roadmap.values() if r.status in (RoadmapStatus.ACTIVE, RoadmapStatus.PENDING)
                   and r.id not in unfinished_parents
                   and all(d in roadmap and roadmap[d].status is RoadmapStatus.DONE and d not in working_refs
                           for d in r.depends_on)}
    proposals = [t for t in tasks.values() if not t.metadata.get("supervisor_source")]
    # A broad user objective supplies the goal for discovery and the final integration.
    # A file-scoped request can go directly to a worker.
    goals = defaultdict(list)
    for task in proposals:
        w = task.write_set
        if (task.status is TaskStatus.QUEUED and task.project in dags
                and len(task.projects or (task.project,)) == 1 and not task.roadmap_refs
                and not (w.files or w.declarations or w.blueprint_nodes or w.workspace)
                and (task.project in projects or task.id in ids)):
            goals[task.project].append(task)
    def has_frontier(project):
        dag = dags.get(project)
        return not dag or not dag.get("nodes") or any(n["state"] != "closed" for n in dag["nodes"])
    if create:
        deferred = {g.id for p, group in goals.items() if has_frontier(p) for g in group}
        proposals = [t for t in proposals if t.id not in deferred]
    linked = {ref for t in tasks.values() for ref in t.roadmap_refs}
    if create:
        proposals.extend(orch.roadmap_task(r) for r in roadmap.values()
                         if r.id in ready_items and r.projects and r.id not in tasks and r.id not in linked
                         and not set(r.task_refs).intersection(tasks))
    owned = {(p, label) for task in tasks.values()
             if not task.metadata.get("supervisor_source") and task.status is not TaskStatus.DONE
             for p in (task.projects or (task.project,)) for label in task.write_set.blueprint_nodes}
    # A roadmap block or unmet dependency must also reserve its labels.
    # Otherwise discovery would dispatch the same proof under a generated ID.
    for item in roadmap.values():
        if item.status not in (RoadmapStatus.DONE, RoadmapStatus.REJECTED):
            labels = orch.roadmap_task(item).write_set.blueprint_nodes if item.projects else ()
            owned.update((p, label) for p in item.projects for label in labels)
    in_progress = defaultdict(set)
    for task in (*active, *(t for t in tasks.values() if t.status is TaskStatus.RUNNING)):
        for project in task.projects or (task.project,):
            in_progress[project].update(task.write_set.blueprint_nodes)
    for project, dag in dags.items():
        proofs = [p for p in _proof_tasks(orch.workspace, project, dag, in_progress[project])
                  if not any((project, label) in owned for label in p.write_set.blueprint_nodes)
                  and not frozen_violations(p.write_set, orch.freeze)]
        unmapped = [p for p in proofs if p.metadata["supervisor_source"] == "blueprint" and not p.write_set.files]
        if len(unmapped) > 1 and create:
            plan = _file_plan(project, unmapped)
            previous = tasks.get(plan.id)
            if previous is None or previous.status is TaskStatus.QUEUED:
                unmapped_ids = {p.id for p in unmapped}
                proofs = [p for p in proofs if p.id not in unmapped_ids] + [plan]
        for proof in proofs:
            if goals.get(project):
                objectives = "\n".join(f"{g.id}: {g.objective or g.title}" for g in goals[project])
                proof = replace(proof, objective=f"{proof.objective}\n\nProject objectives:\n{objectives}")
            old = tasks.get(proof.id)
            if old and old.metadata.get("supervisor_source") and old.status is TaskStatus.QUEUED:
                # Refresh scope and statement when the blueprint moves.
                proposals.append(replace(proof, created_at=old.created_at, metadata={**old.metadata, **proof.metadata}))
            elif old is None and create:
                proposals.append(proof)

    if orch.freeze.agent_rule("horizon"):
        return []
    result = []
    for t in proposals:
        names = set(t.projects or (t.project,))
        if t.status is not TaskStatus.QUEUED or not names.issubset(orch.workspace.projects):
            continue
        direct = t.id in wanted or wanted.intersection(t.roadmap_refs) or names.issubset(projects)
        child = (t.metadata.get("supervisor_source") or t.metadata.get("from_roadmap")) and names.issubset(goals)
        if not (direct or child):
            continue
        if any(ref in roadmap and ref not in ready_items for ref in t.roadmap_refs):
            continue
        if any(orch.freeze.project_rule(p) for p in names) or frozen_violations(t.write_set, orch.freeze):
            continue
        result.append(t)
    return sorted(result, key=lambda t: (priority_rank(t), not bool(t.write_set.files),
                                          bool(t.metadata.get("supervisor_source")),
                                          -int(t.metadata.get("unlocks", 0)), t.id))
