"""Robustness invariants under faults (fast; the slow survey is ``robustness_bench``).

Small layouts on the disc model (``tests/conftest.py``): at-most-once under
heavy loss, replayed and tampered frames never executed, a jammer's garbage
never taken for a command.
"""

from __future__ import annotations

import random

import pytest

pytest.importorskip("cryptography")

from _session_helpers import LADDER, LINE, build, open_session, run  # noqa: E402

OUTSIDER = 0xEE


def executions(network, target: int) -> list[int]:
    executed: list[int] = []
    device = network.devices[target]
    original = device.perform_action

    def perform(frame, original=original):
        executed.append(frame.message_id)
        return original(frame)

    device.perform_action = perform
    return executed


def commands(clock, network, controller, target: int, count: int) -> list[bool]:
    out = []
    for value in range(count):
        future = network.sessions.request(target, 3, value)
        run(clock, network, controller, 30.0, future.done)
        out.append(future.done() and future.result(timeout=0).ok)
        run(clock, network, controller, 0.3)
    return out


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_at_most_once_holds_under_twenty_percent_loss(seed):
    clock, network, controller = build(LADDER, seed=seed, drop=0.20)
    executed = executions(network, 5)
    open_session(clock, network, controller, 5)
    ok = commands(clock, network, controller, 5, 10)
    assert len(executed) == len(set(executed))  # nothing ran twice
    assert sum(ok) <= len(set(executed))  # every success did run
    assert sum(ok) >= 7  # and most got through


def _captured_requests(network, since: int) -> list[bytes]:
    return [data for _, _, data in network.medium.log[since:] if data and data[0] & 0x1F == 0x01]


def test_replayed_commands_are_not_run_again_inside_or_past_the_dedup_window():
    clock, network, controller = build(LINE, seed=3, record=True)
    executed = executions(network, 5)
    open_session(clock, network, controller, 5)
    mark = len(network.medium.log)
    assert all(commands(clock, network, controller, 5, 3))
    captured = _captured_requests(network, mark)
    assert captured
    before = len(executed)
    attacker = network.medium.add(OUTSIDER, network.medium.port(5).position)
    for wait in (0.0, 40.0):  # at once, then 40 s later (the FCNT window refuses both)
        run(clock, network, controller, wait)
        for data in captured:
            attacker.send(data)
            run(clock, network, controller, 0.5)
    run(clock, network, controller, 5.0)
    assert len(executed) == before


def test_tampered_copies_are_rejected():
    clock, network, controller = build(LINE, seed=3, record=True)
    executed = executions(network, 5)
    open_session(clock, network, controller, 5)
    mark = len(network.medium.log)
    assert all(commands(clock, network, controller, 5, 2))
    before = len(executed)
    attacker = network.medium.add(OUTSIDER, network.medium.port(5).position)
    for data in _captured_requests(network, mark):
        for index in (1, len(data) // 2, len(data) - 1):  # header, body, MIC
            raw = bytearray(data)
            raw[index] ^= 0x01
            attacker.send(bytes(raw))
            run(clock, network, controller, 0.5)
    run(clock, network, controller, 5.0)
    assert len(executed) == before


def test_a_jammers_garbage_is_never_run_and_commands_still_get_through():
    clock, network, controller = build(LINE, seed=3)
    executed = executions(network, 5)
    jammer = network.medium.add(OUTSIDER, network.medium.port(3).position)
    rng = random.Random(4)
    original = network.tick
    due = [0.0]

    def tick(now: float) -> None:
        if now >= due[0]:
            jammer.send(bytes(rng.randrange(256) for _ in range(40)))
            due[0] = now + rng.uniform(0.5, 1.5)
        original(now)

    network.tick = tick
    open_session(clock, network, controller, 5)
    ok = commands(clock, network, controller, 5, 10)
    assert len(executed) == len(set(executed)) == sum(ok)  # only real commands ran, once each
    assert sum(ok) >= 8
