"""Task Q12 / B4.5: session channels and a gateway with more than one radio."""

from __future__ import annotations

import pytest

pytest.importorskip("cryptography")

from cairnmesh.events import HeartbeatReceived
from cairnmesh.mesh_sim import GeoMedium
from cairnmesh.secure import SecureTransport, transmitter

from _session_helpers import G, KEY, LADDER, LINE, FakeClock, build, open_session, run

def _on_setting(network, nodes, setting) -> bool:
    return all(network.relays[node].radio_setting == setting for node in nodes)


def test_radios_at_one_site_share_its_links_and_never_hear_each_other():
    clock = FakeClock()
    medium = GeoMedium(radio_range=100.0, airtime=0.01, clock=clock)
    heard = {name: [] for name in ("gw", "second", "node")}
    gateway = medium.add(1, (0.0, 0.0))
    second = medium.add(0x101, (50.0, 50.0), site=1)  # placed at its site whatever is asked
    node = medium.add(2, (80.0, 0.0))
    for name, port in (("gw", gateway), ("second", second), ("node", node)):
        port.set_receiver(lambda data, rssi, name=name: heard[name].append(data))
    assert second.position == gateway.position
    assert medium.neighbours(2) == [1] and medium.neighbours(1) == [2]
    gateway.send(b"a")
    clock.now = 1.0
    medium.tick()
    assert heard == {"gw": [], "second": [], "node": [b"a"]}
    second.set_radio(3, 7)
    node.set_radio(3, 7)
    node.send(b"b")
    clock.now = 2.0
    medium.tick()
    assert heard["second"] == [b"b"] and heard["gw"] == []


def test_with_site_blocking_one_radio_sending_deafens_the_other():
    clock = FakeClock()
    medium = GeoMedium(radio_range=100.0, airtime=0.5, clock=clock)
    medium.site_blocking = True
    heard = []
    gateway = medium.add(1, (0.0, 0.0))
    second = medium.add(0x101, (0.0, 0.0), site=1)
    node = medium.add(2, (80.0, 0.0))
    second.set_radio(3, 7)
    node.set_radio(3, 7)
    second.set_receiver(lambda data, rssi: heard.append(data))
    node.send(b"lost")
    clock.now = 0.1
    gateway.send(b"over it")  # channel 0, same antenna site
    clock.now = 2.0
    medium.tick()
    assert heard == [] and medium.stats["blocked"] == 1


def test_a_second_radio_seals_under_the_first_ones_counter():
    class Null:
        def set_receiver(self, callback):
            pass

        def send(self, data):
            pass

    first = SecureTransport(Null(), KEY, initial_fcnt=10)
    second = SecureTransport(Null(), KEY, initial_fcnt=0, shares=first)
    a, b = first.seal_frame(bytes(6)), second.seal_frame(bytes(6))
    assert first.fcnt == 12 and a != b


def test_a_session_moves_to_a_radio_and_channel_of_its_own_and_comes_back():
    clock, network, controller = build(LINE, seed=1, gateway_options={"idle_timeout": None, "radios": 2})
    gateway = network.sessions
    session = open_session(clock, network, controller, 5)
    run(clock, network, controller, 5.0, lambda: session.radio == 1)
    path = (*session.route, 5)
    assert session.radio == 1 and session.counts["radio_switches"] == 1
    run(clock, network, controller, 1.0)
    assert _on_setting(network, path, (1, 7)) and network.gateway_radios[0].radio_setting == (1, 7)
    assert gateway.relay.radio_setting == (0, 7)  # radio 0 keeps the common channel
    future = gateway.request(5, 3, 1)
    run(clock, network, controller, 30.0, future.done)
    assert future.result(timeout=0).ok
    gateway.close_session(5)
    run(clock, network, controller, 10.0)
    assert _on_setting(network, path, (0, 7)) and gateway._arms[1].holder is None


def test_with_one_radio_a_session_keeps_channel_0():
    clock, network, controller = build(LINE, seed=1, gateway_options={"idle_timeout": None})
    session = open_session(clock, network, controller, 5)
    run(clock, network, controller, 5.0)
    assert session.radio == 0 and _on_setting(network, (*session.route, 5), (0, 7))


def test_the_common_channel_still_reaches_the_gateway_during_a_session():
    clock, network, controller = build(LADDER, seed=1, gateway_options={"idle_timeout": None, "radios": 2})
    heard = []
    controller.events.subscribe(lambda e: heard.append(e.node_id) if isinstance(e, HeartbeatReceived) else None)
    session = open_session(clock, network, controller, 5)
    run(clock, network, controller, 5.0, lambda: session.radio == 1)
    assert session.radio == 1
    # Settled on radio 1: the move's answer has come in and been echoed on radio 0
    # (one heartbeat sent at that very moment collides with the echo).
    run(clock, network, controller, 1.0)
    off = next(node for node in LADDER if node not in (*session.route, 5))
    network.devices[off].send_heartbeat()
    run(clock, network, controller, 5.0)
    assert off in heard


def test_a_scan_counts_a_sessions_path_as_alive_not_missing():
    from cairnmesh.events import ScanFinished

    clock, network, controller = build(
        LADDER, gateway_options={"idle_timeout": None, "radios": 2}, discovery_options={"join": False}
    )
    gateway = network.sessions
    first = gateway.scan()
    run(clock, network, controller, 120.0, lambda: first.done)
    session = open_session(clock, network, controller, 5)
    run(clock, network, controller, 5.0, lambda: session.radio == 1)
    assert session.radio == 1
    finished = []
    controller.events.subscribe(lambda e: finished.append(e) if isinstance(e, ScanFinished) else None)
    scan = gateway.scan()
    run(clock, network, controller, 200.0, lambda: scan.done)
    path = {*session.route, 5}
    assert finished and not path & set(finished[-1].missing)
    assert set(finished[-1].found) | path >= set(LADDER)
    run(clock, network, controller, 10.0, lambda: session.radio == 1)
    assert session.state == "open" and session.radio == 1  # back on its own radio after the scan


def test_frames_from_a_second_radio_name_the_gateway():
    clock, network, controller = build(LINE, seed=1, gateway_options={"idle_timeout": None, "radios": 2}, record=True)
    session = open_session(clock, network, controller, 5)
    run(clock, network, controller, 5.0, lambda: session.radio == 1)
    future = network.sessions.request(5, 3, 1)
    run(clock, network, controller, 30.0, future.done)
    second = G | 1 << 8
    sent = [raw for _, port, raw in network.medium.log if port == second]
    assert sent and all(transmitter(raw) == G for raw in sent)


# ------------------------------------------------- one session at a time (user 2026-10-20)


def test_opening_another_session_closes_the_one_open():
    clock, network, controller = build(LINE, seed=1, gateway_options={"idle_timeout": None, "radios": 2})
    gateway = network.sessions
    far = open_session(clock, network, controller, 5)
    run(clock, network, controller, 5.0, lambda: far.radio == 1)
    path = (*far.route, 5)
    near = open_session(clock, network, controller, 3)
    assert far.state == "closed" and list(gateway.sessions) == [3]
    run(clock, network, controller, 10.0, lambda: near.radio == 1)
    assert near.radio == 1  # the radio went to the new session
    future = gateway.request(3, 3, 1)
    run(clock, network, controller, 30.0, future.done)
    assert future.result(timeout=0).ok
    assert _on_setting(network, [node for node in path if node not in (*near.route, 3)], (0, 7))


def test_a_gateway_has_two_radios_at_most():
    with pytest.raises(ValueError):
        build(LINE, seed=1, gateway_options={"radios": 3})


def test_a_failure_left_on_a_radio_the_session_moved_off_breaks_nothing():
    from cairnmesh.frames import KeepaliveFrame, add_route

    clock, network, controller = build(LINE, seed=1, gateway_options={"idle_timeout": None, "radios": 2})
    gateway = network.sessions
    session = open_session(clock, network, controller, 5)
    run(clock, network, controller, 5.0, lambda: session.radio == 1)
    stale = add_route(KeepaliveFrame(G, 5, 9, lease=15, epoch=1).encode(), session.route)
    gateway._on_route_failed(stale, session.route[0], radio=0)
    assert session.state == "open" and gateway.stats["stale_failures"] == 1
    gateway._on_route_failed(stale, session.route[0], radio=1)
    assert session.state != "open"  # on its own radio it is a break


def _dead_off_channel_0(network, links) -> None:
    """``links`` (unordered pairs) carry nothing off the common channel."""

    medium, original = network.medium, network.medium._copy_rssi

    def copy_rssi(sender, receiver, air):
        if air.channel != 0 and frozenset((sender, receiver)) in links:
            return None
        return original(sender, receiver, air)

    medium._copy_rssi = copy_rssi


def test_a_target_that_fails_on_the_session_channel_stays_on_the_common_one():
    # The target hears nothing off channel 0. Each probe succeeds
    # on channel 0, the session moves and breaks at once; after
    # radio_strikes (2) such breaks the target keeps the common channel.
    clock, network, controller = build(LINE, seed=1, gateway_options={"idle_timeout": None, "radios": 2})
    _dead_off_channel_0(network, {frozenset((5, n)) for n in (G, 1, 2, 3, 4)})
    gateway = network.sessions
    session = open_session(clock, network, controller, 5)
    futures = []
    for _ in range(20):
        futures.append(gateway.request(5, 3, 1))
        run(clock, network, controller, 4.0)
    run(clock, network, controller, 30.0, lambda: all(f.done() for f in futures))
    assert all(f.result(timeout=0).ok for f in futures)
    assert gateway.stats["radio_bans"] == 1 and session.counts["radio_strikes"] == 2
    assert session.counts["broken"] == 2 and session.radio == 0 and gateway._arms[1].holder is None


def test_business_traffic_after_the_trial_clears_a_strike_without_a_keepalive():
    # A quick break is forgotten after any answered exchange outlasts
    # radio_trial, including business traffic when no keepalive was needed.
    clock, network, controller = build(
        LINE,
        seed=1,
        gateway_options={
            "idle_timeout": None,
            "radios": 2,
            "radio_trial": 1.0,
            "keepalive_interval": 1000.0,
        },
    )
    gateway = network.sessions
    session = open_session(clock, network, controller, 5)
    run(clock, network, controller, 5.0, lambda: session.radio == 1)
    gateway._strike(session, clock.now)
    assert gateway._radio_strikes == {5: [clock.now]}
    session.next_keepalive = clock.now + 1000.0
    keepalives = session.counts["keepalives"]
    run(clock, network, controller, 2.0)
    future = gateway.request(5, 3, 1)
    run(clock, network, controller, 10.0, future.done)
    assert future.result(timeout=0).ok
    assert session.counts["keepalives"] == keepalives
    assert gateway._radio_strikes == {} and gateway.stats["radio_bans"] == 0 and session.radio == 1


def test_close_and_reopen_keep_a_recent_strike_and_the_next_quick_break_bans():
    clock, network, controller = build(LINE, seed=1, gateway_options={"idle_timeout": None, "radios": 2})
    gateway = network.sessions
    session = open_session(clock, network, controller, 5)
    run(clock, network, controller, 5.0, lambda: session.radio == 1)
    gateway._strike(session, clock.now)
    first = clock.now

    gateway.close_session(5)
    assert gateway._radio_strikes == {5: [first]}

    session = open_session(clock, network, controller, 5)
    run(clock, network, controller, 5.0, lambda: session.radio == 1)
    gateway._strike(session, clock.now)
    assert gateway.stats["radio_bans"] == 1 and 5 not in gateway._radio_strikes
    assert gateway._radio_banned[5] == clock.now + gateway.radio_ban


@pytest.mark.parametrize(("gap", "banned"), [(10.0, True), (10.001, False)])
def test_strikes_exactly_at_the_window_are_consecutive_but_older_ones_expire(gap, banned):
    clock, network, controller = build(
        LINE,
        seed=1,
        gateway_options={"idle_timeout": None, "radios": 2, "radio_strike_window": 10.0},
    )
    gateway = network.sessions
    session = open_session(clock, network, controller, 5)
    gateway._strike(session, clock.now)
    clock.now += gap
    gateway._strike(session, clock.now)
    assert (gateway.stats["radio_bans"] == 1) is banned
    assert (5 in gateway._radio_banned) is banned
    if not banned:
        assert gateway._radio_strikes == {5: [clock.now]}


def test_a_banned_target_can_get_a_session_radio_after_the_ban_expires():
    clock, network, controller = build(
        LINE,
        seed=1,
        gateway_options={"idle_timeout": None, "radios": 2, "radio_ban": 20.0},
    )
    gateway = network.sessions
    session = open_session(clock, network, controller, 5)
    run(clock, network, controller, 5.0, lambda: session.radio == 1)
    gateway._strike(session, clock.now)
    gateway._strike(session, clock.now + 0.001)
    gateway._leave_radio(session)
    assert session.radio == 0 and session.radio_wanted is None and gateway._arms[1].holder is None

    clock.now = gateway._radio_banned[5] + 0.001
    gateway._assign_radios(clock.now)
    assert session.radio_wanted == 1 and gateway._arms[1].holder == 5


def test_radio_strike_history_is_independent_per_target():
    clock, network, controller = build(LINE, seed=1, gateway_options={"idle_timeout": None, "radios": 2})
    gateway = network.sessions
    first = open_session(clock, network, controller, 5)
    first_at = clock.now
    gateway._strike(first, clock.now)
    gateway.close_session(5)

    second = open_session(clock, network, controller, 3)
    second_at = clock.now
    gateway._strike(second, clock.now)
    assert gateway.stats["radio_bans"] == 0
    assert gateway._radio_strikes == {5: [first_at], 3: [second_at]}
