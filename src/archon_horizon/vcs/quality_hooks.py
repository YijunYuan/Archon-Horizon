"""Per-session Git hooks for member checkouts, including plain git and partial commits."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shlex
from collections import Counter
import subprocess
import sys

from archon_horizon.core.atomic import write_text_atomic
from archon_horizon.core.locks import workspace_lock
from archon_horizon.lean.quality import commit_subject_error, protected_settings, source_findings


def hook_environment(state: Path, roots: list[str]) -> dict[str, str]:
    result = {}
    count = int(os.environ.get("GIT_CONFIG_COUNT", 0))
    for root in roots:
        if not (Path(root) / ".git").exists():
            continue
        def git_path(*args: str) -> str:
            return subprocess.check_output(["git", "-C", root, "rev-parse", "--path-format=absolute", *args],
                                           text=True).strip()
        gitdir = git_path("--git-dir")
        hooks = Path(git_path("--git-path", "hooks"))
        if hooks.is_relative_to(state.resolve() / "quality" / "git"):
            continue  # A nested session already inherits the overlay.
        key = hashlib.sha256(os.fsencode(root) + b"\0" + os.fsencode(hooks) + b"\0" + os.fsencode(sys.executable)).hexdigest()[:24]
        directory = state.resolve() / "quality" / "git" / key
        managed = directory / "hooks"
        with workspace_lock(state, f"quality-hooks:{root}"):
            managed.mkdir(parents=True, exist_ok=True)
            for name in ("prepare-commit-msg", "commit-msg"):
                native = shlex.quote(str(hooks / name))
                body = (f"#!/bin/sh\nif test -x {native}; then {native} \"$@\" || exit $?; fi\n"
                        f"exec {shlex.quote(sys.executable)} -m archon_horizon.vcs.quality_hooks {name} \"$@\"\n")
                path = managed / name
                if not path.exists() or path.read_text() != body:
                    write_text_atomic(path, body)
                    path.chmod(0o755)
            # Delegate the checkout's other hooks, including its signing and
            # post-commit automation. The repository's config is never edited.
            if hooks.is_dir():
                for native_hook in hooks.iterdir():
                    link = managed / native_hook.name
                    if native_hook.name not in ("prepare-commit-msg", "commit-msg") and not link.is_symlink():
                        link.symlink_to(native_hook)
            config = directory / "config"
            write_text_atomic(config, f"[core]\n\thooksPath = {json.dumps(str(managed), ensure_ascii=False)}\n")
        result[f"GIT_CONFIG_KEY_{count}"] = f"includeIf.gitdir:{gitdir}.path"
        result[f"GIT_CONFIG_VALUE_{count}"] = str(config)
        count += 1
    if result:
        result["GIT_CONFIG_COUNT"] = str(count)
    return result


def _blob(spec: str) -> str:
    p = subprocess.run(["git", "show", spec], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    return p.stdout.decode("utf-8", "replace") if not p.returncode else ""


def check_index() -> str:
    # Git supplies the actual (possibly temporary) index for --only commits.
    paths = subprocess.check_output(["git", "diff", "--cached", "--no-renames", "--name-only", "-z"])
    for entry in paths.split(b"\0"):
        path = os.fsdecode(entry)
        if not path.endswith((".lean", ".toml", ".sh", ".bash", ".zsh")) and Path(path).name != "Makefile":
            continue
        old, new = _blob("HEAD:" + path), _blob(":" + path)
        if protected_settings(path, old) != protected_settings(path, new):
            return f"{path}: Horizon must preserve maxHeartbeats and synthInstance.maxHeartbeats."
        # Ordinary proof edits in an existing file are fine. Inherited style
        # findings that this commit would clean up need the human's direction.
        removed = Counter(source_findings(path, old)) - Counter(source_findings(path, new) if new else [])
        removed.pop("unfinished Lean proof", None)
        if old and removed and (root := os.environ.get("HORIZON_QUALITY_ROOT")):
            from archon_horizon.config.loader import build_workspace, load_config
            from archon_horizon.lean.delivery import ask, baseline, questions
            member = json.loads(os.environ["HORIZON_QUALITY_PROJECTS"])[str(Path.cwd().resolve())]
            ws = build_workspace(load_config(Path(root)), Path(root))
            inherited = baseline(ws, member)["files"].get(path, {}).get("issues", [])
            for reason in removed.keys() & set(inherited):
                ident = ask(ws, member, path, reason, automatic=True)
                if not any(q["inbox"] == ident and q["reply"] for q in questions(ws, member)):
                    return f"{path}: ask the human before repairing {reason}. Reply in Inbox {ident}."
    return ""


if __name__ == "__main__":
    if sys.argv[1] == "commit-msg":
        lines = Path(sys.argv[2]).read_text(errors="replace").splitlines()
        error = commit_subject_error(lines[0] if lines else "")
    else:
        error = check_index()
        if not error and len(sys.argv) > 3 and sys.argv[3] == "message":
            lines = Path(sys.argv[2]).read_text(errors="replace").splitlines()
            error = commit_subject_error(lines[0] if lines else "")
    if error:
        sys.exit(error)
