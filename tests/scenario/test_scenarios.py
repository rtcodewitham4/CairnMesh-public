"""Extreme-case integrity hunts on the simulator (``-m slow``; more seeds with ``-m long``).

Every command must end certain and right: never run twice, never DONE without
running, never NOT_RUN and run (``extreme_1022``); RESULT frames lost for
hours must not leave the device's open records full (``result_loss``); one
nonce never seals two different frames (``nonce_audit``). ``gate full`` runs
the slow ones; ``CAIRNMESH_LONG_SEEDS`` (default 16) sets the long run's seeds.
"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("cryptography")

import extreme_1022 as X  # noqa: E402
import scenarios  # noqa: E402
from cairnmesh import simpool  # noqa: E402

LONG_SEEDS = int(os.environ.get("CAIRNMESH_LONG_SEEDS", "16"))


def _hunt(names: list[str], seeds: list[int]) -> None:
    # One pool for every scenario: the long jobs (wrap, restart_wrap) overlap the short ones.
    jobs = [(name, args) for name in names for args in X.SCENARIOS[name][1](seeds)]
    jobs.sort(key=lambda job: job[0] in ("wrap", "restart_wrap"), reverse=True)
    defects = []
    with simpool.pool() as pool:
        for name, args, out in pool.map(X._run_one, jobs):
            bad = {key: value for key, value in out["bad"].items() if value}
            if bad:
                defects.append((name, args, bad, out.get("examples"), out.get("error")))
    assert not defects, defects


@pytest.mark.slow
def test_extreme():
    _hunt(list(X.SCENARIOS), [0])  # about 4 min on 16 cores


@pytest.mark.long
def test_extreme_long():
    _hunt(list(X.SCENARIOS), list(range(1, LONG_SEEDS + 1)))


@pytest.mark.slow
def test_lost_results_do_not_fill_the_open_records():
    out = scenarios.result_loss("random40", 0)
    assert not any(out["bad"].values()), out["bad"]
    assert out["open_records"] == 0 and out["refused_after_channel_ok"] == 0, out


@pytest.mark.slow
@pytest.mark.parametrize("name", ["slow_chaos", "loss"])
def test_a_nonce_never_seals_two_frames(name):
    out = scenarios.nonce_audit(name, 0)
    assert out["nonces"] > 0 and out["nonce_clash"] == 0, out["clash_example"]
