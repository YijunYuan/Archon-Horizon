"""Run a Lean process and its children within a timeout."""
from __future__ import annotations

import os
import signal
import subprocess
import tempfile
import time
from pathlib import Path


def run_lean_command(command: list[str], root: Path, timeout: float | None = None) -> tuple[bool, float, str]:
    from archon_horizon.orchestration.compile_gate import admitted_command
    from archon_horizon.orchestration.worker_cgroup import limit_command

    with admitted_command(command, cwd=root) as launch, tempfile.TemporaryFile() as output:
        # A file's compile-time limit measures Lean after its resource slot becomes available.
        start = time.monotonic()
        proc = subprocess.Popen(limit_command(launch.argv), cwd=root, stdout=output, stderr=subprocess.STDOUT,
                                env=launch.env, pass_fds=launch.pass_fds, start_new_session=True)
        try:
            launch.observe(proc.pid)
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
