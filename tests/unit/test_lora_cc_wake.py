"""Task D2: waking a dormant network (WAKE_NET / SLEEP_NET, ``wake.py``)."""

from __future__ import annotations

import random

import pytest

pytest.importorskip("cryptography")

from cryptography.hazmat.primitives.ciphers.aead import AESCCM

from cairnmesh.airtime import airtime_for
from cairnmesh.frames import (
    KeepaliveFrame,
    RequestFrame,
    SleepNetFrame,
    WakeNetFrame,
    add_route,
    decode_frame,
)
from cairnmesh.liveness import NeighbourWatch
from cairnmesh.mesh import MeshRelay
from cairnmesh.mesh_sim import MeshNetwork, line
from cairnmesh.secure import MIC_LEN, reseal, seal
from cairnmesh.wake import WakeAgent

KEY = bytes(range(16))
AEAD = AESCCM(KEY, tag_length=MIC_LEN)


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class Radio:
    """Records each send with its preamble; dormant or not."""

    def __init__(self) -> None:
        self.sent: list[tuple[bytes, int]] = []
        self.receiver = None
        self.dormant = False

    def send(self, data: bytes, preamble: int = 8) -> None:
        self.sent.append((data, preamble))

    def set_receiver(self, callback) -> None:
        self.receiver = callback

    def set_dormant(self, dormant: bool) -> None:
        self.dormant = dormant


def wake_net(seq=1, lease=60, fcnt=1) -> bytes:
    return seal(AEAD, WakeNetFrame(0x42, seq, lease, epoch=7).encode(), fcnt)


def sleep_net(seq=2, fcnt=2) -> bytes:
    return seal(AEAD, SleepNetFrame(0x42, seq, epoch=7).encode(), fcnt)


def hello(sender: int) -> bytes:
    return seal(AEAD, bytes([0x05, sender, 0xFF, 1, 1]), 1)


def node(**options):
    radio, clock = Radio(), Clock()
    relay = MeshRelay(radio, KEY, 5, clock=clock, rng=random.Random(1), wake_preamble=1000, **options)
    agent = WakeAgent(relay, radio, 5, clock=clock)
    return relay, agent, radio, clock


def settle(relay, agent, clock, seconds: float) -> None:
    for _ in range(int(seconds / 0.01)):
        clock.now += 0.01
        relay.tick(clock.now)
        agent.tick(clock.now)


# ----------------------------------------------------------------- frames


def test_wake_and_sleep_frames_round_trip():
    wake = WakeNetFrame(0x42, 9, 120, epoch=3)
    assert wake.encode() == bytes([0x10, 0x42, 0xFF, 9, 3, 120])
    assert decode_frame(wake.encode()) == wake
    sleep = SleepNetFrame(0x42, 10, epoch=3)
    assert sleep.encode() == bytes([0x11, 0x42, 0xFF, 10, 3])
    assert decode_frame(sleep.encode()) == sleep


def test_wake_frames_are_broadcast_floods_only():
    with pytest.raises(ValueError):
        decode_frame(bytes([0x10, 0x42, 0x05, 9, 3, 120]))  # not broadcast
    with pytest.raises(ValueError):
        decode_frame(bytes([0x11, 0x42, 0xFF, 10]))  # too short
    with pytest.raises(ValueError):
        add_route(WakeNetFrame(0x42, 9, 120).encode(), [3])


# ------------------------------------------------------------- one device


def test_a_wake_wakes_the_node_which_relays_it_with_a_long_preamble():
    relay, agent, radio, clock = node()
    assert not agent.awake and radio.dormant and relay.asleep
    radio.receiver(wake_net(lease=60), -100.0)
    assert agent.awake and not radio.dormant
    settle(relay, agent, clock, 3.0)
    assert radio.sent == [(reseal(AEAD, wake_net(lease=60), 5), 1000)]


def test_the_lease_runs_out_and_the_node_falls_dormant():
    relay, agent, radio, clock = node()
    radio.receiver(wake_net(lease=10), -100.0)
    settle(relay, agent, clock, 9.5)
    assert agent.awake
    settle(relay, agent, clock, 1.0)
    assert not agent.awake and radio.dormant and agent.stats["slept"] == 1


def test_a_sleep_frame_ends_the_lease_once_it_is_passed_on():
    relay, agent, radio, clock = node()
    radio.receiver(wake_net(lease=200), -100.0)
    settle(relay, agent, clock, 3.0)
    radio.receiver(sleep_net(), -100.0)
    assert agent.awake  # it still has to relay the SLEEP_NET
    settle(relay, agent, clock, 3.0)
    assert not agent.awake
    assert radio.sent[-1] == (reseal(AEAD, sleep_net(), 5), 8)  # an ordinary preamble


def test_a_dormant_node_drops_whatever_else_its_sampling_catches():
    relay, agent, radio, clock = node()
    request = seal(AEAD, RequestFrame(0x42, 9, 1, epoch=7, action=3, value=1).encode(), 5)
    radio.receiver(request, -100.0)
    settle(relay, agent, clock, 3.0)
    assert radio.sent == [] and relay.stats["asleep_dropped"] == 1 and not agent.awake


def test_wakes_beyond_the_rate_limit_are_neither_honoured_nor_relayed():
    relay, agent, radio, clock = node()
    for n in range(4):
        radio.receiver(wake_net(seq=n + 1, lease=1, fcnt=n + 1), -100.0)
        settle(relay, agent, clock, 5.0)  # wakes, relays, falls asleep again
    assert agent.stats["woken"] == 3 and agent.stats["refused"] == 1
    assert relay.stats["wakes_refused"] == 1 and len(radio.sent) == 3
    clock.now += 60.0  # the window has passed
    radio.receiver(wake_net(seq=9, lease=1, fcnt=9), -100.0)
    assert agent.awake


def test_a_session_keepalive_through_the_node_holds_it_awake():
    relay, agent, radio, clock = node()
    radio.receiver(wake_net(lease=10), -100.0)
    settle(relay, agent, clock, 5.0)
    keepalive = KeepaliveFrame(0x42, 9, 3, lease=30, epoch=7).encode()
    radio.receiver(seal(AEAD, add_route(keepalive, [5]), 3), -100.0)
    settle(relay, agent, clock, 30.0)  # past the network lease
    assert agent.awake and agent.stats["held"] == 1
    settle(relay, agent, clock, 6.0)  # past the session lease plus ``hold``
    assert not agent.awake


def test_the_originator_repeats_its_wake_until_a_relay_passes_it_on():
    radio, clock = Radio(), Clock()
    gateway = MeshRelay(radio, KEY, 0x42, forward=False, clock=clock, rng=random.Random(1), wake_preamble=1000)
    heard = []
    gateway.wake_heard = lambda: heard.append(clock.now)
    raw = wake_net()
    gateway.send(raw)
    assert radio.sent == [(raw, 1000)]
    for _ in range(3000):  # nobody passes it on: it repeats, then gives up
        clock.now += 0.01
        gateway.tick(clock.now)
    assert [p for _, p in radio.sent] == [1000] * 4 and gateway.stats["wake_repeats"] == 3
    second = wake_net(seq=2, fcnt=2)
    gateway.send(second)
    radio.receiver(reseal(AEAD, second, 7), -90.0)  # a relay passed it on
    assert heard == [clock.now]
    count = len(radio.sent)
    for _ in range(3000):
        clock.now += 0.01
        gateway.tick(clock.now)
    assert len(radio.sent) == count


def warmed(*neighbours: int, **options):
    """A dormant node that heard ``neighbours`` while it was last awake."""

    relay, agent, radio, clock = node(**options)
    relay.asleep = False
    for neighbour in neighbours:
        relay._on_radio(hello(neighbour), -100.0)
    relay.asleep = True
    clock.now = 100.0
    return relay, agent, radio, clock


def test_a_relay_repeats_its_wake_while_a_known_neighbour_stays_silent():
    relay, agent, radio, clock = warmed(6, 7, wake_copies=3)
    radio.receiver(reseal(AEAD, wake_net(), 6), -100.0)  # 6 is awake; 7 is not heard
    settle(relay, agent, clock, 20.0)
    assert len(radio.sent) == 3 and relay.stats["wake_repeats"] == 2


def test_a_relay_whose_neighbours_are_all_awake_sends_no_repeat():
    relay, agent, radio, clock = warmed(6, 7, wake_copies=3)
    radio.receiver(reseal(AEAD, wake_net(), 6), -100.0)
    radio.receiver(reseal(AEAD, wake_net(), 7), -100.0)  # within the backoff
    settle(relay, agent, clock, 20.0)
    assert len(radio.sent) == 1 and relay.stats["wake_repeats"] == 0 and relay.stats["wakes_done"] == 1


def test_a_watch_back_from_dormant_reports_nobody_lost():
    clock, radio = Clock(), Radio()
    relay = MeshRelay(radio, KEY, 5, clock=clock, rng=random.Random(1))

    class Sink:
        def send(self, data):
            pass

        def set_receiver(self, callback):
            pass

    watch = NeighbourWatch(Sink(), relay, 5, hello_interval=10.0, clock=clock, rng=random.Random(2))
    watch.on_heard(bytes([0x05, 6, 0xFF, 1, 1]), -90.0)
    clock.now = 1000.0  # slept for long
    watch.resume(clock.now)
    watch.tick(clock.now)
    assert watch.stats["lost"] == 0
    watch.tick(clock.now + 51.0)
    assert watch.stats["lost"] == 1  # silent for miss_limit intervals while awake


# ---------------------------------------------------------------- network


def run(network, clock, seconds: float) -> None:
    end = clock.now + seconds
    while clock.now < end:
        clock.now += 0.005
        network.tick(clock.now)


def test_a_dormant_line_wakes_end_to_end_and_falls_asleep_after_the_lease():
    clock = Clock()
    network = MeshNetwork(line(5), key=KEY, clock=clock, airtime=airtime_for(7), seed=1,
                          wake_options={"period": 0.5})
    run(network, clock, 5.0)
    assert all(not agent.awake for agent in network.wakers.values())  # nothing wakes them by itself
    network.waker.wake(20)
    run(network, clock, 10.0)
    assert all(agent.awake for agent in network.wakers.values())
    assert network.waker.heard_at is not None
    woke = [network.wakers[n].woke_at for n in range(1, 6)]
    assert woke == sorted(woke)  # hop by hop
    run(network, clock, 20.0)
    assert all(not agent.awake for agent in network.wakers.values())
    meter = network.medium.port(5).meter
    assert meter.wakeups == 1 and meter.dormant > 0


def test_a_woken_line_runs_a_request_and_sleeps_on_command():
    clock = Clock()
    network = MeshNetwork(line(4), key=KEY, clock=clock, airtime=airtime_for(7), seed=2,
                          wake_options={"period": 0.5})
    network.waker.wake(120)
    run(network, clock, 8.0)
    network.gateway.send(RequestFrame(0x42, 4, 1, epoch=1, action=1, value=0).encode())
    run(network, clock, 5.0)
    assert network.devices[4].last_sequence == 1
    network.waker.sleep()
    run(network, clock, 10.0)
    assert all(not agent.awake for agent in network.wakers.values())
    assert all(agent.stats["told_to_sleep"] == 1 for agent in network.wakers.values())


# ------------------------------------------------------------ D3: wake probes


from cairnmesh.frames import (  # noqa: E402
    PROBE_WAKE,
    JoinFrame,
    LinkQuality,
    ProbeFrame,
    ProbeHop,
    ProbeReplyFrame,
    split_route,
)
from cairnmesh.secure import open_sealed  # noqa: E402


def wake_probe(sender=0x42, hops=(), fcnt=1, window=40) -> bytes:
    frame = ProbeFrame(sender, 9, 3, 0x42, window | PROBE_WAKE, hops=hops, epoch=7)
    return seal(AEAD, frame.encode(), fcnt)


def test_the_wake_flag_is_the_windows_top_bit_on_a_probe_and_a_join():
    frame = ProbeFrame(0x42, 9, 3, 0x42, 40 | PROBE_WAKE)
    assert frame.wake and frame.window_s == pytest.approx(2.0)
    assert not ProbeFrame(0x42, 9, 3, 0x42, 40).wake
    # Q22 (v1 supplement, user 2026-10-21): a booting node's wake JOIN.
    join = JoinFrame(5, 0x42, 3, 5, 10 | PROBE_WAKE)
    assert join.wake and join.window_s == pytest.approx(0.5)
    assert not JoinFrame(5, 0x42, 3, 5, 10).wake


def test_a_wake_probe_wakes_a_node_only_for_its_window_and_is_relayed_long():
    relay, agent, radio, clock = node()
    relay.sealer = lambda plain: seal(AEAD, plain, 100)
    radio.receiver(wake_probe(window=40), -100.0)  # 2 s
    assert agent.awake
    settle(relay, agent, clock, 1.0)
    assert radio.sent and radio.sent[0][1] == 1000  # passed on with the long preamble
    settle(relay, agent, clock, 4.5)  # past window 2 s + probe_hold 3 s
    assert not agent.awake


def test_only_the_first_copy_of_a_wake_probe_sets_the_lease():
    relay, agent, radio, clock = node()
    radio.receiver(wake_probe(window=40), -100.0)
    first = agent.lease_until
    clock.now = 1.5
    hop = (ProbeHop(6, LinkQuality(-100, 0)),)
    radio.receiver(wake_probe(sender=6, hops=hop, fcnt=2, window=40), -100.0)  # another relay's copy
    assert agent.lease_until == first


def test_a_dormant_node_on_a_route_is_woken_and_held_by_it():
    relay, agent, radio, clock = node()
    keepalive = KeepaliveFrame(0x42, 9, 3, lease=15, epoch=7).encode()
    radio.receiver(seal(AEAD, add_route(keepalive, [5]), 3), -100.0)
    assert agent.awake and agent.stats["woken_by_route"] == 1
    settle(relay, agent, clock, 10.0)
    assert agent.awake  # held for LEASE + hold
    closing = KeepaliveFrame(0x42, 9, 4, lease=0, epoch=7).encode()
    radio.receiver(seal(AEAD, add_route(closing, [5]), 4), -100.0)
    settle(relay, agent, clock, 3.0)
    assert not agent.awake  # the session closed: no hold left


def test_a_dormant_node_off_the_route_stays_asleep():
    relay, agent, radio, clock = node()
    keepalive = KeepaliveFrame(0x42, 9, 3, lease=15, epoch=7).encode()
    radio.receiver(seal(AEAD, add_route(keepalive, [6]), 3), -100.0)
    assert not agent.awake and relay.stats["asleep_dropped"] == 1


def test_the_answer_to_a_wake_probe_goes_back_with_the_long_preamble():
    relay, agent, radio, clock = node()
    relay.sealer = lambda plain: seal(AEAD, plain, 100)
    radio.receiver(wake_probe(window=40), -100.0)
    settle(relay, agent, clock, 1.0)  # relayed the probe; remembers it
    reply = ProbeReplyFrame(9, 0x42, 3, links=(LinkQuality(-100, 0), LinkQuality(-100, 0)), epoch=7)
    routed = seal(AEAD, add_route(reply.encode(), [5]), 7)
    radio.receiver(routed, -100.0)
    settle(relay, agent, clock, 2.0)
    data, preamble = radio.sent[-1]
    assert preamble == 1000 and split_route(open_sealed(AEAD, data)[0])[1] == (5,)


def test_a_dormant_line_opens_a_session_by_a_wake_probe():
    from cairnmesh.controller import Controller

    clock = Clock()
    network = MeshNetwork(line(4), key=KEY, clock=clock, airtime=airtime_for(7), seed=3,
                          wake_options={}, gateway_options={"idle_timeout": None})
    controller = network.controller(network.sessions, clock=clock, timeout=8.0, max_retries=2)
    network.sessions.attach(controller)
    assert network.sessions.wake == "probe"
    session = network.sessions.open_session(4)
    end = clock.now + 30.0
    while clock.now < end and session.state != "open":
        clock.now += 0.005
        network.tick(clock.now)
        controller.tick(clock.now)
    assert session.state == "open" and session.route == (1, 2, 3)
    future = network.sessions.request(4, 3, 1)
    end = clock.now + 10.0
    while clock.now < end and not future.done():
        clock.now += 0.005
        network.tick(clock.now)
        controller.tick(clock.now)
    assert future.result(timeout=0).ok


def test_a_command_with_no_session_in_a_dormant_line_opens_one_instead_of_flooding():
    # Q20: an ordinary flood into the sleeping network reaches nobody.
    clock = Clock()
    network = MeshNetwork(line(4), key=KEY, clock=clock, airtime=airtime_for(7), seed=3,
                          wake_options={}, gateway_options={"idle_timeout": None})
    controller = network.controller(network.sessions, clock=clock, timeout=8.0, max_retries=2)
    network.sessions.attach(controller)
    assert network.sessions.session(4) is None
    future = network.sessions.request(4, 3, 1)
    end = clock.now + 30.0
    while clock.now < end and not future.done():
        clock.now += 0.005
        network.tick(clock.now)
        controller.tick(clock.now)
    assert future.result(timeout=0).ok  # by the path, or a retry flooded once the wake probe passed
    assert network.sessions.stats["auto_opened"] == 1
    assert network.devices[4].handled_count == 1


# ------------------------------------------------------------ D4: the model


def test_the_queueing_model_on_a_line_is_one_backoff_and_frame_per_hop():
    from cairnmesh.wake_bench import _wake_air, queued_wake

    network = MeshNetwork(line(4), key=KEY, airtime=airtime_for(7), seed=1)
    medium = network.medium
    woke = queued_wake(medium, network.controller_id)
    air = _wake_air()
    assert woke[1] == pytest.approx(air)
    for node in (2, 3, 4):
        rssi = medium.median_rssi(node - 1, node)
        closeness = min(1.0, max(0.0, (rssi + 120.0) / 80.0))
        backoff = 0.05 + 0.95 * (0.7 * closeness + 0.15)
        assert woke[node] == pytest.approx(woke[node - 1] + backoff + air)


def test_the_queueing_model_never_has_two_neighbours_on_air_together():
    import random as _random

    from cairnmesh.mesh_sim import scatter
    from cairnmesh.wake_bench import queued_wake

    network = MeshNetwork(scatter(25, 300.0, _random.Random(4)), key=KEY, airtime=airtime_for(7), seed=4)
    medium = network.medium
    sends: dict = {}
    woke = queued_wake(medium, network.controller_id, sends=sends)
    assert len(woke) >= 20
    for a, (start, end) in sends.items():
        for b in medium.neighbours(a):
            if b in sends and b != a:
                other_start, other_end = sends[b]
                assert end <= other_start or other_end <= start, (a, b)


# ------------------------------------------------------- D5: existing features


def test_a_scan_on_a_sleeping_line_wakes_it_first_then_finds_everyone():
    from cairnmesh.controller import Controller

    clock = Clock()
    network = MeshNetwork(line(4), key=KEY, clock=clock, airtime=airtime_for(7), seed=5,
                          wake_options={}, gateway_options={}, discovery_options={}, record=True)
    controller = network.controller(network.sessions, clock=clock, timeout=8.0, max_retries=2)
    network.sessions.attach(controller)
    gateway = network.sessions
    scan = gateway.scan(expected_nodes=4)
    first = [open_sealed(AEAD, data)[0][0] for _, node, data in network.medium.log if node == network.controller_id]
    assert first == [0x10]  # the WAKE_NET, nothing else yet
    end = clock.now + 300.0
    while clock.now < end and not scan.done:
        clock.now += 0.005
        network.tick(clock.now)
        controller.tick(clock.now)
    assert scan.done and set(scan.answered) == {1, 2, 3, 4}
    discovers = [t for t, node, data in network.medium.log
                 if node == network.controller_id and open_sealed(AEAD, data)[0][0] == 0x0E]
    assert discovers and discovers[0] >= gateway.scan_wake_settle
    # Awake from the scan's wake: a session needs no wake probe.
    session = gateway.open_session(4)
    end = clock.now + 10.0
    while clock.now < end and session.state != "open":
        clock.now += 0.005
        network.tick(clock.now)
        controller.tick(clock.now)
    probes = [open_sealed(AEAD, data)[0] for _, node, data in network.medium.log
              if node == network.controller_id and open_sealed(AEAD, data)[0][0] == 0x08]
    assert session.state == "open" and probes and not probes[-1][6] & PROBE_WAKE


# ------------------------------------------- WAKE_NET awake budget (review B2)


def test_wake_net_leases_draw_on_an_awake_budget():
    relay, agent, radio, clock = node()
    agent.net_awake_quota, agent._budget = 300.0, 300.0
    radio.receiver(wake_net(seq=1, lease=255, fcnt=10), -80.0)
    settle(relay, agent, clock, 1.0)
    assert agent.awake and agent.lease_until == pytest.approx(clock.now - 1.0 + 255.0, abs=0.05)
    settle(relay, agent, clock, 260.0)
    assert not agent.awake
    radio.receiver(wake_net(seq=2, lease=255, fcnt=11), -80.0)  # about 45 s + the refill left
    settle(relay, agent, clock, 0.5)
    assert agent.stats["lease_cut"] == 1 and agent.lease_until - clock.now < 120.0


def test_a_spent_budget_refuses_and_does_not_relay_wake_net():
    relay, agent, radio, clock = node()
    agent._budget = 0.0
    sent = len(radio.sent)
    radio.receiver(wake_net(seq=3, lease=60, fcnt=12), -80.0)
    settle(relay, agent, clock, 2.0)
    assert not agent.awake and agent.stats["over_budget"] >= 1
    assert len(radio.sent) == sent  # not passed on
