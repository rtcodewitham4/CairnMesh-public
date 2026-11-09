"""Deployment acceptance: a node is commanded only once its path can give a certain outcome (``Gateway.accept``)."""

from __future__ import annotations

import pytest

pytest.importorskip("cryptography")

from cairnmesh.events import AcceptanceFinished, LinkTestFinished
from cairnmesh.frames import LinkQuality, ProbeHop
from cairnmesh.gateway import ACCEPT_DETOUR_DB, Acceptance, KnownNode, NotAccepted

from _session_helpers import G, LINE, build, open_session, run

QUIET = {"auto_trace": False, "idle_timeout": None}


def accepted(**gateway_options):
    clock, network, controller = build(LINE, seed=2, gateway_options={**QUIET, **gateway_options})
    events = []
    order = []
    controller.events.subscribe(lambda event: isinstance(event, AcceptanceFinished) and events.append(event))
    controller.events.subscribe(
        lambda event: order.append(type(event).__name__)
        if isinstance(event, (LinkTestFinished, AcceptanceFinished))
        else None
    )
    network.sessions.accept(5, rounds=5)
    run(clock, network, controller, 120.0, lambda: 5 in network.sessions.acceptance)
    return clock, network, controller, events, order


def snr(margin_db: float) -> LinkQuality:
    """A link ``margin_db`` over the SF7 limit (-7.5 dB SNR)."""

    return LinkQuality(-100, round((margin_db - 7.5) * 4))


def test_a_strong_line_passes_and_says_so():
    clock, network, controller, events, order = accepted()
    result = network.sessions.acceptance[5]
    assert result.passed and not result.reasons
    assert result.path == (G, 1, 2, 3, 4, 5) and all(hop.ok for hop in result.hops)
    assert [event.acceptance for event in events] == [result]
    assert order == ["LinkTestFinished", "AcceptanceFinished"]


def test_a_weak_hop_without_a_detour_fails_and_names_the_place_to_fix():
    clock, network, controller = build(LINE, seed=2, gateway_options=QUIET)
    session = open_session(clock, network, controller, 5)
    session.links = (snr(20.0), snr(20.0), snr(2.0), snr(20.0), snr(20.0))  # 2→3 is thin
    result = network.sessions.assess(5)
    assert not result.passed
    assert [(hop.sender, hop.receiver) for hop in result.hops if not hop.ok] == [(2, 3)]
    assert "02→03" in result.reasons[0] and "02 与 03 之间补中继" in result.remedies[0]


def test_a_thin_hop_with_a_known_detour_passes():
    clock, network, controller = build(LINE, seed=2, gateway_options=QUIET)
    session = open_session(clock, network, controller, 5)
    session.links = (snr(20.0), snr(20.0), snr(2.0), snr(20.0), snr(20.0))
    good = snr(ACCEPT_DETOUR_DB + 1.0)
    # node 9 (off the path) hears both 2 and 3 well, as a scan would tell
    network.sessions.nodes[9] = KnownNode(9, (), (), clock.now, "scan", neighbours=(ProbeHop(2, good), ProbeHop(3, good)))
    result = network.sessions.assess(5)
    assert result.passed
    assert next(hop for hop in result.hops if hop.sender == 2).detours == 1


def test_with_acceptance_required_only_accepted_nodes_take_commands():
    clock, network, controller = build(LINE, seed=2, gateway_options={**QUIET, "require_acceptance": True})
    gateway = network.sessions
    with pytest.raises(NotAccepted):
        gateway.request(5, 3, 1)
    forced = gateway.request(5, 3, 1, force=True)  # the operator's own call
    run(clock, network, controller, 60.0, forced.done)
    gateway.accept(5, rounds=5)
    run(clock, network, controller, 120.0, lambda: 5 in gateway.acceptance)
    assert gateway.acceptance[5].passed
    future = gateway.request(5, 3, 2)
    run(clock, network, controller, 60.0, future.done)
    assert future.result(timeout=0).ok
    gateway.acceptance[5] = Acceptance(5, clock.now, False, reasons=("x",))
    with pytest.raises(NotAccepted, match="x"):
        gateway.request(5, 3, 3)
