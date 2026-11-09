"""``LinkSession.mesh``: the facade the TUI drives the multi-hop simulator through.

Everything the TUI relies on is pinned here: the routed-session verbs
(``open_session`` / ``close_session`` / ``trace`` / ``link_info`` /
``path_view`` / ``advance``), the ``SessionChanged`` and ``TraceArrived``
events, and the model-based timeouts of ``request`` / ``wake``. All on the
virtual clock (``session.advance``), small topologies and fixed seeds; the
one threaded test is the smoke test of the background loop.
"""

from __future__ import annotations

import time

import pytest

pytest.importorskip("cryptography")

from cairnmesh.events import NodeReported, SessionChanged, TraceArrived
from cairnmesh.frames import ROUTED, FrameKind, ReportEvent
from cairnmesh.mesh_sim import ladder, line
from cairnmesh.pathview import OK, render_path
from cairnmesh.session import LinkSession

KEY = bytes(range(16))


def mesh(positions, **options) -> LinkSession:
    options.setdefault("key", KEY)
    options.setdefault("seed", 2)
    return LinkSession.mesh(positions, **options)


def record(session: LinkSession) -> list:
    events: list = []
    session.events.subscribe(events.append)
    return events


def open_link(session: LinkSession, node: int, limit: float = 20.0, settle: float = 0.5) -> None:
    """Open the session with ``node`` and run until it is up (then ``settle`` seconds more)."""

    session.open_session(node)
    waited = 0.0
    while waited < limit:
        info = session.link_info(node)
        if info is not None and info.state == "open":
            break
        session.advance(0.1)
        waited += 0.1
    assert session.link_info(node).state == "open", "the session never came up"
    if settle:
        session.advance(settle)


def finish(session: LinkSession, future, limit: float = 10.0):
    waited = 0.0
    while not future.done() and waited < limit:
        session.advance(0.1)
        waited += 0.1
    assert future.done(), "the request never finished"
    return future.result(timeout=0)


def session_events(events: list, node: int) -> list[SessionChanged]:
    return [e for e in events if isinstance(e, SessionChanged) and e.node_id == node]


def hop_figures(view) -> list[tuple]:
    return [(hop.rssi_out, hop.repeats_out, hop.repeats_back) for hop in view.hops]


# ------------------------------------------------------------ opening a session


def test_opening_a_session_returns_probing_and_the_path_comes_up():
    session = mesh(line(5))
    assert session.multihop
    assert session.link_info(5) is None
    assert session.open_session(5) == "probing"
    assert session.link_info(5).state == "probing"
    open_link(session, 5)
    assert session.link_info(5).state == "open"


def test_opening_a_session_again_keeps_the_one_already_there():
    session = mesh(line(5))
    events = record(session)
    open_link(session, 5)
    assert session.open_session(5) == "open"
    assert [e.state for e in session_events(events, 5)] == ["probing", "open"]


def test_an_open_five_hop_session_reports_the_route_and_the_latency_models_figures():
    session = mesh(line(5))
    open_link(session, 5)
    info = session.link_info(5)
    gateway_session = session.gateway.session(5)
    model = session.gateway.model.command(5, session.gateway.expected_loss)
    assert info.node_id == 5 and info.state == "open"
    assert info.route == (1, 2, 3, 4)
    assert info.hops == 5
    assert info.expected == pytest.approx(model.expected)
    assert info.worst == pytest.approx(model.worst)
    assert (info.expected, info.worst) == (gateway_session.latency.expected, gateway_session.latency.worst)
    assert info.within_target is True
    assert info.realtime_target == 5.0
    assert 0.0 <= info.age < 1.0
    assert info.idle_timeout == session.gateway.idle_timeout
    assert not info.tracing
    assert info.counts["established"] == 1 and info.counts["probes"] >= 1


def test_session_events_come_probing_then_open_with_the_route_and_figures():
    session = mesh(line(5))
    events = record(session)
    open_link(session, 5)
    changes = session_events(events, 5)
    assert [e.state for e in changes] == ["probing", "open"]
    opened = changes[1]
    info = session.link_info(5)
    assert opened.route == (1, 2, 3, 4)
    assert opened.setup is not None and opened.setup > 0
    assert opened.within_target is True
    assert opened.realtime_target == 5.0
    assert opened.expected == pytest.approx(info.expected)
    assert opened.worst == pytest.approx(info.worst)


def test_a_ten_hop_session_misses_the_five_second_target_in_info_and_event():
    session = mesh(line(10))
    events = record(session)
    open_link(session, 10, limit=30.0)
    info = session.link_info(10)
    opened = session_events(events, 10)[-1]
    assert info.hops == 10
    assert info.worst > 5.0
    assert info.within_target is False
    assert opened.state == "open" and opened.within_target is False
    assert opened.worst > 5.0 and opened.realtime_target == 5.0


# --------------------------------------------------------------------- commands


def test_a_request_crosses_the_session_route_once_in_about_the_expected_time():
    session = mesh(line(5), drop=0.0)
    open_link(session, 5)
    outcome = finish(session, session.request(5, 3, 1))
    assert outcome.ok and outcome.attempts == 1
    (delivery,) = session.gateway.deliveries(5)
    assert delivery.route == session.link_info(5).route == (1, 2, 3, 4)
    assert not delivery.fell_back
    assert delivery.ok is True
    assert outcome.elapsed == pytest.approx(delivery.expected, rel=0.10)


def test_a_wake_resolves_ok_over_the_session():
    session = mesh(line(5))
    open_link(session, 5)
    outcome = finish(session, session.wake(5))
    assert outcome.ok
    (delivery,) = session.gateway.deliveries(5)
    assert delivery.route == (1, 2, 3, 4) and not delivery.fell_back


def test_commands_without_a_session_still_get_answered_by_flooding():
    session = mesh(line(3))
    assert finish(session, session.request(3, 3, 1), limit=20.0).ok
    assert finish(session, session.wake(3), limit=20.0).ok
    assert session.link_info(3) is None


# ----------------------------------------------------------------------- traces


def test_trace_is_refused_without_a_session_and_before_any_command():
    session = mesh(line(5))
    assert session.trace(5) is False
    open_link(session, 5)
    assert session.trace(5) is False
    assert not session.link_info(5).tracing


def test_a_trace_after_a_command_is_wanted_at_once_and_arrives_as_one_event():
    session = mesh(line(5))
    events = record(session)
    open_link(session, 5)
    assert finish(session, session.request(5, 3, 1)).ok
    assert session.trace(5) is True
    assert session.link_info(5).tracing is True
    session.advance(10.0)
    arrived = [e for e in events if isinstance(e, TraceArrived)]
    assert len(arrived) == 1
    assert arrived[0].node_id == 5
    assert arrived[0].answered == arrived[0].expected == 5
    assert arrived[0].sequence == session.gateway.deliveries(5)[-1].sequence
    assert not session.link_info(5).tracing


def test_the_trace_view_matches_the_simulators_ground_truth():
    session = mesh(line(5))
    open_link(session, 5)
    assert finish(session, session.request(5, 3, 1)).ok
    assert session.trace(5)
    # Compare as soon as the answer is in: the nodes' route logs are short
    # (max_route_log entries), so keepalives push an old command out of them.
    waited = 0.0
    while session.path_view(5, "trace") is None and waited < 10.0:
        session.advance(0.1)
        waited += 0.1
    trace = session.path_view(5, "trace")
    truth = session.path_view(5, "truth")
    assert trace is not None and truth is not None
    assert len(trace.hops) == 5
    assert [hop.state for hop in trace.hops] == [OK] * 5
    assert [hop.state for hop in truth.hops] == [OK] * 5
    assert hop_figures(trace) == hop_figures(truth)
    assert all(hop.rssi_out is not None for hop in trace.hops)
    assert trace.delivery is truth.delivery is session.gateway.deliveries(5)[-1]
    assert trace.session_state == truth.session_state == "open"


def test_a_second_command_after_a_trace_still_runs_and_the_trace_waits_for_a_free_path():
    session = mesh(line(5))
    events = record(session)
    open_link(session, 5)
    assert finish(session, session.request(5, 3, 1)).ok
    assert session.trace(5)
    first = session.gateway.deliveries(5)[-1]
    outcome = finish(session, session.request(5, 3, 2), limit=15.0)
    assert outcome.ok
    session.advance(10.0)
    arrived = [e for e in events if isinstance(e, TraceArrived)]
    assert [e.sequence for e in arrived] == [first.sequence]  # the trace asked about the first command
    assert session.gateway.deliveries(5)[-1] is not first


# ------------------------------------------------------------------ path views


def test_the_truth_view_says_when_the_nodes_have_already_dropped_an_old_command():
    # Each node keeps only its last few routed frames; keepalives push an old
    # command out, which must not read as a path where nothing arrived.
    session = mesh(line(5))
    open_link(session, 5)
    assert finish(session, session.request(5, 3, 1)).ok
    assert session.path_view(5, "truth").notes == []
    session.advance(30.0)
    view = session.path_view(5, "truth")
    assert all(hop.repeats_out is None for hop in view.hops)
    assert any("挤出" in note for note in view.notes)


def test_the_gateway_view_has_the_first_hop_ok_and_renders_the_hop_count_and_latency():
    session = mesh(line(5))
    open_link(session, 5)
    assert finish(session, session.request(5, 3, 1)).ok
    view = session.path_view(5, "gateway")
    assert view is not None and len(view.hops) == 5
    assert view.hops[0].state == OK
    assert view.session_state == "open"
    assert view.expected == pytest.approx(session.link_info(5).expected)
    assert view.realtime_target == 5.0
    text = render_path(view)
    assert "5 跳" in text
    assert "理论往返" in text


def test_the_default_path_view_is_the_gateway_view():
    session = mesh(line(5))
    open_link(session, 5)
    default, gateway = session.path_view(5), session.path_view(5, "gateway")
    assert render_path(default) == render_path(gateway)


def test_a_bad_view_source_is_a_value_error_even_without_a_session():
    session = mesh(line(5))
    with pytest.raises(ValueError):
        session.path_view(5, "bogus")
    open_link(session, 5)
    with pytest.raises(ValueError):
        session.path_view(5, "bogus")


def test_path_views_are_none_where_there_is_nothing_to_show():
    session = mesh(line(5))
    for source in ("gateway", "trace", "truth"):
        assert session.path_view(5, source) is None  # no session
    open_link(session, 5)
    assert session.path_view(5, "gateway") is not None  # the probe's links
    assert session.path_view(5, "trace") is None  # nothing traced yet
    assert session.path_view(5, "truth") is None  # no command yet


# ---------------------------------------------------------------------- closing


def test_closing_a_session_drops_the_info_and_publishes_closed():
    session = mesh(line(5))
    events = record(session)
    open_link(session, 5)
    session.close_session(5)
    assert session.link_info(5) is None
    assert session.path_view(5, "gateway") is None
    assert [e.state for e in session_events(events, 5)] == ["probing", "open", "closed"]
    session.close_session(5)  # closing nothing is harmless
    session.advance(2.0)
    assert session.link_info(5) is None


def test_an_idle_session_is_closed_by_the_gateway_and_reported():
    session = mesh(line(3), seed=1, gateway_options={"idle_timeout": 2.0})
    events = record(session)
    open_link(session, 3, settle=0.0)
    assert session.link_info(3).idle_timeout == 2.0
    session.advance(3.0)
    assert session.link_info(3) is None
    assert [e.state for e in session_events(events, 3)][-1] == "closed"


def test_a_command_after_closing_still_works_by_flooding_and_a_reopened_session_is_new():
    session = mesh(line(3), seed=1)
    open_link(session, 3)
    session.close_session(3)
    assert finish(session, session.request(3, 3, 1), limit=20.0).ok
    assert session.open_session(3) == "probing"
    open_link(session, 3)
    assert session.link_info(3).counts["established"] == 1  # a fresh Session object


# ------------------------------------------------------------------ broken path


def test_a_dead_relay_breaks_the_session_which_reopens_around_it_and_the_request_still_runs():
    # Seed 3 is one of the many that work: 39 of 40 seeds recover on a
    # ladder. The odd one (seed 2 with the ETX metric, 10 with the old
    # widest-link one) reopens around the dead relay each time, but the
    # fallback flood and the new route collide until the controller gives up.
    session = mesh(ladder(5), seed=3)
    events = record(session)
    open_link(session, 5)
    route = session.link_info(5).route
    dead = route[len(route) // 2]
    with session.lock:
        session.simulation.set_power(dead, False)
    future = session.request(5, 3, 1)
    outcome = finish(session, future, limit=40.0)
    assert outcome.ok

    waited = 0.0
    while waited < 30.0:
        info = session.link_info(5)
        if info.state == "open" and dead not in info.route:
            break
        session.advance(0.5)
        waited += 0.5
    info = session.link_info(5)
    assert info.state == "open" and dead not in info.route

    states = [e.state for e in session_events(events, 5)]
    assert "broken" in states
    broken_at = states.index("broken")
    assert states[:broken_at] == ["probing", "open"]
    broken = session_events(events, 5)[broken_at]
    assert broken.detail  # why it broke
    reopened = [e for e in session_events(events, 5)[broken_at:] if e.state == "open"]
    assert reopened and dead not in reopened[-1].route
    assert reopened[-1].setup is not None
    assert info.counts["broken"] >= 1


# ------------------------------------------------------------- HELLO liveness


def test_a_node_that_goes_dark_is_reported_lost_by_its_neighbour_and_later_recovered():
    session = mesh(line(3), seed=1, hello_interval=2.0)
    events = record(session)
    session.advance(10.0)
    assert not [e for e in events if isinstance(e, NodeReported)]  # healthy: no false alarm
    with session.lock:
        session.simulation.set_power(3, False)
    waited = 0.0
    while waited < 60.0 and not any(isinstance(e, NodeReported) for e in events):
        session.advance(1.0)
        waited += 1.0
    reports = [e for e in events if isinstance(e, NodeReported)]
    assert reports and reports[0].node_id == 3
    assert reports[0].event == ReportEvent.LOST
    assert reports[0].reporter == 2  # the only node in range of it

    with session.lock:
        session.simulation.set_power(3, True)
    waited = 0.0
    while waited < 60.0 and not any(
        isinstance(e, NodeReported) and e.event == ReportEvent.RECOVERED for e in events
    ):
        session.advance(1.0)
        waited += 1.0
    assert any(
        isinstance(e, NodeReported) and e.node_id == 3 and e.event == ReportEvent.RECOVERED
        for e in events
    )


def test_without_hello_nobody_reports_a_dead_node():
    session = mesh(line(3), seed=1, hello_interval=None)
    events = record(session)
    with session.lock:
        session.simulation.set_power(3, False)
    session.advance(60.0)
    assert not [e for e in events if isinstance(e, NodeReported)]


# ------------------------------------------------------------- not a mesh session


@pytest.mark.parametrize(
    "verb",
    [
        lambda s: s.open_session(1),
        lambda s: s.close_session(1),
        lambda s: s.trace(1),
        lambda s: s.link_info(1),
        lambda s: s.path_view(1, "gateway"),
        lambda s: s.advance(1.0),
        lambda s: s.air_activity(),
    ],
    ids=["open_session", "close_session", "trace", "link_info", "path_view", "advance", "air_activity"],
)
def test_a_single_hop_session_refuses_the_routed_session_verbs(verb):
    session = LinkSession.simulated((1, 2), seed=1)
    assert session.multihop is False
    assert session.gateway is None and session.driver is None
    with pytest.raises(RuntimeError):
        verb(session)


# ------------------------------------------------------------ air activity (flashing)


def test_the_medium_remembers_the_last_frame_each_radio_sent():
    session = mesh(line(5))
    medium = session.simulation.medium
    assert medium.last_transmissions() == {}
    session.open_session(5)
    session.advance(0.02)
    last = medium.last_transmissions()
    gateway = session.gateway_id
    assert gateway in last
    start, end, first = last[gateway]
    assert 0.0 <= start < end
    assert first & 0x80  # sealed: KIND | 0x80, readable without the key
    assert first & 0x3F == FrameKind.PROBE
    session.advance(3.0)
    later = medium.last_transmissions()
    assert later is not last and later[gateway][1] >= end
    assert {1, 2, 3, 4, 5} <= set(later)  # the relays and the target answered
    last[gateway] = None  # a copy: the medium is not affected
    assert medium.last_transmissions()[gateway] is not None


def test_air_activity_shows_a_probe_while_probing_and_session_frames_once_a_command_runs():
    session = mesh(line(5))
    assert session.air_activity() == {}
    session.open_session(5)
    session.advance(0.02)
    activity = session.air_activity()
    start, end, first = activity[session.gateway_id]
    assert start <= session.now()
    assert first & 0x80 and first & 0x3F == FrameKind.PROBE and not first & ROUTED

    open_link(session, 5)
    future = session.request(5, 3, 1)
    session.advance(0.05)
    start, end, first = session.air_activity()[session.gateway_id]
    assert first & 0x80 and first & ROUTED  # the command goes out routed
    assert first & 0x1F == FrameKind.REQUEST
    assert finish(session, future).ok
    assert all(tx[0] <= session.now() for tx in session.air_activity().values())


def test_air_activity_leaves_out_frames_still_queued_behind_the_radios_previous_one():
    session = mesh(line(3))
    session.open_session(3)
    session.advance(0.5)
    medium = session.simulation.medium
    now = session.now()
    with session.lock:
        medium._last_tx[2] = (now + 5.0, now + 5.1, 0x81)  # starts in the future
    activity = session.air_activity()
    assert 2 not in activity
    assert session.gateway_id in activity
    with session.lock:
        medium._last_tx[2] = (now - 0.1, now, 0x81)
    assert session.air_activity()[2] == (now - 0.1, now, 0x81)


def test_a_broken_event_carries_the_path_that_broke_and_the_hop_and_comes_before_the_next_open():
    session = mesh(ladder(5), seed=2)
    events = record(session)
    open_link(session, 5)
    route = session.link_info(5).route
    assert len(route) >= 2
    dead = route[1]
    with session.lock:
        session.simulation.set_power(dead, False)  # the second relay: the first relay cannot reach it
    future = session.request(5, 3, 1)
    assert finish(session, future, limit=40.0).ok
    waited = 0.0
    while waited < 30.0:
        info = session.link_info(5)
        if info.state == "open" and dead not in info.route:
            break
        session.advance(0.5)
        waited += 0.5

    changes = session_events(events, 5)
    states = [e.state for e in changes]
    broken = changes[states.index("broken")]
    assert broken.route == route  # the route that broke, not the one probed since
    assert broken.broken_at == (route[0], dead)
    assert broken.detail == "route_error"
    reopened = [e for e in changes[states.index("broken") :] if e.state == "open"]
    assert reopened and dead not in reopened[0].route and reopened[0].route != route
    assert states.index("broken") < states.index("open", states.index("broken"))


def test_a_dead_first_relay_breaks_the_session_at_the_gateways_own_hop():
    session = mesh(line(5), gateway_options={"idle_timeout": None})
    events = record(session)
    open_link(session, 5)
    with session.lock:
        session.simulation.set_power(1, False)
    session.advance(15.0)  # the keepalive finds out
    broken = [e for e in session_events(events, 5) if e.state == "broken"]
    assert broken
    assert broken[0].route == (1, 2, 3, 4)
    assert broken[0].broken_at == (session.gateway_id, 1)
    assert broken[0].detail == "first_hop"


# ---------------------------------------------------------------- the real loop


def test_the_background_loop_runs_virtual_time_faster_than_the_wall_clock():
    session = mesh(line(2), speed=20.0)
    events = record(session)
    started = time.monotonic()
    session.start()
    try:
        session.open_session(2)
        info = None
        while time.monotonic() - started < 3.0:
            info = session.link_info(2)
            if info is not None and info.state == "open" and session.now() > 1.0:
                break
            time.sleep(0.02)
        assert info is not None and info.state == "open"
        assert session.now() > 1.0
    finally:
        session.stop()
    assert [e.state for e in session_events(events, 2)][:2] == ["probing", "open"]
    frozen = session.now()
    time.sleep(0.1)
    assert session.now() == frozen  # stopped: virtual time stands still
