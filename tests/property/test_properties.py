"""Property tests and the coverage-guided fuzzer.

Short budgets (fixed case counts, a few seconds) run with every ``pytest``;
long ones only with ``-m long`` (``CAIRNMESH_LONG_SECONDS`` each, default 600),
on a server. The fuzzer runs in a child process under a memory cap: before
the replay-window fix (#4) one input made memory run away.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("cryptography")

import pbt  # noqa: E402

HERE = Path(__file__).resolve().parent
LONG = float(os.environ.get("CAIRNMESH_LONG_SECONDS", "600"))
MEMORY = int(os.environ.get("CAIRNMESH_FUZZ_MEMORY", str(3 << 30)))
#: Past this a device has given up sending a RESULT (10 s doubling, 5 sends, -25 %).
RESULT_GIVEN_UP = 230.0


def _late_ack(ops) -> bool:
    """An acknowledgement after the device gave up sending its RESULT: it ignores it (the
    record closes on the controller's query instead), where the model closes the record."""

    elapsed = 0.0
    for op in ops:
        if op[0] == "time":
            elapsed += op[1]
        elif op[0] == "ack" and elapsed >= RESULT_GIVEN_UP:
            return True
    return False


def _aged_reboot(ops) -> bool:
    """A reboot while some record was past ``pbt.TTL``: such a record is gone or, never looked
    up before the reboot, kept and timed from boot again (no RTC); spec 4.1 allows both. Its
    presence decides lookups and the ``max_open`` count, so either outcome is right."""

    elapsed, first = 0.0, {}
    for op in ops:
        if op[0] == "time":
            elapsed += op[1]
        elif op[0] in ("req", "query"):
            first.setdefault((op[1], op[2]), elapsed)
        elif op[0] == "reboot" and any(elapsed - t > pbt.TTL for t in first.values()):
            return True
    return False


def _known(key: str, failure: dict) -> str | None:
    if key.startswith("dedup:") and _late_ack(failure["ops"]):
        # The device is right (spec 4.1): a REPORT's acknowledgement is a REPLY too, under the
        # device's own ids; closing on any REPLY could release a record whose result never arrived.
        return "model only: an acknowledgement after the device gave up its RESULT (spec 4.1: only a query closes it then)"
    if key.startswith("dedup:") and _aged_reboot(failure["ops"]):
        return "spec allows either: a record past dedup_ttl before a reboot, gone or timed from boot again"
    return None


def _check(out: dict) -> None:
    unknown = {key: value for key, value in out["failures"].items() if _known(key, value) is None}
    assert not unknown, json.dumps(unknown, default=str, indent=1)
    if out["failures"]:
        reasons = sorted({_known(key, value) for key, value in out["failures"].items()})
        pytest.xfail("known divergences from the spec model: " + "; ".join(reasons))


@pytest.mark.parametrize("seed", [1, 2])
def test_replay_window_matches_its_model(seed):
    _check(pbt.run("window", 60.0, seed, max_cases=20_000))


@pytest.mark.parametrize("seed", [1, 2])
def test_device_dedup_matches_the_spec_model(seed):
    _check(pbt.run("dedup", 60.0, seed, max_cases=1_500))


@pytest.mark.long
@pytest.mark.parametrize("kind", ["window", "dedup"])
def test_properties_long(kind):
    _check(pbt.run(kind, LONG, int.from_bytes(os.urandom(4), "little")))


def _limit() -> None:
    import resource

    resource.setrlimit(resource.RLIMIT_AS, (MEMORY, MEMORY))


def _fuzz(target: str, seconds: float, seed: int) -> dict:
    code = f"import json, fuzz; print(json.dumps(fuzz.run({target!r}, {seconds}, {seed}), default=str))"
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(filter(None, [str(HERE.parents[1]), os.environ.get("PYTHONPATH")])))
    done = subprocess.run(
        [sys.executable, "-c", code], cwd=HERE, env=env, capture_output=True, text=True,
        timeout=seconds * 3 + 120, preexec_fn=_limit,
    )
    assert done.returncode == 0, done.stderr[-3000:]
    return json.loads(done.stdout.strip().splitlines()[-1])


needs_monitoring = pytest.mark.skipif(sys.version_info < (3, 12), reason="the fuzzer's coverage uses sys.monitoring (3.12+)")
TARGETS = ["node", "gateway", "decode", "forge"]


@needs_monitoring
@pytest.mark.skipif(sys.platform != "linux", reason="memory cap through RLIMIT_AS")
@pytest.mark.parametrize("target", TARGETS)
def test_fuzz_short(target):
    out = _fuzz(target, 3.0, 1)
    assert out["execs"] > 0 and not out["findings"], json.dumps(out["findings"], indent=1)


@pytest.mark.long
@needs_monitoring
@pytest.mark.parametrize("target", TARGETS)
def test_fuzz_long(target):
    out = _fuzz(target, LONG, int.from_bytes(os.urandom(4), "little"))
    assert not out["findings"], json.dumps(out["findings"], indent=1)
