"""Publish a complete file to concurrent readers in the same workspace."""

from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4


def write_text_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
