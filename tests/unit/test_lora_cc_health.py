"""Layer 2: maintenance notices when a node's deployment stops holding in operation (``Gateway.health``)."""

from __future__ import annotations

import pytest

pytest.importorskip("cryptography")

from cairnmesh.events import HealthChanged
from cairnmesh.gateway import Acceptance, Health

from _session_helpers import LINE, build, open_session, run

QUIET = {"auto_trace": False, "idle_timeout": None}


def notices(controller) -> list:
    seen = []
    controller.events.subscribe(lambda event: isinstance(event, HealthChanged) and seen.append(event))
    return seen


def sends(gateway, target: int, frames: int, sends_each: int, through: bool = True) -> None:
    for _ in range(frames):
        gateway._note_attempts(target, sends_each, through)


def test_the_design_loss_raises_nothing_and_more_raises_a_notice():
    clock, network, controller = build(LINE, seed=2, gateway_options=QUIET)
    gateway, seen = network.sessions, notices(controller)
    # 30 % per frame: a send fails 51 % of the time (frame or acknowledgement lost)
    for k in range(100):  # spread out, as random loss is: 37 is prime to 100, so 49 of the 100 go through
        gateway._note_attempts(5, 1, (k * 37) % 100 >= 51)
    assert gateway.health[5].ok and not seen
    loss, low = gateway.health[5].interference()
    assert loss == pytest.approx(0.30, abs=0.01) and low < 0.30
    sends(gateway, 5, 50, 3, through=False)  # 50 % per frame and worse
    assert "interference" in gateway.health[5].reasons and not seen[-1].ok
    sends(gateway, 5, 100, 1)  # clean again: the window forgets
    assert gateway.health[5].ok and seen[-1].ok


def test_an_unreachable_node_is_noticed_after_two_failed_probe_rounds_only():
    clock, network, controller = build(LINE, seed=2, gateway_options={**QUIET, "reprobe_after": 5.0})
    gateway, seen = network.sessions, notices(controller)
    network.set_power(5, False)
    gateway.open_session(5)
    run(clock, network, controller, 120.0, lambda: bool(seen))
    session = gateway.session(5)
    assert session.counts["probes"] >= 2 * (gateway.probe_retries + 1)  # two rounds, not one
    assert seen and "unreachable" in gateway.health[5].reasons
    network.set_power(5, True)
    run(clock, network, controller, 60.0, lambda: session.state == "open")
    assert gateway.health[5].ok


def test_a_path_that_keeps_failing_acceptance_is_noticed_and_clears_when_it_passes():
    clock, network, controller = build(LINE, seed=2, gateway_options=QUIET)
    gateway, seen = network.sessions, notices(controller)
    verdict = {"passed": False}
    gateway.assess = lambda target, test=None: Acceptance(target, clock.now, verdict["passed"], reasons=("03→04 余量 +1.0 dB、绕行 0 条",))
    open_session(clock, network, controller, 5)
    assert gateway.health[5].ok  # once is ordinary at 30 % loss
    gateway.close_session(5)
    open_session(clock, network, controller, 5)
    assert "path" in gateway.health[5].reasons and "03→04" in gateway.health[5].reasons["path"]
    verdict["passed"] = True
    gateway.close_session(5)
    open_session(clock, network, controller, 5)
    assert gateway.health[5].ok and seen[-1].ok


def test_a_notice_does_not_hold_back_commands():
    clock, network, controller = build(LINE, seed=2, gateway_options={**QUIET, "require_acceptance": True})
    gateway = network.sessions
    gateway.acceptance[5] = Acceptance(5, clock.now, True)
    gateway.health[5] = Health(5, reasons={"path": "x"})
    future = gateway.request(5, 3, 1)
    run(clock, network, controller, 60.0, future.done)
    assert future.result(timeout=0).ok
