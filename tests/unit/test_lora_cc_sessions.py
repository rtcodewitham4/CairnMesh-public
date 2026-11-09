"""Routed sessions: probing (A2), source-routed relaying (A3), endpoint and gateway (A4).

Unit tests drive one ``MeshRelay`` with a fake radio; the rest run whole
networks on a fake clock with the Semtech airtime. No sleeping.
"""

from __future__ import annotations

import random
from collections import defaultdict

import pytest

pytest.importorskip("cryptography")

from cairnmesh.airtime import lora_airtime
from cairnmesh.frames import (
    ROUTED,
    FrameKind,
    HelloFrame,
    NodeState,
    KeepaliveFrame,
    LinkQuality,
    ProbeFrame,
    ProbeHop,
    ProbeReplyFrame,
    ReplyFrame,
    RequestFrame,
    RouteErrorFrame,
    add_route,
    decode_routed,
    is_routed,
)
from cairnmesh.mesh import MeshRelay
from cairnmesh.mesh_sim import scatter
from cairnmesh.routed import RouteEndpoint
from cairnmesh.secure import open_sealed, reseal, seal, transmitter

from _session_helpers import AEAD, G, KEY, LADDER, LINE, FakeClock, build, open_session, run

STRONG, WEAK = -60.0, -115.0
#: 2.5 dB over the SF7 floor: below the ETX safety margin, about 5 expected sends.
FADING = -122.0


class Radio:
    def __init__(self) -> None:
        self.sent: list[bytes] = []
        self.receiver = None

    def send(self, data: bytes) -> None:
        self.sent.append(data)

    def set_receiver(self, callback) -> None:
        self.receiver = callback


class Sealer:
    """Stands in for the node's ``SecureTransport.seal_frame``."""

    def __init__(self) -> None:
        self.fcnt = 1000

    def __call__(self, plain: bytes) -> bytes:
        self.fcnt += 1
        return seal(AEAD, plain, self.fcnt)


def relay(node: int = 5, **options) -> tuple[MeshRelay, Radio, FakeClock]:
    radio, clock = Radio(), FakeClock()
    node_relay = MeshRelay(
        radio, KEY, node, clock=clock, rng=random.Random(1), sealer=Sealer(), **options
    )
    return node_relay, radio, clock


def settle(node: MeshRelay, clock: FakeClock, seconds: float = 2.0) -> None:
    for _ in range(int(seconds / 0.005)):
        clock.now += 0.005
        node.tick(clock.now)


def routed_request(relays=(5, 6), target=9, fcnt=1, seq=1) -> bytes:
    plain = add_route(RequestFrame(G, target, seq, 3, 1, epoch=7).encode(), relays)
    return seal(AEAD, plain, fcnt)


def heard(radio: Radio) -> list[tuple[bytes, int]]:
    """``(plain frame, TX)`` of everything the radio sent."""

    return [(open_sealed(AEAD, raw)[0], transmitter(raw)) for raw in radio.sent]


# ------------------------------------------------------------------ airtime


def test_airtime_follows_the_semtech_formula():
    # SF7/125 kHz, 16 B: 12.25 preamble + 38 payload symbols of 1.024 ms.
    assert lora_airtime(16, 7) == pytest.approx(0.051456)
    # SF12 switches on low data rate optimisation: 40.25 symbols of 32.768 ms.
    assert lora_airtime(16, 12) == pytest.approx(1.318912)
    assert lora_airtime(33, 7) > lora_airtime(16, 7)
    with pytest.raises(ValueError):
        lora_airtime(16, 6)


# --------------------------------------------------------- routed relaying


def test_a_listed_relay_forwards_at_once_and_only_changes_tx():
    node, radio, clock = relay()
    radio.receiver(routed_request(), WEAK)
    settle(node, clock, 0.02)
    assert heard(radio) == [(open_sealed(AEAD, routed_request())[0], 5)]


def test_an_unlisted_node_ignores_a_routed_frame():
    node, radio, clock = relay(node=7)
    radio.receiver(routed_request(), STRONG)
    settle(node, clock)
    assert radio.sent == [] and node.stats["not_on_route"] == 1


def test_hearing_the_next_hop_acknowledges_ours():
    node, radio, clock = relay()
    radio.receiver(routed_request(), WEAK)
    settle(node, clock, 0.02)
    radio.receiver(reseal(AEAD, routed_request(), 6), WEAK)
    settle(node, clock)
    assert len(radio.sent) == 1 and node.stats["hop_acked"] == 1


def test_an_unacknowledged_hop_is_repeated_then_reported_back():
    node, radio, clock = relay(route_retries=2)
    radio.receiver(routed_request(seq=4), WEAK)
    settle(node, clock)
    frames = heard(radio)
    assert [tx for _, tx in frames[:3]] == [5, 5, 5]  # first try + 2 repeats
    errors = frames[3:]
    assert len(errors) == 3  # into the gateway: repeated like any hop until its echo (none here)
    error, route = decode_routed(errors[0][0])
    assert error == RouteErrorFrame(5, G, 4, broken=6, epoch=7) and route == ()
    assert node.stats["route_errors"] == 1


def test_the_reply_acknowledges_every_hop_of_its_request():
    node, radio, clock = relay()
    radio.receiver(routed_request(seq=2), WEAK)
    settle(node, clock, 0.02)
    reply = add_route(ReplyFrame(9, G, 2, 0, epoch=7).encode(), (6, 5))
    radio.receiver(seal(AEAD, reply, 1, tx=6), WEAK)
    settle(node, clock, 0.02)
    radio.receiver(seal(AEAD, reply, 1, tx=G), WEAK)  # the gateway's echo
    settle(node, clock)
    kinds = [plain[0] for plain, _ in heard(radio)]
    # the request once (the reply acknowledged it); the reply once (the echo acknowledged it)
    assert kinds == [FrameKind.REQUEST | ROUTED, FrameKind.REPLY | ROUTED]


def test_a_retry_from_behind_is_answered_once_with_the_furthest_copy():
    node, radio, clock = relay()
    radio.receiver(routed_request(relays=(5, 6, 7)), WEAK)
    settle(node, clock, 0.02)
    radio.receiver(reseal(AEAD, routed_request(relays=(5, 6, 7)), 7), WEAK)  # 7 has it
    radio.receiver(routed_request(relays=(5, 6, 7)), WEAK)  # the gateway retries
    radio.receiver(routed_request(relays=(5, 6, 7)), WEAK)  # and again
    settle(node, clock)
    assert [tx for _, tx in heard(radio)] == [5, 7]
    assert node.stats["retry_answered"] == 1


def test_a_skipped_hop_waits_for_the_relay_whose_turn_it_is():
    node, radio, clock = relay(node=6)
    radio.receiver(routed_request(), STRONG)  # straight from the gateway, past 5
    settle(node, clock, 0.02)
    assert radio.sent == []
    radio.receiver(reseal(AEAD, routed_request(), 5), STRONG)  # 5 did its job
    settle(node, clock, 0.02)
    assert [tx for _, tx in heard(radio)] == [6]

    alone, alone_radio, alone_clock = relay(node=6)
    alone_radio.receiver(routed_request(), STRONG)  # 5 stays silent
    settle(alone, alone_clock, 0.2)
    assert [tx for _, tx in heard(alone_radio)] == [6]  # one slot later


def test_a_routed_frame_listens_before_it_talks():
    node, radio, clock = relay()
    radio.busy_until = lambda: 0.3  # a neighbour is on the air until 0.3 s
    radio.receiver(routed_request(), WEAK)
    settle(node, clock, 0.25)
    assert radio.sent == []
    settle(node, clock, 0.1)
    assert len(radio.sent) == 1


def test_the_targets_answer_is_repeated_only_when_the_last_relay_asks():
    node, radio, clock = relay(node=9)
    request = routed_request(relays=(5, 6))
    radio.receiver(reseal(AEAD, request, 6), WEAK)  # from the last relay
    answer = add_route(ReplyFrame(9, G, 1, 0, epoch=7).encode(), (6, 5))
    node.send(seal(AEAD, answer, 70))
    settle(node, clock, 3.0)
    assert len(radio.sent) == 1  # no timer of its own: nobody asked again
    node.send(seal(AEAD, answer, 71))  # next answer, to a repeat that will come
    radio.receiver(reseal(AEAD, request, 6), WEAK)  # 6 repeats: it missed it
    settle(node, clock, 0.05)
    assert len(radio.sent) == 3


def test_the_latency_model_counts_hop_repeats_under_loss():
    clock, network, controller = build(LINE, seed=1)
    model = network.sessions.model
    clean, lossy = model.command(5), model.command(5, 0.05)
    assert clean.worst == lossy.worst  # every hop repeating once, either way
    assert clean.expected < lossy.expected < clean.worst
    # protocol_spec 15.4: 2.776 s without the end-to-end layer, 3.124 s with it (+5 B on REQUEST and REPLY, Q14)
    assert model.app == 5 and model.command(17).expected == pytest.approx(3.124, abs=0.01)


def test_the_last_hop_into_the_gateway_is_repeated_until_the_gateways_echo():
    node, radio, clock = relay()
    reply = add_route(ReplyFrame(9, G, 2, 0, epoch=7).encode(), (6, 5))
    radio.receiver(seal(AEAD, reply, 1, tx=6), WEAK)
    settle(node, clock, 0.5)
    assert len(radio.sent) >= 2  # no echo yet: repeated on the hop timeout
    sent = len(radio.sent)
    radio.receiver(seal(AEAD, reply, 1, tx=G), WEAK)
    settle(node, clock, 2.0)
    assert len(radio.sent) == sent and node.pending() == 0 and node.stats["hop_acked"] == 1


def test_without_echoes_the_last_hop_into_the_gateway_goes_twice_blind():
    node, radio, clock = relay(last_hop_ack=False)
    reply = add_route(ReplyFrame(9, G, 2, 0, epoch=7).encode(), (6, 5))
    radio.receiver(seal(AEAD, reply, 1, tx=6), WEAK)
    settle(node, clock, 0.5)
    assert len(radio.sent) == 2 and node.pending() == 0


def test_the_gateway_echoes_each_copy_of_an_inbound_frame_at_once():
    gateway, radio, clock = relay(node=G)
    delivered = []
    gateway.set_receiver(delivered.append)
    reply = add_route(ReplyFrame(9, G, 2, 0, epoch=7).encode(), (6, 5))
    radio.receiver(seal(AEAD, reply, 1, tx=5), WEAK)
    # before any tick: the echo must leave on the setting the frame came in on
    assert [(plain, tx) for plain, tx in heard(radio)] == [(reply, G)] and len(delivered) == 1
    radio.receiver(seal(AEAD, reply, 1, tx=5), WEAK)  # 5 missed it and repeats
    assert len(radio.sent) == 2 and len(delivered) == 1  # echoed again, delivered once
    assert gateway.stats["echoed"] == 2


# ------------------------------------------------------------------ probes


def probe(hops=(), sender=G, fcnt=1, target=9, window=10) -> bytes:
    frame = ProbeFrame(sender, target, 3, origin_id=G, window=window, hops=hops, epoch=7)
    return seal(AEAD, frame.encode(), fcnt)


GOOD = LinkQuality(-60, 120)


def test_a_probe_relay_appends_itself_and_seals_a_frame_of_its_own():
    node, radio, clock = relay(probe_retries=0)
    radio.receiver(probe(), -80.0)
    settle(node, clock)
    (sent,) = radio.sent
    plain, fcnt = open_sealed(AEAD, sent)
    frame = ProbeFrame.from_bytes(plain)
    assert frame.sender_id == 5 and fcnt == 1001  # the node's own counter
    assert [hop.node_id for hop in frame.hops] == [5]
    assert frame.hops[0].link.rssi == -80

    radio.receiver(probe(hops=(ProbeHop(4, GOOD),), sender=4, fcnt=9), -70.0)
    settle(node, clock)
    assert len(radio.sent) == 1  # one probe id, passed on once


def test_a_probe_nobody_carries_on_is_sent_once_more_as_a_new_frame():
    node, radio, clock = relay()
    radio.receiver(probe(), -80.0)
    settle(node, clock, 5.0)
    first, again = radio.sent
    assert open_sealed(AEAD, first)[0] == open_sealed(AEAD, again)[0]
    assert open_sealed(AEAD, again)[1] > open_sealed(AEAD, first)[1]  # fresh FCNT

    node, radio, clock = relay()
    radio.receiver(probe(), -80.0)
    settle(node, clock, 0.2)  # sent (backoff ≤ 0.15 s), not yet repeated
    (sent,) = radio.sent
    ours = ProbeFrame.from_bytes(open_sealed(AEAD, sent)[0])
    onward = ours.extended(6, GOOD)
    radio.receiver(seal(AEAD, onward.encode(), 50), -80.0)  # 6 carried it on
    settle(node, clock, 5.0)
    assert len(radio.sent) == 1


def test_a_probe_relay_passes_on_the_better_copy_heard_while_waiting():
    node, radio, clock = relay(probe_retries=0)
    radio.receiver(probe(), FADING)  # straight from the gateway, but barely
    radio.receiver(probe(hops=(ProbeHop(4, GOOD),), sender=4, fcnt=9), STRONG)
    settle(node, clock)
    (sent,) = radio.sent
    assert [hop.node_id for hop in ProbeFrame.from_bytes(open_sealed(AEAD, sent)[0]).hops] == [4, 5]


def test_the_target_answers_the_best_path_after_its_window():
    node, radio, clock = relay(node=9)
    radio.receiver(probe(hops=(ProbeHop(4, LinkQuality(-110, 20)),), sender=4, fcnt=5), STRONG)
    radio.receiver(probe(hops=(ProbeHop(6, GOOD),), sender=6, fcnt=5), STRONG)
    settle(node, clock, 0.3)
    assert radio.sent == []  # still inside its 0.5 s window
    settle(node, clock, 0.5)
    frame, route = decode_routed(open_sealed(AEAD, radio.sent[0])[0])
    assert isinstance(frame, ProbeReplyFrame) and route == (6,)
    assert frame.links[0] == GOOD and len(frame.links) == 2


def test_a_full_probe_is_not_passed_on():
    node, radio, clock = relay()
    hops = tuple(ProbeHop(n, GOOD) for n in range(10, 26))
    radio.receiver(probe(hops=hops, sender=25), STRONG)
    settle(node, clock)
    assert radio.sent == []


# ------------------------------------------------------------- endpoint


class Pipe:
    def __init__(self) -> None:
        self.sent: list[bytes] = []
        self.receiver = None

    def send(self, data: bytes) -> None:
        self.sent.append(bytes(data))

    def set_receiver(self, callback) -> None:
        self.receiver = callback


def test_the_endpoint_hides_routes_from_the_device_and_answers_along_them():
    pipe = Pipe()
    endpoint = RouteEndpoint(pipe, 9)
    got = []
    endpoint.set_receiver(got.append)
    request = RequestFrame(G, 9, 5, 3, 1, epoch=7)
    pipe.receiver(add_route(request.encode(), (1, 2)))
    assert got == [request.encode()]
    endpoint.send(ReplyFrame(9, G, 5, 0, epoch=7).encode())
    assert decode_routed(pipe.sent[-1]) == (ReplyFrame(9, G, 5, 0, epoch=7), (2, 1))

    pipe.receiver(request.encode())  # the gateway's fallback: same id, by flood
    endpoint.send(ReplyFrame(9, G, 5, 0, epoch=7).encode())
    assert not is_routed(pipe.sent[-1])  # the route may be what broke

    pipe.receiver(add_route(KeepaliveFrame(G, 9, 6, lease=30, epoch=7).encode(), (1,)))
    assert got == [request.encode(), request.encode()]  # the device never sees it
    assert decode_routed(pipe.sent[-1]) == (ReplyFrame(9, G, 6, 0, epoch=7), (1,))


# --------------------------------------------------------- whole networks


def test_the_target_takes_the_strongest_path_not_the_first_to_arrive():
    # Straight from the gateway is one long weak hop and arrives first; the
    # two short hops through node 2 are much stronger (widest-link rule).
    options = {"relay_options": {"probe_metric": "widest"}}
    clock, network, controller = build({1: (95.0, 0.0), 2: (47.5, 0.0)}, seed=1, **options)
    session = open_session(clock, network, controller, 1)
    assert session.route == (2,)


def test_the_target_takes_the_fewest_expected_sends():
    # ETX (default): the long hop still has 8.5 dB over the floor, about one
    # send, so it beats two sends through node 2.
    clock, network, controller = build({1: (95.0, 0.0), 2: (47.5, 0.0)}, seed=1)
    session = open_session(clock, network, controller, 1)
    assert session.route == ()


def test_etx_adds_up_mediocre_links_and_ignores_strong_ones():
    node, _, _ = relay()
    strong, fading = LinkQuality.measured(-60.0, 57.0), LinkQuality.measured(-122.0, -5.0)
    decent, slightly_less = LinkQuality.measured(-114.0, 3.0), LinkQuality.measured(-114.5, 2.5)
    assert node.path_etx((strong, strong)) == pytest.approx(2.0, abs=0.01)
    assert node.path_etx((fading,)) > node.path_etx((strong, strong, strong))
    # The widest-link rule only sees the weakest link and would take five
    # decent hops over a strong one and a slightly less decent one.
    five, two = (decent,) * 5, (strong, slightly_less)
    assert node._score_widest(five) > node._score_widest(two)
    assert node.path_etx(five) > node.path_etx(two) and node._better(two, five)


def test_a_probe_reports_the_smoothed_rssi_of_the_link_it_came_in_on():
    node, radio, clock = relay(probe_retries=0)
    for fcnt in range(1, 4):
        radio.receiver(seal(AEAD, HelloFrame(sender_id=4, sequence=fcnt, state=NodeState.READY).encode(), fcnt, 4), -100.0)
    radio.receiver(probe(hops=(ProbeHop(4, GOOD),), sender=4, fcnt=9), -112.0)  # one deep fade
    settle(node, clock)
    (sent,) = radio.sent
    hop = ProbeFrame.from_bytes(open_sealed(AEAD, sent)[0]).hops[-1]
    assert hop.node_id == 5 and hop.link.rssi == -103  # 0.75 × -100 + 0.25 × -112


def test_routed_requests_cross_five_hops_once_each_at_the_modelled_latency():
    clock, network, controller = build(LINE, seed=2, record=True)
    session = open_session(clock, network, controller, 5)
    assert session.route == (1, 2, 3, 4)
    expected = network.sessions.model.command(session.hops).expected
    start = len(network.medium.log)
    for value in range(10):
        future = network.sessions.request(5, 3, value)
        run(clock, network, controller, 10.0, future.done)
        outcome = future.result(timeout=0)
        assert outcome.ok and outcome.attempts == 1
        assert outcome.elapsed == pytest.approx(expected, rel=0.10)
        run(clock, network, controller, 0.3)
    assert network.devices[5].handled_count == 10
    log = network.medium.log[start:]
    assert len(log) <= 10 * (2 * session.hops + 4)
    on_route = {G, 5, *session.route}
    assert all(node in on_route for _, node, raw in log if open_sealed(AEAD, raw)[0][0] & ROUTED)


def test_a_dead_relay_is_routed_around_and_the_request_still_runs_once():
    clock, network, controller = build(LADDER, seed=3, record=True)
    session = open_session(clock, network, controller, 5)
    dead = session.route[len(session.route) // 2]
    network.set_power(dead, False)
    future = network.sessions.request(5, 3, 1)
    run(clock, network, controller, 30.0, future.done)
    assert future.result(timeout=0).ok
    assert network.devices[5].handled_count == 1
    assert any(event.startswith("broken") for _, event, _ in network.sessions.history)
    run(clock, network, controller, 20.0, lambda: session.state == "open")
    assert session.state == "open" and dead not in session.route

    # No nonce was ever used twice: one (SENDER, TARGET, FCNT, KIND, TX), one frame.
    frames = defaultdict(set)
    for _, _, raw in network.medium.log:
        frames[(raw[0], raw[1], raw[2], raw[4:8], raw[8])].add(raw)
    assert all(len(copies) == 1 for copies in frames.values())


def test_a_command_while_the_path_is_probed_keeps_trying_for_the_fallback_window():
    clock, network, controller = build(LINE, seed=3, gateway_options={"fallback_window": 20.0})
    gateway = network.sessions
    hold = gateway.flood_hold
    assert gateway.timeouts_for(5) == (hold, 3)  # no session: a few floods
    session = gateway.open_session(5)
    assert session.state == "probing"
    # Probing: floods every flood_hold for the whole fallback window, so the
    # retry after the path opens can still go over it.
    timeout, retries = gateway.timeouts_for(5)
    assert timeout == hold and (retries + 1) * hold <= 20.0 < (retries + 2) * hold
    gateway.fallback_window = 60.0  # never past the controller's own limit
    timeout, retries = gateway.timeouts_for(5)
    assert (retries + 1) * hold <= controller.max_retry_window < (retries + 2) * hold
    gateway.fallback_window = 20.0
    run(clock, network, controller, 20.0, lambda: session.state == "open")
    timeout, retries = gateway.timeouts_for(5)
    assert timeout < hold and (retries + 1) * timeout <= 20.0  # open: routed tries


def test_a_keepalive_notices_a_dead_relay_within_the_modelled_time():
    clock, network, controller = build(LADDER, seed=4, gateway_options={"idle_timeout": None})
    session = open_session(clock, network, controller, 5)
    bound = network.sessions.model.detection(session.hops, network.sessions.keepalive_interval)
    off = clock.now
    network.set_power(session.route[0], False)
    run(clock, network, controller, 30.0, lambda: session.state != "open")
    broken = [t for t, event, _ in network.sessions.history if event.startswith("broken")]
    assert broken and broken[0] - off <= bound + network.sessions.margin


# The gateway hears relay 2 too (95, weak), but the path through 1 is stronger.
SKIPPABLE = {1: (50.0, 0.0), 2: (95.0, 0.0), 3: (180.0, 0.0)}


@pytest.mark.parametrize("skip_reprobe", [2, None])
def test_a_first_relay_skipped_in_a_row_is_probed_around(skip_reprobe):
    gateway_options = {"idle_timeout": None, "skip_reprobe": skip_reprobe}
    # The widest-link rule keeps the path through 1 (ETX would go round it).
    options = {"relay_options": {"probe_metric": "widest"}}
    clock, network, controller = build(SKIPPABLE, seed=6, gateway_options=gateway_options, **options)
    session = open_session(clock, network, controller, 3)
    assert session.route == (1, 2)
    network.set_power(1, False)
    # Relay 2 overhears the gateway and takes over: nothing ever breaks.
    run(clock, network, controller, 15.0, lambda: session.state != "open")
    events = [event for _, event, _ in network.sessions.history]
    if skip_reprobe is None:
        assert session.state == "open" and session.route == (1, 2)
        assert not any(event.startswith("broken") for event in events)
        assert session.counts["first_hop_skips"] >= 2
        return
    assert "broken:first_hop_skipped" in events
    run(clock, network, controller, 20.0, lambda: session.state == "open")
    assert session.state == "open" and session.route == (2,)


def test_floods_are_sent_one_at_a_time():
    clock, network, controller = build(LINE, seed=5, record=True)
    first = network.sessions.request(4, 3, 1)
    second = network.sessions.request(5, 3, 1)
    run(clock, network, controller, 0.01)
    assert [node for _, node, _ in network.medium.log] == [G]  # only the first is out
    run(clock, network, controller, 30.0, lambda: first.done() and second.done())
    assert first.result(timeout=0).ok and second.result(timeout=0).ok


def test_requests_run_at_most_once_with_sessions_and_loss():
    positions = scatter(30, 300.0, random.Random(6))
    clock, network, controller = build(
        positions, seed=6, drop=0.05, controller_position=(150.0, 150.0)
    )
    targets = [node for node in positions if network.reachable(node)][:4]
    futures = defaultdict(list)
    for target in targets:
        open_session(clock, network, controller, target)
        for value in range(5):
            future = network.sessions.request(target, 3, value)
            run(clock, network, controller, 30.0, future.done)
            futures[target].append(future)
        network.sessions.close_session(target)
    for target in targets:
        delivered = sum(f.result(timeout=0).ok for f in futures[target])
        assert network.devices[target].handled_count == delivered
        assert delivered >= 4


def test_the_gateway_history_keeps_only_the_latest_events():
    clock, network, controller = build({1: (50.0, 0.0)}, seed=7)
    gateway = network.sessions
    gateway.max_history = 20
    for _ in range(50):
        gateway._log("probe", 1)
    assert len(gateway.history) <= 22 and gateway.history_base + len(gateway.history) == 50


# ------------------------------------------------------- C5: detours


def test_a_routed_probe_is_passed_on_only_by_the_next_listed_relay():
    from cairnmesh.frames import ProbeFrame, add_route, decode_routed
    from cairnmesh.secure import open_sealed, seal

    counter = iter(range(50, 100))

    def relay_at(node_id):
        radio = Radio()
        node = MeshRelay(radio, KEY, node_id, clock=FakeClock(), rng=random.Random(1))
        node.sealer = lambda plain: seal(AEAD, plain, next(counter))  # the node's own FCNT
        return node, radio

    probe = ProbeFrame(G, 9, 3, G, 1, epoch=7)
    raw = seal(AEAD, add_route(probe.encode(), (5, 6)), 1)
    listed, radio = relay_at(5)
    radio.receiver(raw, -90.0)
    for _ in range(100):
        listed.clock.now += 0.01
        listed.tick(listed.clock.now)
    frame, rest = decode_routed(open_sealed(AEAD, radio.sent[0])[0])
    assert [hop.node_id for hop in frame.hops] == [5] and rest == (6,) and frame.sender_id == 5
    other, other_radio = relay_at(6)  # listed, but not next
    other_radio.receiver(raw, -90.0)
    for _ in range(100):
        other.clock.now += 0.01
        other.tick(other.clock.now)
    assert other_radio.sent == [] and other.stats["not_on_route"] == 1


def test_a_detour_goes_round_the_suspect_relay_over_known_links():
    from cairnmesh.frames import LinkQuality, ProbeHop
    from cairnmesh.gateway import KnownNode

    clock, network, controller = build(LADDER, seed=1, signal=None, gateway_options={"idle_timeout": None})
    gateway = network.sessions
    good = LinkQuality(-80, 40)
    # What scans told the gateway: the lower row's route and the upper row's.
    for node in range(1, 6):
        gateway.nodes[node] = KnownNode(node, tuple(range(1, node)), (good,) * node, 0.0, "scan")
        gateway.nodes[10 + node] = KnownNode(10 + node, tuple(range(11, 10 + node)), (good,) * node, 0.0, "scan",
                                             neighbours=(ProbeHop(node, good),))
    session = gateway.open_session(5)
    session.route, session.links = (1, 2, 3, 4), (good,) * 5
    session.suspect = (2, 3)
    detour = gateway._detour(session)
    assert detour is not None and 3 not in detour and detour[-1] in (4, 15)


def test_a_dead_relay_is_gone_round_by_a_detour_after_a_scan():
    clock, network, controller = build(LADDER, seed=1, signal=None, discovery_options={"join": False},
                                       gateway_options={"idle_timeout": None})
    gateway = network.sessions
    scan = gateway.scan(expected_nodes=10)
    run(clock, network, controller, 200.0, lambda: scan.done)
    session = open_session(clock, network, controller, 5)
    dead = session.route[len(session.route) // 2]
    network.set_power(dead, False)
    future = gateway.request(5, 3, 1)
    run(clock, network, controller, 30.0, future.done)
    run(clock, network, controller, 20.0, lambda: session.state == "open")
    assert future.result(timeout=0).ok
    assert session.counts["detours"] >= 1 and dead not in session.route


# ------------------------------------------------- Q11: session-level SF


def test_the_radio_byte_holds_a_channel_and_an_sf():
    from cairnmesh.frames import radio_byte, radio_of

    assert radio_byte() == 0 and radio_of(0) == (0, 7)
    assert radio_of(radio_byte(5, 9)) == (5, 9)
    keepalive = KeepaliveFrame(G, 9, 6, lease=30, epoch=7, radio=radio_byte(0, 9))
    assert len(keepalive.encode()) == 7 and KeepaliveFrame.from_bytes(keepalive.encode()) == keepalive
    with pytest.raises(ValueError):
        radio_byte(0, 13)


def test_a_session_slows_down_to_its_weakest_hops_margin():
    clock, network, controller = build(LINE, seed=1, gateway_options={"idle_timeout": None})
    gateway = network.sessions
    strong = LinkQuality(-90, 40)  # SNR +10 dB: 17.5 dB over the SF7 floor
    assert gateway._choose_sf((strong, strong)) == 7
    marginal = LinkQuality(-118, -24)  # SNR −6 dB: 1.5 dB over it
    assert gateway._choose_sf((strong, marginal)) == 8  # one step of 2.5 dB reaches the 4 dB margin
    hopeless = LinkQuality(-125, -80)
    assert gateway._choose_sf((hopeless,)) == gateway.max_sf


def test_the_whole_path_moves_to_the_sessions_sf_and_back_when_it_closes():
    clock, network, controller = build(LINE, seed=1, gateway_options={"idle_timeout": None, "sf_margin": 30.0})
    gateway = network.sessions
    session = open_session(clock, network, controller, 5)
    run(clock, network, controller, 5.0, lambda: session.sf != 7)
    assert session.sf == gateway.max_sf and session.counts["sf_switches"] == 1
    path = (G, *session.route, 5)
    run(clock, network, controller, 1.0)
    assert all(network.relays[node].radio_setting == (0, session.sf) for node in path)
    future = gateway.request(5, 3, 1)
    run(clock, network, controller, 30.0, future.done)
    assert future.result(timeout=0).ok
    gateway.close_session(5)
    run(clock, network, controller, 10.0)
    assert all(network.relays[node].radio_setting == (0, 7) for node in path)


def test_closing_a_slow_session_brings_the_path_home_by_its_answer_before_the_gateway_leaves():
    clock, network, controller = build(LINE, seed=1, gateway_options={"idle_timeout": None, "sf_margin": 30.0})
    gateway = network.sessions
    session = open_session(clock, network, controller, 5)
    run(clock, network, controller, 5.0, lambda: session.sf != 7)
    sf, path = session.sf, (*session.route, 5)
    run(clock, network, controller, 1.0)
    gateway.close_session(5)
    # The closing keepalive and its repeats go out on the session's setting.
    assert gateway.relay.radio_setting == (0, sf)
    following = gateway.open_session(4)  # its probe waits until the gateway is home
    run(clock, network, controller, 0.05)
    assert following.probe_id is None
    run(clock, network, controller, 5.0, lambda: gateway.relay.radio_setting == (0, 7))
    assert gateway.relay.radio_setting == (0, 7)
    # Home by following the answer (the relay next to the gateway after its
    # repeat into it), well before the silence timeout.
    home = lambda: all(network.relays[node].radio_setting == (0, 7) for node in path)  # noqa: E731
    run(clock, network, controller, 1.0, home)
    assert home()
    assert all(network.relays[node].stats["radio_reverted"] == 0 for node in path)
    run(clock, network, controller, 20.0, lambda: following.state == "open")
    assert following.state == "open"


def test_a_relay_left_alone_on_a_session_setting_comes_back_by_itself():
    radio = Radio()
    clock = FakeClock()
    relay = MeshRelay(radio, KEY, 5, clock=clock, rng=random.Random(1), radio_timeout=6.0)
    relay.set_radio(0, 9)
    for _ in range(int(5.0 / 0.01)):
        clock.now += 0.01
        relay.tick(clock.now)
    assert relay.radio_setting == (0, 9)
    for _ in range(int(2.0 / 0.01)):
        clock.now += 0.01
        relay.tick(clock.now)
    assert relay.radio_setting == (0, 7) and relay.stats["radio_reverted"] == 1
