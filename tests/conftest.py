"""Fixtures for tests that invoke a member project's real Lean toolchain."""
import os
import re
import shutil
import subprocess

import pytest


@pytest.fixture
def lean_toolchain(monkeypatch):
    if not shutil.which("lake"):
        pytest.skip("Lake is not installed")
    if shutil.which("elan"):
        # Pin an installed release so the Elan proxy never fetches a toolchain.
        installed = subprocess.check_output(["elan", "toolchain", "list"], text=True)
        versions = re.findall(r"^leanprover/lean4:v4\.(\d+)\.(\d+)(?=\s|$)", installed, re.M)
        if not versions:
            pytest.skip("No installed Lean 4 release")
        releases = {f"leanprover/lean4:v4.{m}.{p}": (int(m), int(p)) for m, p in versions}
        minor, patch = releases.get(os.environ.get("ELAN_TOOLCHAIN"), max(releases.values()))
        if minor < 23:
            pytest.skip("Lean module support is required")
        monkeypatch.setenv("ELAN_TOOLCHAIN", f"leanprover/lean4:v4.{minor}.{patch}")
