"""TRACE end to end: the gateway asks a session path how one command fared.

Whole networks on the fake clock (see test_lora_cc_sessions): a trace's
answer must agree with the simulator's ground truth (every relay's own
route log, read directly), and it must never get in the way of commands.
"""

from __future__ import annotations

import pytest

pytest.importorskip("cryptography")

from cairnmesh.frames import APP, ROUTED, FrameKind, TraceRecord
from cairnmesh.pathview import OK, records_view, trace_view
from cairnmesh.secure import open_sealed

from _session_helpers import AEAD, LINE, build, open_session, run

G = 0x42
TRACE_ON_AIR = FrameKind.TRACE | ROUTED  # 0x4B
QUIET = {"auto_trace": False, "idle_timeout": None}


def command(clock, network, controller, target, value=1):
    future = network.sessions.request(target, 3, value)
    run(clock, network, controller, 30.0, future.done)
    return future.result(timeout=0), network.sessions.deliveries(target)[-1]


def truth(network, delivery, target=5):
    return records_view(
        G, delivery.route, target, network.route_records(), delivery.sequence, delivery.epoch
    )


def hop_figures(view):
    return [
        (hop.sender, hop.receiver, hop.state, hop.repeats_out, hop.repeats_back, hop.rssi_out, hop.rssi_back)
        for hop in view.hops
    ]


def traced(seed, **gateway_options):
    """A clean command on LINE to 5, then a trace of it by hand."""

    clock, network, controller = build(LINE, seed=seed, gateway_options={**QUIET, **gateway_options})
    session = open_session(clock, network, controller, 5)
    outcome, delivery = command(clock, network, controller, 5)
    assert outcome.ok and not delivery.anomalous
    assert network.sessions.trace(5)
    run(clock, network, controller, 10.0, lambda: delivery.trace is not None)
    return clock, network, controller, session, delivery


@pytest.fixture
def clean_trace():
    return traced(seed=2)


def test_a_manual_trace_reports_every_hop_as_the_relays_saw_it(clean_trace):
    clock, network, controller, session, delivery = clean_trace
    assert session.route == (1, 2, 3, 4)
    assert delivery.trace is not None and delivery.trace.route == session.route
    assert set(delivery.trace.records) == {1, 2, 3, 4, 5, G}
    assert all(isinstance(record, TraceRecord) for record in delivery.trace.records.values())
    view = trace_view(network.sessions, 5, delivery)
    assert view is not None and view.delivery is delivery
    assert hop_figures(view) == hop_figures(truth(network, delivery))
    assert [hop.state for hop in view.hops] == [OK] * 5
    assert all(hop.repeats_out == 0 and hop.repeats_back == 0 for hop in view.hops)
    assert all(hop.rssi_out == -104 and hop.rssi_back == -104 for hop in view.hops)
    assert trace_view(network.sessions, 5) is not None  # the last traced command by default
    assert network.sessions.stats["traces"] == 1
    assert any(event.startswith("追踪结果到达（5/5") for _, event, _ in delivery.events)


def test_the_last_hop_into_the_gateway_is_acknowledged_by_its_echo(clean_trace):
    clock, network, controller, session, delivery = clean_trace
    first = session.route[0]
    log = network.route_records()[first]
    reply = log[(FrameKind.REPLY, 5, G, delivery.sequence, delivery.epoch)]
    assert reply.sends == 1 and reply.repeats == 0
    assert reply.state == "acked" and reply.ack_from == G  # the gateway's echo
    assert delivery.trace.records[first].back_repeats == 0
    assert trace_view(network.sessions, 5, delivery).hops[0].repeats_back == 0


@pytest.mark.parametrize("trace_repeats", [False, True])
def test_clean_commands_are_never_traced(trace_repeats):
    clock, network, controller = build(
        LINE, seed=7, record=True, gateway_options={"trace_repeats": trace_repeats}
    )
    open_session(clock, network, controller, 5)
    start = len(network.medium.log)
    for value in range(4):
        outcome, delivery = command(clock, network, controller, 5, value)
        assert outcome.ok and not delivery.anomalous
        run(clock, network, controller, 2.0)
    run(clock, network, controller, 3.0)
    assert network.sessions.stats["traces"] == 0
    kinds = {open_sealed(AEAD, raw)[0][0] for _, _, raw in network.medium.log[start:]}
    assert TRACE_ON_AIR not in kinds and FrameKind.REQUEST | ROUTED | APP in kinds


def first_anomalous(seeds, **gateway_options):
    """The first seed whose LINE session, at 5 % loss, has a command that needed repeats."""

    for seed in seeds:
        clock, network, controller = build(
            LINE, seed=seed, drop=0.05, gateway_options=gateway_options
        )
        session = network.sessions.open_session(5)
        run(clock, network, controller, 20.0, lambda: session.state == "open")
        if session.state != "open":
            continue
        run(clock, network, controller, 0.5)
        for value in range(10):
            outcome, delivery = command(clock, network, controller, 5, value)
            if delivery.anomalous and delivery.route == session.route:
                return clock, network, controller, session, outcome, delivery
            run(clock, network, controller, 1.0)
    pytest.fail(f"no anomalous command on seeds {list(seeds)}")


def test_a_command_that_needed_repeats_is_traced_by_itself():
    clock, network, controller, session, outcome, delivery = first_anomalous(
        range(1, 6), trace_repeats=True
    )
    assert outcome.ok
    finished = clock.now
    run(clock, network, controller, 5.0, lambda: delivery.trace is not None)
    assert delivery.trace is not None
    assert clock.now - finished >= network.sessions.trace_delay
    view = trace_view(network.sessions, 5, delivery)
    assert hop_figures(view) == hop_figures(truth(network, delivery))
    repeats = sum(hop.repeats_out + hop.repeats_back for hop in view.hops)
    assert repeats > 0  # the trace shows where the time went
    assert network.sessions.stats["traces"] == 1


def test_by_default_mere_repeats_do_not_cost_a_trace():
    clock, network, controller, session, outcome, delivery = first_anomalous(range(1, 6))
    assert outcome.ok and not delivery.faulty
    run(clock, network, controller, 5.0)
    assert delivery.trace is None and network.sessions.stats["traces"] == 0


def test_a_request_during_a_trace_waits_for_it_and_runs_once():
    clock, network, controller = build(LINE, seed=2, gateway_options=QUIET)
    session = open_session(clock, network, controller, 5)
    outcome, traced_delivery = command(clock, network, controller, 5, 1)
    assert outcome.ok
    assert network.sessions.trace(5)
    run(clock, network, controller, 5.0, lambda: session.trace_id is not None)
    assert session.trace_id is not None

    future = network.sessions.request(5, 3, 2)
    run(clock, network, controller, 0.05)
    assert session.waiting is not None  # one exchange at a time on the path
    run(clock, network, controller, 30.0, future.done)
    assert future.result(timeout=0).ok
    assert network.devices[5].handled_count == 2  # once for each command
    assert traced_delivery.trace is not None
    later = network.sessions.deliveries(5)[-1]
    assert later is not traced_delivery and later.ok
    assert later.sent_at >= traced_delivery.trace.received_at


def test_a_trace_whose_answer_is_lost_gives_up_quietly():
    clock, network, controller = build(LINE, seed=2, gateway_options=QUIET)
    session = open_session(clock, network, controller, 5)
    outcome, delivery = command(clock, network, controller, 5)
    assert outcome.ok
    assert network.sessions.trace(5)
    run(clock, network, controller, 5.0, lambda: network.relays[5].stats["traces_answered"] == 1)
    assert session.trace_id is not None
    network.set_power(1, False)  # the answer dies on its way back, past relay 2
    deadline = session.trace_deadline
    run(clock, network, controller, 10.0, lambda: session.trace_id is None)
    assert session.trace_id is None and session.trace_of is None
    assert delivery.trace is None
    events = [event for _, event, _ in delivery.events]
    assert "发出追踪" in events and "追踪无回应" in events
    assert deadline <= clock.now <= deadline + 0.05  # given up at its deadline, not before
    assert trace_view(network.sessions, 5, delivery) is None


def test_a_trace_into_a_dead_target_still_ends():
    clock, network, controller = build(LINE, seed=2, gateway_options=QUIET)
    session = open_session(clock, network, controller, 5)
    outcome, delivery = command(clock, network, controller, 5)
    assert outcome.ok
    assert network.sessions.trace(5)
    run(clock, network, controller, 5.0, lambda: session.trace_id is not None)
    network.set_power(5, False)
    run(clock, network, controller, 15.0)
    assert session.state != "open"  # the last relay reported it could not reach 5
    assert session.trace_id is None and delivery.trace is None
    assert any(event == "追踪中断（路径断了）" for _, event, _ in delivery.events)
