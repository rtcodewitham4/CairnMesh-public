"""Link test: keepalive round trips on a session tell how well a node talks (``Gateway.test_link``)."""

from __future__ import annotations

import pytest

pytest.importorskip("cryptography")

from cairnmesh.events import LinkTestFinished
from cairnmesh.gateway import LINK_DOWN, LINK_GOOD, LINK_LOSSY, LINK_WEAK_SIGNAL, LinkTest

from _session_helpers import LINE, build, run

QUIET = {"auto_trace": False, "idle_timeout": None}


def finished_test(rounds=5, **gateway_options):
    clock, network, controller = build(LINE, seed=2, gateway_options={**QUIET, **gateway_options})
    events = []
    controller.events.subscribe(lambda event: isinstance(event, LinkTestFinished) and events.append(event))
    test = network.sessions.test_link(5, rounds=rounds)
    run(clock, network, controller, 120.0, lambda: test.finished is not None)
    return clock, network, controller, test, events


def test_a_clean_line_answers_every_round_and_runs_nothing():
    clock, network, controller, test, events = finished_test()
    assert test.done == 5 and test.answered == 5 and test.loss == 0.0 and not test.stopped
    assert test.verdict == LINK_GOOD
    assert test.path == (network.sessions.controller_id, *network.sessions.session(5).route, 5)
    assert len(test.margins) == len(test.path) - 1
    assert 0 < test.rtt(0.5) <= test.rtt(0.95) <= test.worst
    assert network.devices[5].handled_count == 0  # keepalives only: no action ran
    assert [event.test for event in events] == [test]


def test_a_command_during_the_test_keeps_priority():
    clock, network, controller = build(LINE, seed=2, gateway_options=QUIET)
    test = network.sessions.test_link(5, rounds=6)
    run(clock, network, controller, 60.0, lambda: test.done >= 2)
    future = network.sessions.request(5, 3, 1)
    run(clock, network, controller, 60.0, lambda: future.done() and test.finished is not None)
    assert future.result(timeout=0).ok
    assert network.devices[5].handled_count == 1
    assert test.done == 6 and test.answered == 6


def test_an_unreachable_target_ends_the_test_as_down():
    clock, network, controller = build(LINE, seed=2, gateway_options={**QUIET, "test_patience": 30.0})
    network.set_power(5, False)
    test = network.sessions.test_link(5, rounds=5)
    run(clock, network, controller, 60.0, lambda: test.finished is not None)
    assert test.finished is not None and test.stopped
    assert test.verdict == LINK_DOWN


def made(results, margins=(10.0, 10.0), sf=7, breaks=0):
    test = LinkTest(9, len(results), 0.0, 0.5, 4.0, path=(0x42, 1, 9), margins=margins, sf=sf, breaks=breaks)
    test.results = [(float(k), rtt, sf) for k, rtt in enumerate(results)]
    return test


def test_the_verdict_tells_a_weak_signal_from_loss_with_margin_to_spare():
    lossy = [1.0] * 8 + [None] * 2
    assert made([1.0] * 10).verdict == LINK_GOOD
    assert made(lossy).verdict == LINK_LOSSY
    assert made([1.0] * 10, breaks=1).verdict == LINK_LOSSY
    assert made(lossy, margins=(10.0, -2.0), sf=8).verdict == LINK_WEAK_SIGNAL  # +0.5 dB at SF8
    assert made([None] * 4).verdict == LINK_DOWN
    assert made([1.0] * 10, margins=(10.0, -2.0), sf=8).verdict == LINK_GOOD  # thin but it held


def test_a_thin_sf7_margin_the_session_slowed_down_for_is_not_a_weak_signal():
    # Measured: judged on the SF7 margin, random 40 under SNR-blind loss came out "weak signal" 18/24.
    test = made([1.0] * 8 + [None] * 2, margins=(10.0, 0.0), sf=9)
    assert test.session_margin == pytest.approx(5.0)
    assert test.verdict == LINK_LOSSY
