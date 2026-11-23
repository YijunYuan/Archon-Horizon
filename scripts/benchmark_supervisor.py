"""Time one disposable blueprint from queued proofs to verified delivery.

Uses the horizon executable on PATH, the active Lean toolchain and a local command
harness with a fixed model-response delay. No model service or mathlib is required.
Use identical CPU affinity, options and toolchain when comparing revisions.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import time

import psutil
import yaml


def run_agent(delay: float) -> None:
    task = os.environ["ARCHON_HORIZON_TASK"]
    if task.startswith("review-"):
        # The supervisor independently builds and checks every declaration before delivery.
        subprocess.run([os.environ["HORIZON_BIN"], "task", "set", task, "--status", "done"], check=True)
        return
    name = os.environ["ARCHON_HORIZON_TASK_TITLE"].rsplit(":", 1)[-1]
    source = Path(name + ".lean")
    (Path(os.environ["ARCHON_HORIZON_ROOT"]) / "attempts" / str(os.getpid())).write_text(name)
    time.sleep(delay)
    source.write_text(source.read_text().replace("by sorry", "by simp"))
    subprocess.run(["lake", "lean", str(source)], check=True)
    subprocess.run([os.environ["HORIZON_GIT"], "commit", "-m", f"feat: prove {name}", "--", str(source)], check=True)


def benchmark(root: Path, nodes: int, delay: float, poll_seconds: float) -> dict:
    member = root / "Proofs"
    (root / "attempts").mkdir()
    blueprint = member / "blueprint/src"
    blueprint.mkdir(parents=True)
    (root / "config.yaml").write_text(yaml.safe_dump({
        "workspace": {"name": "benchmark", "horizon_agent": {"harness": "local"},
                      "scheduler": {"poll_seconds": poll_seconds}},
        "harnesses": {"local": {"kind": "command", "command": sys.executable,
                                "args": [str(Path(__file__).resolve()), "--agent", "--delay", str(delay)]}},
        "projects": {"math": {"path": "Proofs"}},
    }))
    (member / "lakefile.toml").write_text('name = "proofs"\n')
    (member / ".gitignore").write_text(".lake/\n")
    statements = []
    for i in range(nodes):
        name = f"node{i}"
        (member / (name + ".lean")).write_text(
            f"/- Copyright (c) Horizon contributors. -/\nmodule\n/-! # {name} -/\n"
            f"public theorem {name} (n : Nat) : n + 0 = n := by sorry\n"
        )
        statements.append(rf"\begin{{theorem}}\label{{thm:{name}}}\lean{{{name}}}\leanok "
                          r"Adding zero preserves a natural number.\end{theorem}")
    (blueprint / "content.tex").write_text("\n".join(statements))
    subprocess.run(["lake", "build"], cwd=member, check=True, capture_output=True)
    for args in (["init", "-q"], ["config", "user.name", "Benchmark"], ["config", "user.email", "bench@localhost"],
                 ["add", "."], ["commit", "-qm", "initialize proofs"]):
        subprocess.run(["git", *args], cwd=member, check=True)
    binary = shutil.which("horizon")
    if not binary:
        raise RuntimeError("Install horizon and put its executable on PATH")
    state = root / ".archon-horizon"
    started = time.monotonic()
    peak_rss = max_active = 0
    with (root / "supervisor.log").open("w+") as log:
        proc = subprocess.Popen([binary, "--root", str(root), "run", "--supervisor", "math",
                                 "--no-dashboard", "--rounds", "1"], cwd=root, stdout=log, stderr=log)
        tree = psutil.Process(proc.pid)
        try:
            while proc.poll() is None:
                rss = 0
                for child in [tree, *tree.children(recursive=True)]:
                    try:
                        rss += child.memory_info().rss
                    except psutil.NoSuchProcess:
                        continue
                peak_rss = max(peak_rss, rss)
                status = state / "supervisor.json"
                if status.exists():
                    max_active = max(max_active, len(json.loads(status.read_text())["active"]))
                time.sleep(0.1)
        finally:
            if proc.poll() is None:
                proc.terminate()
                proc.wait()
        elapsed = time.monotonic() - started
        if proc.returncode:
            log.seek(0)
            raise RuntimeError(log.read()[-5000:])
    status = json.loads((state / "supervisor.json").read_text())
    if status.get("delivery") != "delivered":
        raise RuntimeError("Blueprint did not reach verified delivery")
    attempts = [path.read_text() for path in (root / "attempts").iterdir()]
    if set(attempts) != {f"node{i}" for i in range(nodes)}:
        raise RuntimeError("The benchmark harness did not execute every proof")
    return {"verified_nodes": nodes, "wall_seconds": round(elapsed, 2), "model_delay_seconds": delay,
            "supervisor_poll_seconds": poll_seconds,
            "proof_retries": len(attempts) - nodes, "max_active_runs": max_active,
            "peak_sampled_tree_rss_gib": round(peak_rss / 1024**3, 3), "compiles": status.get("compiles")}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nodes", type=int, default=6)
    parser.add_argument("--delay", type=float, default=8, help="synthetic model wait per proof in seconds")
    parser.add_argument("--poll-seconds", type=float, default=5, help="supervisor poll interval (seconds)")
    parser.add_argument("--agent", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("prompt", nargs="?", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.nodes < 1 or args.delay < 0 or args.poll_seconds < 0.1:
        parser.error("nodes must be positive, delay non-negative and poll-seconds at least 0.1")
    if args.agent:
        run_agent(args.delay)
    else:
        with TemporaryDirectory(prefix="horizon-benchmark-") as directory:
            print(json.dumps(benchmark(Path(directory), args.nodes, args.delay, args.poll_seconds), indent=2))
