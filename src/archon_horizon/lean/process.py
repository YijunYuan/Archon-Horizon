"""Run a Lean process and its children within a timeout."""
from __future__ import annotations

import os
import signal
import subprocess
import tempfile
import time
from pathlib import Path


def run_lean_command(command: list[str], root: Path, timeout: float | None = None) -> tuple[bool, float, str]:
    start = time.monotonic()
    with tempfile.TemporaryFile() as output:
        proc = subprocess.Popen(command, cwd=root, stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            proc.wait(timeout=timeout)
        except BaseException as exc:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait()
            if not isinstance(exc, subprocess.TimeoutExpired):
                raise
            return False, time.monotonic() - start, "compile time exceeded"
        elapsed = time.monotonic() - start
        output.seek(max(0, output.tell() - 3000))
        return proc.returncode == 0, elapsed, output.read().decode(errors="replace").strip()
