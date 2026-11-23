"""Persistent baselines, legacy questions and reproducible member-level checks."""
from __future__ import annotations

import hashlib
import json
import os
import shlex
import subprocess
import tempfile
from dataclasses import replace
from pathlib import Path

from archon_horizon.core.atomic import write_text_atomic
from archon_horizon.core.inbox import InboxDraft, InboxKind, InboxScope
from archon_horizon.core.locks import workspace_lock
from archon_horizon.core.tasks import HorizonTask, TaskStatus
from archon_horizon.core.workspace import Workspace
from archon_horizon.inboxes.filesystem import FilesystemInboxProvider

from .benchmark import iter_lean_files
from .process import run_lean_command as _run
from .quality import source_findings
from .signatures import SignatureCheckError, may_have_setup, signature_findings


def directory(ws: Workspace, name: str) -> Path:
    key = hashlib.sha256(os.fsencode(ws.project_path(name).resolve())).hexdigest()[:24]
    return ws.state_path / "quality" / "projects" / key


def save(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomic(path, json.dumps(data, ensure_ascii=False, indent=2))


def read(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return {}


def head(root: Path) -> str:
    if not (root / ".git").exists():
        return ""
    p = subprocess.run(["git", "-C", str(root), "rev-parse", "--verify", "HEAD"],
                       capture_output=True, text=True)
    return p.stdout.strip() if p.returncode == 0 else ""


def sources(root: Path) -> list[Path]:
    if (root / ".git").exists():
        names = subprocess.check_output(["git", "-C", str(root), "ls-files", "-co", "--exclude-standard", "-z"])
        paths = {root / os.fsdecode(raw) for raw in names.split(b"\0") if raw
                 and not set(Path(os.fsdecode(raw)).parts).intersection({".lake", "lake-packages", ".git", ".archon-horizon"})}
    else:
        paths = set(iter_lean_files(root)) | {root / "lakefile.toml"}
    return sorted(p for p in paths if p.is_file() and
                  (p.suffix in (".lean", ".sh", ".bash", ".zsh") or p.name in ("lakefile.toml", "Makefile")))


def inspect(root: Path, *, capture_signatures: bool = False) -> dict:
    result = {}
    for p in sources(root):
        name = p.relative_to(root).as_posix()
        if p.is_symlink() and not p.resolve().is_relative_to(root.resolve()):
            result[name] = {"hash": "", "issues": ["source symlink leaves the member"]}
            continue
        content = p.read_bytes()
        source = content.decode("utf-8", "replace")
        result[name] = {"hash": hashlib.sha256(content).hexdigest(),
                        "issues": source_findings(name, source)}
        if capture_signatures and p.suffix == ".lean" and may_have_setup(source):
            # Baselines also cover uncommitted sources. Keep candidate text so proof-only
            # edits don't turn an unchanged statement into a newly introduced finding.
            result[name]["setup_source"] = source.replace("\r\n", "\n")
    return result


def baseline(ws: Workspace, name: str, *, begin: bool = False) -> dict:
    path = directory(ws, name) / "baseline.json"
    with workspace_lock(ws.state_path, f"quality-baseline:{name}"):
        stored = read(path)
        if not stored or (begin and stored.get("delivered")):
            root = ws.project_path(name)
            stored = {"head": head(root), "has_setup_sources": True, "files": inspect(root, capture_signatures=True)}
            with workspace_lock(ws.state_path, f"quality-questions:{name}"):
                # The Inbox keeps the old conversations. Their decisions belong
                # to the previous baseline, not to newly imported human work.
                save(directory(ws, name) / "questions.json", {})
                save(path, stored)
        return stored


def mark_delivered(ws: Workspace, name: str, revision: str) -> None:
    path = directory(ws, name) / "baseline.json"
    with workspace_lock(ws.state_path, f"quality-baseline:{name}"):
        data = read(path)
        data["delivered"] = revision
        save(path, data)


def _baseline_setup_source(root: Path, file: str, starting: dict) -> str | None:
    old = starting["files"].get(file)
    if not old or starting.get("has_setup_sources"):
        return (old or {}).get("setup_source", "")
    # Older baselines kept only hashes. Recover the original bytes when committed.
    # An uncommitted baseline that cannot be recovered needs a human decision.
    if starting.get("head"):
        blob = subprocess.run(["git", "show", starting["head"] + ":" + file], cwd=root, capture_output=True)
        if blob.returncode == 0 and hashlib.sha256(blob.stdout).hexdigest() == old["hash"]:
            return blob.stdout.decode("utf-8", "replace").replace("\r\n", "\n")
    return None


def _record_questions(ws: Workspace, name: str, entries: list[tuple[str, str, bool]]) -> dict:
    path = directory(ws, name) / "questions.json"
    with workspace_lock(ws.state_path, f"quality-questions:{name}"):
        saved = read(path)
        missing = [e for e in entries if json.dumps(e) not in saved]
        if missing:
            details = "\n".join(f"- {file}: {reason}" for file, reason, _ in missing)
            box = FilesystemInboxProvider(ws.state_path / "inbox" / "local")
            item = box.create_item(InboxDraft(
                kind=InboxKind.CONVERSATION, author="horizon", audience="human",
                body=(f"Existing quality issues in {name}\n\n{details}\n\n"
                      "How should Horizon handle these files? Reply here with the intended repair, "
                      "or confirm your own fix here and rerun the supervisor. To retain a listed historical style or "
                      "heartbeat setting, write 'keep PATH' on its own line. Proof holes, statement setup "
                      "chains and file time limits must pass verification. Only the project owner can "
                      "change the time limit."),
                scope=InboxScope(projects=(name,), files=tuple(dict.fromkeys(e[0] for e in missing))),
                metadata={"participants": ["human", f"project:{name}"], "started_by": f"project:{name}"},
            ))
            saved.update({json.dumps(e): item.id for e in missing})
            save(path, saved)
        return saved


def _statement_findings(ws: Workspace, name: str, files: dict, compiled: list[str], starting: dict) -> list[dict]:
    root = ws.project_path(name)
    limit = ws.project(name).max_file_seconds
    issues = []
    inherited = []
    for file in compiled:
        try:
            setups = signature_findings(root, file, per_file_seconds=limit)
        except SignatureCheckError as exc:
            issues.append({"file": file, "reason": f"Lean signature review failed: {exc}"})
            continue
        if not setups:
            continue
        unchanged = files[file]["hash"] == starting["files"].get(file, {}).get("hash")
        prior_source = None if unchanged else _baseline_setup_source(root, file, starting)
        for setup in setups:
            issue = {"file": file, "line": setup.line, "reason": setup.reason}
            issues.append(issue)
            if prior_source is None or setup.header.replace("\r\n", "\n") in prior_source:
                inherited.append(issue)
    if inherited:
        entries = [(i["file"], i["reason"], True) for i in inherited]
        linked = _record_questions(ws, name, entries)
        for issue, entry in zip(inherited, entries):
            issue["inbox"] = linked[json.dumps(entry)]
    return issues


def prepare_baseline(ws: Workspace, name: str, *, begin: bool = False) -> None:
    files = baseline(ws, name, begin=begin)["files"]
    entries = [(file, issue, True) for file, info in files.items()
               for issue in sorted(set(info["issues"]) - {"unfinished Lean proof"})]
    if entries:
        _record_questions(ws, name, entries)


def ask(ws: Workspace, name: str, file: str, reason: str, *, automatic: bool = False) -> str:
    """Link each inherited finding to one durable human conversation."""
    if file not in baseline(ws, name)["files"]:
        raise ValueError(f"{file} did not exist at baseline; repair the new issue in the review.")
    entry = (file, reason, automatic)
    return _record_questions(ws, name, [entry])[json.dumps(entry)]


def questions(ws: Workspace, name: str) -> list[dict]:
    box = FilesystemInboxProvider(ws.state_path / "inbox" / "local")
    result = []
    comments = {}
    for key, ident in read(directory(ws, name) / "questions.json").items():
        file, reason, automatic = json.loads(key)
        try:
            if ident not in comments:
                comments[ident] = box.get_item(ident).metadata.get("comments", [])
        except (KeyError, FileNotFoundError):
            replies = []
        else:
            replies = [c["body"] for c in comments[ident]
                       if (c.get("author") == "human" or c.get("human_reply"))
                       and not (c.get("provenance") or {}).get("run")]
        latest = replies[-1] if replies else ""
        retain = any(line.strip() in (f"keep {file}", f"保留 {file}") for line in latest.splitlines())
        result.append({"file": file, "reason": reason, "inbox": ident, "automatic": automatic,
                       "reply": "\n".join(replies), "retain": retain})
    return result


def dirty_sources(root: Path) -> bool:
    return bool(subprocess.check_output(["git", "-C", str(root), "status", "--porcelain", "--untracked-files=all",
                                         "--", "*.lean", "*.toml", "*.sh", "*.bash", "*.zsh", "*.tex", "*.bib", "Makefile", "lean-toolchain", "lake-manifest.json"]))


def bind_review_completion(ws: Workspace, old: HorizonTask, updated: HorizonTask) -> HorizonTask:
    """Bind an explicit semantic sign-off to the committed sources visible at that transition."""
    if (updated.metadata.get("supervisor_source") != "quality" or updated.status is not TaskStatus.DONE
            or old.status is TaskStatus.DONE):
        return updated
    revisions = {}
    for name in updated.projects or (updated.project,):
        root = ws.project_path(name)
        revision = head(root)
        if not revision or dirty_sources(root):
            raise ValueError(f"Commit the delivery sources in {name} before completing the quality review.")
        revisions[name] = revision
    return replace(updated, metadata={**updated.metadata, "reviewed_heads": revisions})


def check_project(ws: Workspace, name: str, *, final: bool = False) -> dict:
    root = ws.project_path(name)
    starting = baseline(ws, name)
    original = starting["files"]
    before = head(root)
    limit = ws.project(name).max_file_seconds
    files = inspect(root)
    issues = []
    timings = {}
    retained = []
    existing = [(file, reason, True) for file, info in files.items()
                for reason in sorted(set(info["issues"]).intersection(original.get(file, {}).get("issues", [])))]
    if existing:
        _record_questions(ws, name, existing)
    decisions = {(q["file"], q["reason"]): q for q in questions(ws, name)}

    def finding(file: str, reason: str, legacy: bool = False):
        issue = {"file": file, "reason": reason}
        if legacy:
            decision = decisions.get((file, reason))
            issue["inbox"] = decision["inbox"] if decision else ask(ws, name, file, reason, automatic=True)
            if (decision and decision["retain"] and reason in original[file]["issues"]
                    and reason not in {"unfinished Lean proof", "source symlink leaves the member"}):
                retained.append(issue)
                return
        issues.append(issue)

    for file, info in files.items():
        for reason in sorted(set(info["issues"])):
            inherited = original.get(file, {}).get("issues", [])
            finding(file, reason, info["issues"].count(reason) <= inherited.count(reason))
    if final:
        if not before:
            finding(".", "No committed member revision to verify")
        elif dirty_sources(root):
            finding(".", "Commit the delivery sources and configuration before final verification")
    if not any((root / f).is_file() for f in ("lakefile.lean", "lakefile.toml")):
        finding(".", "Missing Lake project configuration")
    else:
        try:
            ok, _, error = _run(["lake", "build"], root)
            configured = ws.project(name).build_command
            if ok and configured and shlex.split(configured) != ["lake", "build"]:
                ok, _, error = _run(["sh", "-c", configured], root)
            if not ok:
                finding(".", f"lake build failed: {error}")
            else:
                compiled = []
                with tempfile.TemporaryDirectory() as scratch:
                    for file in files:
                        if not file.endswith(".lean") or files[file]["hash"] == "":
                            continue
                        # Prepare this file's imports, including non-default targets. Lake passes the
                        # package arguments and invokes Lean on the source again for the timed pass.
                        ok, _, error = _run(["lake", "lean", file, "--", "--deps"], root)
                        if not ok:
                            finding(file, f"import preparation failed: {error}")
                            continue
                        ok, elapsed, error = _run(["lake", "lean", file, "--", "-o", str(Path(scratch) / "Review.olean")],
                                                  root, limit)
                        timings[file] = round(elapsed, 4)
                        if (ok or error == "compile time exceeded") and elapsed > limit:
                            finding(file, f"compilation exceeds {limit:g}s",
                                    files[file]["hash"] == original.get(file, {}).get("hash"))
                        elif not ok:
                            finding(file, f"Lean compilation failed: {error}")
                        else:
                            compiled.append(file)
                issues.extend(_statement_findings(ws, name, files, compiled, starting))
        except OSError as exc:
            finding(".", str(exc))
    if head(root) != before or inspect(root) != files or (final and before and dirty_sources(root) and not any(i["file"] == "." for i in issues)):
        finding(".", "Member changed during verification; rerun on a stable revision")
    result = {"project": name, "revision": before, "limit_seconds": limit,
              "timings": timings, "retained": retained, "issues": issues, "ok": not issues}
    save(directory(ws, name) / "check.json", result)
    return result
