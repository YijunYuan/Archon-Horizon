"""Final delivery state for a set of member projects, persisted independently of proof tasks."""
from __future__ import annotations

import hashlib
import json

from archon_horizon.core.locks import task_lease
from archon_horizon.core.scope import ItemScope
from archon_horizon.core.tasks import HorizonTask, TaskStatus, WriteSet
from archon_horizon.lean.delivery import check_project, dirty_sources, head, mark_delivered, prepare_baseline, questions, read, save


def _digest(data) -> str:
    return hashlib.sha256(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()[:24]


class QualityReview:
    def __init__(self, orch, projects, ids, preview):
        self.orch = orch
        tasks = orch.task_store.list()
        roadmap = orch.roadmap_store.load().items
        names = set(projects)
        for item in (*tasks, *roadmap):
            if item.id in ids:
                names.update(item.projects or (getattr(item, "project", ""),))
        self.names = sorted(n for n in names if n in orch.workspace.projects and orch.workspace.project(n).type == "lean")
        self.path = orch.workspace.state_path / "quality" / "reviews" / (_digest(self.names) + ".json")
        self.record = read(self.path)
        self.preview = preview
        self.status = ""
        self.detail = ""
        if not preview:
            for name in self.names:
                changed = (any(t.status is TaskStatus.QUEUED and name in (t.projects or (t.project,)) for t in tasks)
                           or self.record.get("heads", {}).get(name) != head(orch.workspace.project_path(name)))
                prepare_baseline(orch.workspace, name, begin=changed)

    def publish(self, status, detail):
        self.status, self.detail = status, detail
        if not self.preview:
            self.record.update(status=status, detail=detail, projects=self.names)
            save(self.path, self.record)

    @property
    def reviewing(self):
        return self.record.get("status") in ("quality review", "awaiting user", "verifying delivery")

    def next_task(self, dags, *, measure=True):
        """Called only at a quiescent frontier, after the last worker and graph refresh."""
        if not self.names:
            return None
        ws = self.orch.workspace
        tasks = [t for t in self.orch.task_store.list() if t.metadata.get("supervisor_source") != "quality"
                 and set(self.names).intersection(t.projects or (t.project,))]
        items = [i for i in self.orch.roadmap_store.load().items if set(self.names).intersection(i.projects)]
        nodes = [node for n in self.names for node in (dags.get(n) or {}).get("nodes", [])]
        if not (tasks or items or nodes):
            return None
        # Proof workers may close a blueprint node without editing its derived task.
        # A queued human objective, a blocked milestone, or an unclosed node still counts.
        closed = {name: {n["id"] for n in (dags.get(name) or {}).get("nodes", []) if n["state"] == "closed"}
                  for name in self.names}
        unfinished = []
        for task in tasks:
            if task.status in (TaskStatus.DONE, TaskStatus.CANCELLED):
                continue
            labels = set(task.write_set.blueprint_nodes)
            if (task.status == TaskStatus.QUEUED and task.metadata.get("supervisor_source") == "blueprint"
                    and labels and labels.issubset(closed.get(task.project, set()))):
                continue
            unfinished.append(task.id)
        unfinished.extend(i.id for i in items if i.status.value not in ("done", "rejected"))
        graph_issues = [n["id"] for n in nodes if n["state"] != "closed"]
        graph_issues.extend(name for name in self.names if name not in dags or (dags[name] or {}).get("dangling"))
        if self.reviewing:
            for name, labels in self.record.get("blueprint", {}).items():
                graph_issues.extend(f"{name}: restore {label}" for label in sorted(set(labels) - closed.get(name, set())))
        else:
            unfinished.extend(graph_issues)
        if unfinished:
            self.publish("proofs pending", ", ".join(unfinished[:15]))
            return None
        if self.orch.freeze.agent_rule("horizon") or self.orch.freeze.workspace_rule() or any(
                self.orch.freeze.project_rule(n) for n in self.names):
            self.publish("proofs pending", "Review scope is frozen")
            return None
        heads = {n: head(ws.project_path(n)) for n in self.names}
        limits = {n: ws.project(n).max_file_seconds for n in self.names}
        expected = self.record.get("blueprint") if self.reviewing else None
        labels = sorted(label for group in expected.values() for label in group) if expected is not None else sorted(n["id"] for n in nodes)
        signature = _digest((sorted((t.id, t.objective, t.updated_at) for t in tasks),
                             sorted((i.id, i.summary) for i in items), labels, limits))
        same = self.record.get("obligations") == signature
        if same and self.record.get("status") == "delivered" and self.record.get("heads") == heads:
            # A changed uncommitted proof must never inherit the prior delivery.
            if not any(dirty_sources(ws.project_path(n)) for n in self.names):
                self.publish("delivered", "Verified revisions " + ", ".join(f"{n} {h[:12]}" for n, h in heads.items()))
                return None
        existing = self.record.get("task") if same and self.record.get("status") != "delivered" else None
        task_id = existing or "review-" + _digest((self.names, signature, heads))
        try:
            previous = self.orch.task_store.get(task_id)
        except FileNotFoundError:
            previous = None
        reports = self.record.get("checks", {}) if existing else {}
        has_review = previous is not None and bool(existing)
        if not existing:
            self.record["blueprint"] = {name: sorted(labels) for name, labels in closed.items()}
        if measure and not self.preview and (has_review or not reports):
            guard = HorizonTask(task_id, self.names[0], "Verify member revisions", projects=tuple(self.names))
            with task_lease(ws, guard) as acquired:
                if not acquired:
                    self.publish("proofs pending", "Another run holds the delivery scope")
                    return None
                reports = {n: check_project(ws, n, final=True) for n in self.names}
            self.record["checks"] = reports
        all_questions = {n: questions(ws, n) for n in self.names}
        reply_version = _digest(sorted({(q["inbox"], q["reply"]) for qs in all_questions.values()
                                        for q in qs if q["reply"]}))
        pending = [q["inbox"] for n, qs in all_questions.items() for q in qs if not q["reply"] and
                   (not q.get("automatic") or any(i.get("inbox") == q["inbox"] for i in reports.get(n, {}).get("issues", [])))]
        if has_review and pending and (measure or self.preview):
            self.publish("awaiting user", "Reply in the Inbox: " + ", ".join(dict.fromkeys(pending)))
            return None
        if (has_review and measure and not self.preview and previous.status is TaskStatus.DONE
                and previous.metadata.get("quality_replies") == reply_version
                and not graph_issues and reports
                and all(r["ok"] and r["revision"] == head(ws.project_path(n)) for n, r in reports.items())):
            self.record["heads"] = {n: r["revision"] for n, r in reports.items()}
            for name, revision in self.record["heads"].items():
                mark_delivered(ws, name, revision)
            count = sum(len(r.get("retained", [])) for r in reports.values())
            detail = "All members passed the final quality review"
            if count:
                detail += f" with {count} explicitly retained legacy findings"
            self.publish("delivered", detail)
            return None

        feedback = [f"{n}/{i['file']}: {i['reason']}" for n, r in reports.items() for i in r["issues"]]
        feedback.extend(graph_issues)
        replies = [f"{n}/{q['file']}: {q['reason']} — human reply: {q['reply']}" for n, qs in all_questions.items()
                   for q in qs if q["reply"]]
        objectives = [f"{t.id}: {t.objective}" for t in tasks if not t.metadata.get("supervisor_source")
                      and not t.metadata.get("from_roadmap") and t.status != TaskStatus.CANCELLED]
        objective = ("Review and repair the whole completed formalization in the listed members. Read the original "
                     "sources, blueprint and exported Lean APIs independently. Follow the final quality review "
                     "instructions. Run `horizon quality check -p PROJECT` before editing to identify inherited "
                     "issues. Ask about other pre-existing issues using `horizon quality ask -p PROJECT --file "
                     "PATH --reason TEXT`. Wait for human direction before changing those issues. Repair new "
                     "issues yourself, update consumers, build, commit and rerun the checks. Mark this task done "
                     "only after completing the semantic review and all checks.\n\nOriginal objectives:\n"
                     + "\n".join(objectives) + "\n\n" + "\n".join(feedback + replies))
        data = {"supervisor_source": "quality"}
        if existing and previous:
            data.update(previous.metadata)
            run_id = self.record.get("run")
            if run_id:
                sessions = self.orch.run_logs.get(run_id).sessions()
                for session in reversed(sessions):
                    meta = session.read_meta()
                    if meta.get("task_id") != task_id:
                        continue
                    if meta.get("engine_session_id"):
                        data["quality_session_id"] = meta["engine_session_id"]
                    report = session.path / "report.md"
                    if report.is_file():
                        objective += f"\nYour previous quality review report: {report}"
                    break
        data["quality_replies"] = reply_version
        task = HorizonTask(task_id, self.names[0], objective, title="Final Lean quality review and repair",
                           projects=tuple(self.names), write_set=WriteSet(projects=tuple(self.names)),
                           scope=ItemScope(projects=tuple(self.names)), metadata=data)
        self.record.update(task=task.id, obligations=signature)
        self.publish("quality review", "; ".join(feedback[:4]) or "Proofs complete; independent review")
        return task
