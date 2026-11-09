"""Path diagnostics: relays' route logs, the gateway's free view, the hop table."""

from __future__ import annotations

import pytest

pytest.importorskip("cryptography")

from cairnmesh.frames import FrameKind
from cairnmesh.mesh import HopRecord
from cairnmesh.pathview import (
    OK,
    STUCK,
    UNREACHED,
    gateway_view,
    records_view,
    render_path,
)

from _session_helpers import LINE, build, open_session, run

G = 0x42


def finished(clock, network, controller, target, value=1):
    future = network.sessions.request(target, 3, value)
    run(clock, network, controller, 30.0, future.done)
    return future.result(timeout=0), network.sessions.deliveries(target)[-1]


def test_every_hop_is_seen_with_its_strength_and_repeats_when_all_goes_well():
    clock, network, controller = build(LINE, seed=2)
    session = open_session(clock, network, controller, 5)
    outcome, delivery = finished(clock, network, controller, 5)
    assert outcome.ok and delivery.ok and delivery.first_hop_sends == 1
    truth = records_view(G, session.route, 5, network.route_records(), delivery.sequence, delivery.epoch)
    assert [hop.state for hop in truth.hops] == [OK] * 5
    assert all(hop.repeats_out == 0 and hop.repeats_back == 0 for hop in truth.hops)
    # 80 m hops on the simulator's linear RSSI: -40 - 80 * 0.8 = -104 dBm, both ways
    assert all(hop.rssi_out == pytest.approx(-104) for hop in truth.hops)
    assert all(hop.rssi_back == pytest.approx(-104) for hop in truth.hops[1:])

    free = gateway_view(network.sessions, 5)
    assert free.hops[0].repeats_out == 0 and free.hops[0].rssi_back == pytest.approx(-104)
    assert free.hops[1].repeats_out is None  # only a trace could tell
    assert "G→01" in render_path(free)


def test_a_dead_relay_shows_where_the_message_stuck():
    clock, network, controller = build(LINE, seed=3, gateway_options={"idle_timeout": None})
    controller.confirm.window = None  # the path view of the command itself
    session = open_session(clock, network, controller, 5)
    route = session.route
    network.set_power(3, False)  # on a line nobody can go round it
    outcome, delivery = finished(clock, network, controller, 5)
    assert not outcome.ok and delivery.ok is False
    assert delivery.broken_at == (2, 3) and delivery.fell_back

    truth = records_view(G, route, 5, network.route_records(), delivery.sequence, delivery.epoch)
    assert [hop.state for hop in truth.hops] == [OK, OK, STUCK, UNREACHED, UNREACHED]
    # Every repeat the hop allows (``route_retries``), then it gave up.
    assert truth.stuck_at.sender == 2 and truth.hops[2].repeats_out == network.relays[2].route_retries

    free = gateway_view(network.sessions, 5)
    assert free.stuck_at is not None and (free.stuck_at.sender, free.stuck_at.receiver) == (2, 3)
    assert free.stuck_at.errors == 1
    text = render_path(free)
    assert "✖" in text and "断在 02→03" in text


def record(kind, sender, target, node_route, index, **fields):
    return HopRecord(kind, sender, target, 9, 7, node_route, index, **fields)


def test_a_lost_answer_is_placed_on_its_hop_on_the_way_back():
    out_route = (G, 1, 2, 5)
    back_route = (5, 2, 1, G)
    req, rep = FrameKind.REQUEST, FrameKind.REPLY

    def log(*records):
        return {(r.kind, r.sender, r.target, r.sequence, r.epoch): r for r in records}

    records = {
        G: log(record(req, G, 5, out_route, 0, sends=1, state="acked")),
        1: log(
            record(req, G, 5, out_route, 1, prev_rssi=-90.0, sends=1, state="answered"),
        ),
        2: log(
            record(req, G, 5, out_route, 2, prev_rssi=-91.0, sends=1, state="answered"),
            record(rep, 5, G, back_route, 1, prev_rssi=-92.0, sends=3, state="gave_up"),
        ),
        5: log(
            record(req, G, 5, out_route, 3, prev_rssi=-93.0, state="delivered"),
            record(rep, 5, G, back_route, 0, sends=1, state="sent"),
        ),
    }
    view = records_view(G, (1, 2), 5, records, 9, 7)
    # the request got through; its answer died between 2 and 1, so 1 and G never saw it
    assert [hop.state for hop in view.hops] == [UNREACHED, STUCK, OK]
    assert view.hops[1].repeats_back == 2 and "回答" in view.hops[1].note
    assert view.hops[0].note == "回答未到"


def test_a_node_the_trace_answer_skipped_is_shown_as_not_reported_not_as_lost():
    from cairnmesh.frames import TRACE_DONE, TraceRecord
    from cairnmesh.pathview import NO_REPORT, reports_view

    done = TraceRecord(rssi_out=-90, rssi_back=-90, out_state=TRACE_DONE, back_state=TRACE_DONE)
    # G → 1 → 2 → 3, and the answer came back past 1 (it reports nothing).
    reports = {G: done, 2: done, 3: done}
    view = reports_view(G, (1, 2), 3, reports, reported=True)
    assert [hop.state for hop in view.hops] == [NO_REPORT, OK, OK]
    assert view.stuck_at is None
    assert "未回报" in view.hops[0].note and "未回报" in render_path(view)
    # The same records as ground truth (every node is there): 1 did not get it.
    truth = reports_view(G, (1, 2), 3, reports)
    assert truth.hops[0].state == STUCK


def test_a_silent_node_before_one_that_did_not_receive_leaves_the_break_unknown():
    from cairnmesh.frames import TRACE_DONE, TraceRecord
    from cairnmesh.pathview import NO_REPORT, reports_view

    done = TraceRecord(rssi_out=-90, rssi_back=-90, out_state=TRACE_DONE, back_state=TRACE_DONE)
    reports = {G: done, 2: TraceRecord()}  # 1 silent, 2 never got the request
    view = reports_view(G, (1, 2), 3, reports, reported=True)
    assert [hop.state for hop in view.hops] == [NO_REPORT, NO_REPORT, UNREACHED]
