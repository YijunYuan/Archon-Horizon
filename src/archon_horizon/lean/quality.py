"""Checks shared by agent commits and final Lean delivery."""
from __future__ import annotations

import re
from pathlib import Path

OPTIONS = r"(?:synthInstance\.maxHeartbeats|maxHeartbeats)"
_NAT = r"(?:0[xX][0-9a-fA-F_]+|0[bB][01_]+|0[oO][0-7_]+|[0-9][0-9_]*)"
_OVERRIDE = re.compile(rf"\bset_option\s+({OPTIONS})\s+({_NAT})\b(\s+in\b)?")
_FLAG = re.compile(rf"(?<!\w)(?:-D\s*({OPTIONS})\s*=\s*({_NAT})|(?:-T\s*|--timeout[=\s]+)({_NAT}))\b")
_LAKE_OPTION = re.compile(r"(?<![\w.])(" + OPTIONS + r")(?![\w.])[`'\"\s:=,()]*?(?:\.ofNat\s+)?(\d[\d_]*)")
_SUBJECT = re.compile(r"(feat|fix|doc|style|refactor|test|chore|perf|ci)(\([^()\n]+\))?: (\S.*)")
_PROCESS = re.compile(r"(?:\b(?:Horizon\s+run\s+#?\d+|run\s+#\d+|run\s+0\d{2,}|(?:in|during)\s+run\s+\d+)\b|"
                      r"\brun\s+\d+\s*:|第\s*\d+\s*(?:轮|次|个)\s*run)", re.I)
_TOKENS = re.compile(r'/\-|--[^\n]*|"(?:\\.|[^"\\])*"', re.S)
_BLOCK = re.compile(r'/\-|-/')


def lean_parts(source: str, *, keep_strings: bool = False) -> tuple[str, str]:
    """Mask strings/comments for command checks; preserve line numbers and collect comments."""
    code, comments = [], []
    start = 0
    while match := _TOKENS.search(source, start):
        code.append(source[start:match.start()])
        end = match.end()
        if match[0] == '/-':
            depth = 1
            for token in _BLOCK.finditer(source, end):
                depth += 1 if token[0] == '/-' else -1
                if depth == 0:
                    end = token.end()
                    break
            else:
                end = len(source)
        part = source[match.start():end]
        if not part.startswith('"'):
            comments.append(part)
        code.append(part if keep_strings and part.startswith('"') else re.sub(r'[^\n]', ' ', part))
        start = end
    code.append(source[start:])
    return ''.join(code), '\n'.join(comments)


def protected_settings(path: str, source: str) -> list[str]:
    lean = path.endswith(".lean")
    lake = Path(path).name in ("lakefile.lean", "lakefile.toml")
    if lean:
        code, _ = lean_parts(source)
        changes = [f"{key}={val}" + (" in" if local else "") for key, val, local in _OVERRIDE.findall(code)]
    else:
        changes = []
    if lake or path.endswith((".sh", ".bash", ".zsh")) or Path(path).name == "Makefile":
        # Retain quoted Lake option keys and compiler argument strings.
        if lake and lean:
            source, _ = lean_parts(source, keep_strings=True)
        elif not lean:
            source = '\n'.join(line for line in source.splitlines() if not line.lstrip().startswith('#'))
        changes.extend(f"{key}={val}" for key, val in _LAKE_OPTION.findall(source) if lake)
        changes.extend(f"{key or 'maxHeartbeats'}={val or short}" for key, val, short in _FLAG.findall(source))
    return sorted(changes)


def changes_protected_setting(tool: str, data: dict | str, cwd: Path) -> bool:
    """Compare the proposed source on edit tools before the engine writes it."""
    def relevant(path: str) -> bool:
        return path.endswith((".lean", ".sh", ".bash", ".zsh")) or Path(path).name in ("lakefile.toml", "Makefile")

    def current(path: str) -> str:
        try:
            return (cwd / path).read_text(errors="replace")
        except OSError:
            return ""

    if tool in ("edit", "multiedit", "write") and isinstance(data, dict):
        path = data.get("file_path", "")
        if not isinstance(path, str) or not relevant(path):
            return False
        before = current(path)
        after = before
        if tool == "write":
            after = data.get("content", "")
        else:
            for edit in data.get("edits", [data]):
                if not isinstance(edit, dict) or not all(isinstance(edit.get(k), str) for k in ("old_string", "new_string")):
                    return False
                after = after.replace(edit["old_string"], edit["new_string"], -1 if edit.get("replace_all") else 1)
        return isinstance(after, str) and protected_settings(path, before) != protected_settings(path, after)
    if tool not in ("applypatch", "applypatchedit", "apply_patch"):
        return False
    patch = data if isinstance(data, str) else data.get("patch") or data.get("input") or ""
    for part in re.split(r"(?m)^\*\*\* (?=(?:Add|Update|Delete) File: )", patch)[1:]:
        header, _, body = part.partition("\n")
        operation, path = header.split(" File: ", 1)
        if not relevant(path):
            continue
        if operation == "Delete":
            if protected_settings(path, current(path)):
                return True
            continue
        lines = body.splitlines()
        old = "\n".join(line[1:] for line in lines if line.startswith((" ", "-")))
        new = "\n".join(line[1:] for line in lines if line.startswith((" ", "+")))
        if protected_settings(path, old) != protected_settings(path, new):
            return True
    return False


def commit_subject_error(subject: str) -> str:
    match = _SUBJECT.fullmatch(subject)
    if not match or match[3][0].isupper() or subject.endswith("."):
        return "Use a mathlib title, e.g. feat(Algebra): prove the cancellation lemma"
    return ""


def source_findings(path: str, source: str) -> list[str]:
    issues = [f"protected setting {v}" for v in protected_settings(path, source)]
    if not path.endswith(".lean"):
        return issues
    code, comments = lean_parts(source)
    if Path(path).name != "lakefile.lean":
        if not re.match(r"^\s*/-\s*Copyright\b", source, re.I):
            issues.append("missing copyright header")
        if not re.search(r"(?m)^module\s*$", code):
            issues.append("missing module header")
        if not re.search(r"/-!\s*#\s+\S", comments):
            issues.append("missing titled module docstring")
    if _PROCESS.search(comments):
        issues.append("run history in source comments")
    if re.search(r"(?<![\w.'])\b(?:sorry|admit)\b(?![\w'])", code):
        issues.append("unfinished Lean proof")
    return issues
