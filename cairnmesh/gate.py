"""The checks every protocol change must pass, in two tiers.

    python -m cairnmesh.gate quick   # pytest, both sweep baselines, stress 4 seeds (report to a temp dir)
    python -m cairnmesh.gate full    # the same, then the slow pytest scenarios, stress 16 and robustness 8 (the committed reports)

Run from the repository root. Each step prints its last line; the run stops
at the first step that fails. ``CAIRNMESH_WORKERS`` sets the parallel
processes (``simpool``: every core by default).
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import time
from pathlib import Path


def _steps(tier: str, scratch: Path) -> list[tuple[str, list[str]]]:
    py = [sys.executable]
    steps = [
        ("pytest", py + ["-m", "pytest", "-q"]),
        ("sweep 40", py + ["-m", "cairnmesh.session_bench", "sweep", "40"]),
        ("sweep 40 --disc", py + ["-m", "cairnmesh.session_bench", "sweep", "40", "--disc"]),
    ]
    if tier == "quick":
        steps.append(("stress 4", py + ["-m", "cairnmesh.stress_bench", "4", "--out", str(scratch / "stress.md")]))
    else:
        # tests/scenario (-m slow): one seed of each extreme case, RESULT loss, nonce audit (about 5 min)
        steps.append(("pytest -m slow", py + ["-m", "pytest", "-q", "-m", "slow"]))
        steps.append(("stress 16", py + ["-m", "cairnmesh.stress_bench", "16"]))
        steps.append(("robustness 8", py + ["-m", "cairnmesh.robustness_bench", "8"]))
    return steps


def main(argv: list[str]) -> int:
    tier = argv[0] if argv else "quick"
    if tier not in ("quick", "full"):
        print(__doc__)
        return 2
    with tempfile.TemporaryDirectory() as scratch:
        for name, command in _steps(tier, Path(scratch)):
            start = time.monotonic()
            done = subprocess.run(command, capture_output=True, text=True)
            lines = [line for line in (done.stdout + done.stderr).splitlines() if line.strip()]
            last = lines[-1] if lines else ""
            print(f"[{name}] {time.monotonic() - start:5.0f} s  {last}", flush=True)
            if done.returncode != 0:
                print("\n".join(lines[-30:]))
                return done.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
