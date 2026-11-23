"""Review public theorem statements with the member's own Lean parser."""
from __future__ import annotations

import json
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .process import run_lean_command


@dataclass(frozen=True)
class SignatureFinding:
    reason: str
    header: str
    line: int


class SignatureCheckError(RuntimeError):
    """The native review could not be completed."""


def may_have_setup(source: str) -> bool:
    """Cheap necessary condition only. Lean resolves actual tokens and visibility."""
    return ("theorem" in source or "lemma" in source) and (
        source.count("letI") >= 2 or source.count("let") >= 3
    )


def signature_findings(
    root: Path, file: str, *, per_file_seconds: float = 30,
) -> list[SignatureFinding]:
    """Run a fresh frontend per candidate. Lean keeps imported environments alive until exit."""
    try:
        if not file.endswith(".lean") or Path(file).name == "lakefile.lean":
            return []
        if not may_have_setup((root / file).read_text(encoding="utf-8")):
            return []
        helper = Path(__file__).with_name("SignatureReview.lean").resolve()
        with tempfile.TemporaryDirectory() as scratch:
            scratch_dir = Path(scratch)
            config = scratch_dir / "member.json"
            output = scratch_dir / "report.json"
            # Lake compiles its DSL in the scratch project, including in read-only installations.
            shutil.copyfile(helper.with_name("ReviewConfig.lean"), scratch_dir / "lakefile.lean")
            ok, _, error = run_lean_command(
                ["lake", "-d", scratch, "script", "run", "prepare", str(root.resolve()),
                 str((root / file).resolve()), str(config)], root, per_file_seconds + 30,
            )
            if not ok:
                raise SignatureCheckError(error or "Lake could not prepare the member configuration")
            ok, _, error = run_lean_command(
                ["lake", "env", "lean", "--run", str(helper), file, str(config), str(output)],
                root, per_file_seconds + 30,
            )
            if not ok:
                raise SignatureCheckError(error or "Lean exited without a signature report")
            rows = json.loads(output.read_text(encoding="utf-8"))
            return [
                SignatureFinding(
                    f"public theorem {r['name']} has nested setup in its signature "
                    f"({r['let']} let, {r['letI']} letI). Refactor the public API "
                    "so callers can state the proposition directly",
                    r["header"], r["line"],
                )
                for r in sorted(rows, key=lambda row: row["line"])
            ]
    except (OSError, UnicodeError, ValueError, KeyError, TypeError) as exc:
        raise SignatureCheckError(str(exc)) from exc
