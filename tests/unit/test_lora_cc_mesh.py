"""Managed flooding (``mesh.py``) on a simulated multi-hop radio (``mesh_sim.py``).

Unit tests drive one ``MeshRelay`` with a fake radio; the rest run whole
networks on a fake clock. No sleeping.
"""

from __future__ import annotations

import random
from collections import Counter

import pytest

pytest.importorskip("cryptography")

from cryptography.hazmat.primitives.ciphers.aead import AESCCM

from cairnmesh.frames import HeartbeatFrame, NodeState, ReplyFrame, RequestFrame
from cairnmesh.mesh import MeshRelay
from cairnmesh.mesh_sim import MeshNetwork, scatter
from cairnmesh.secure import MIC_LEN, open_sealed, reseal, seal, transmitter

KEY = bytes(range(16))
AEAD = AESCCM(KEY, tag_length=MIC_LEN)
STRONG, WEAK = -45.0, -115.0


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class Radio:
    def __init__(self) -> None:
        self.sent: list[bytes] = []
        self.receiver = None

    def send(self, data: bytes) -> None:
        self.sent.append(data)

    def set_receiver(self, callback) -> None:
        self.receiver = callback


def request(seq=1, target=9, fcnt=1) -> bytes:
    frame = RequestFrame(sender_id=0x42, target_id=target, sequence=seq, epoch=7, action=3, value=1)
    return seal(AEAD, frame.encode(), fcnt)


def reply(seq=1, sender=9, fcnt=1) -> bytes:
    frame = ReplyFrame(sender_id=sender, target_id=0x42, sequence=seq, epoch=7, result=0)
    return seal(AEAD, frame.encode(), fcnt)


def relayed(raw: bytes, by: int = 5) -> bytes:
    """``raw`` as relay ``by`` puts it on the air: only TX differs."""

    return reseal(AEAD, raw, by)


def relay(**options) -> tuple[MeshRelay, Radio, FakeClock]:
    radio, clock = Radio(), FakeClock()
    node = MeshRelay(radio, KEY, 5, clock=clock, rng=random.Random(1), **options)
    return node, radio, clock


def settle(node: MeshRelay, clock: FakeClock, seconds: float = 5.0) -> None:
    for _ in range(int(seconds / 0.01)):
        clock.now += 0.01
        node.tick(clock.now)


# ----------------------------------------------------------- relay rules


def test_a_frame_for_someone_else_is_relayed_once():
    node, radio, clock = relay()
    radio.receiver(request(), WEAK)
    radio.receiver(request(), WEAK)  # the same transmission again
    settle(node, clock)
    assert radio.sent == [relayed(request())]  # once
    assert node.stats["relayed"] == 1 and node.stats["duplicates"] == 1


def test_a_relay_changes_only_the_transmitter():
    # SENDER, FCNT and the plain frame (every duplicate key) survive the hop.
    node, radio, clock = relay()
    radio.receiver(request(fcnt=33), WEAK)
    settle(node, clock)
    (copy,) = radio.sent
    assert transmitter(request(fcnt=33)) == 0x42 and transmitter(copy) == 5
    assert open_sealed(AEAD, copy) == open_sealed(AEAD, request(fcnt=33))
    assert copy[:8] == request(fcnt=33)[:8]


def test_old_copies_are_never_relayed_again_however_busy_the_origin():
    # A FIFO cache would have forgotten fcnt=1 after this much traffic.
    node, radio, clock = relay(max_pending=1000)
    for fcnt in range(1, 300):
        radio.receiver(request(seq=fcnt & 0xFF, fcnt=fcnt), WEAK)
    settle(node, clock)
    relayed = len(radio.sent)

    radio.receiver(request(seq=1, fcnt=1), WEAK)  # still circulating somewhere
    settle(node, clock)
    assert len(radio.sent) == relayed


def test_a_reboot_empties_the_relay_windows_but_the_seeded_controller_one():
    # Spec 7: relay windows live in RAM; only the controller's comes back,
    # from the persisted replay mark. Another origin's old frame goes out once more.
    node, radio, clock = relay()
    beat = seal(AEAD, HeartbeatFrame(sender_id=9, target_id=0x42, sequence=0, state=NodeState.READY).encode(), 4)
    radio.receiver(request(fcnt=10), WEAK)
    radio.receiver(beat, WEAK)
    settle(node, clock)
    before = len(radio.sent)

    node.reboot({0x42: 10})
    radio.receiver(request(fcnt=10), WEAK)
    radio.receiver(beat, WEAK)
    settle(node, clock)
    assert radio.sent[before:] == [relayed(beat)]


def test_an_end_to_end_retry_is_relayed_again():
    # Same message id (EPOCH, SEQ), new FCNT: keying relays on the message id
    # would stop every retry here.
    node, radio, clock = relay()
    radio.receiver(request(seq=4, fcnt=10), WEAK)
    settle(node, clock)
    radio.receiver(request(seq=4, fcnt=11), WEAK)
    settle(node, clock)
    assert radio.sent == [relayed(request(seq=4, fcnt=10)), relayed(request(seq=4, fcnt=11))]


def test_hearing_enough_copies_cancels_a_waiting_relay():
    node, radio, clock = relay(suppress_after=2, backoff=(1.0, 1.0))
    radio.receiver(request(), WEAK)
    radio.receiver(request(), WEAK)
    assert radio.sent == [] and node.pending() == 1
    radio.receiver(request(), WEAK)
    settle(node, clock)
    assert radio.sent == [] and node.stats["suppressed"] == 1


def test_a_strong_signal_waits_longer_than_a_weak_one():
    near, near_radio, near_clock = relay(rssi_weight=1.0)
    far, far_radio, far_clock = relay(rssi_weight=1.0)
    near_radio.receiver(request(), STRONG)
    far_radio.receiver(request(), WEAK)
    for _ in range(200):
        near_clock.now = far_clock.now = near_clock.now + 0.01
        near.tick()
        far.tick()
        if far_radio.sent:
            break
    assert far_radio.sent and not near_radio.sent  # the distant node goes first


def test_a_flood_relay_listens_before_it_talks():
    node, radio, clock = relay()
    radio.busy_until = lambda: 2.0  # a neighbour is on the air until 2 s, past any backoff
    radio.receiver(request(), WEAK)
    settle(node, clock, 1.95)
    assert radio.sent == []
    settle(node, clock, 0.2)
    assert radio.sent == [relayed(request())]


def test_the_reply_cancels_relays_of_its_request():
    node, radio, clock = relay(backoff=(1.0, 1.0))
    radio.receiver(request(seq=6), WEAK)
    radio.receiver(reply(seq=6), WEAK)
    settle(node, clock)
    assert radio.sent == []  # request cancelled; the reply is not on our path
    assert node.stats["answered"] == 1


def test_only_nodes_that_relayed_the_request_relay_its_reply():
    node, radio, clock = relay()
    radio.receiver(reply(seq=2), WEAK)  # never saw the request
    settle(node, clock)
    assert radio.sent == [] and node.stats["not_on_path"] == 1

    radio.receiver(request(seq=3), WEAK)
    settle(node, clock)
    radio.receiver(reply(seq=3, fcnt=2), WEAK)
    settle(node, clock)
    assert radio.sent[-1] == relayed(reply(seq=3, fcnt=2))


def test_a_reply_relay_repeats_once_unless_it_hears_it_carried_on():
    node, radio, clock = relay(hop_retries=1)
    radio.receiver(request(seq=3), WEAK)
    settle(node, clock)
    radio.receiver(reply(seq=3), WEAK)
    settle(node, clock, 10.0)
    assert radio.sent.count(relayed(reply(seq=3))) == 2  # nobody upstream answered

    radio.receiver(request(seq=4, fcnt=2), WEAK)
    settle(node, clock)
    radio.receiver(reply(seq=4, fcnt=2), WEAK)
    while relayed(reply(seq=4, fcnt=2)) not in radio.sent:
        clock.now += 0.01
        node.tick()
    radio.receiver(relayed(reply(seq=4, fcnt=2), by=6), WEAK)  # upstream relay 6 carried it on
    settle(node, clock, 10.0)
    assert radio.sent.count(relayed(reply(seq=4, fcnt=2))) == 1


def test_a_request_relay_does_not_repeat():
    node, radio, clock = relay(hop_retries=3)
    radio.receiver(request(), WEAK)
    settle(node, clock, 10.0)
    assert radio.sent == [relayed(request())]


def test_frames_for_this_node_go_up_and_are_not_relayed():
    node, radio, clock = relay()
    got = []
    node.set_receiver(got.append)
    radio.receiver(request(target=5), WEAK)
    radio.receiver(request(target=5), WEAK)
    settle(node, clock)
    assert got == [request(target=5)] and radio.sent == []


def test_a_forged_frame_cannot_silence_an_origin():
    node, radio, clock = relay()
    forged = bytearray(request(fcnt=0xFFFFFF00))
    forged[-1] ^= 0xFF  # broken MIC
    radio.receiver(bytes(forged), WEAK)
    radio.receiver(request(fcnt=5), WEAK)
    settle(node, clock)
    assert node.stats["auth_failed"] == 1
    assert radio.sent == [relayed(request(fcnt=5))]


def test_plain_frames_are_never_relayed():
    node, radio, clock = relay()
    plain = RequestFrame(sender_id=0x42, target_id=9, sequence=1, action=3, value=1).encode()
    radio.receiver(plain, WEAK)
    settle(node, clock)
    assert radio.sent == [] and node.stats["plain_dropped"] == 1


def test_heartbeats_toward_the_controller_are_relayed():
    node, radio, clock = relay()
    beat = HeartbeatFrame(sender_id=9, target_id=0x42, sequence=0, state=NodeState.READY)
    radio.receiver(seal(AEAD, beat.encode(), 1), WEAK)
    settle(node, clock)
    assert len(radio.sent) == 1


# ------------------------------------------------------- whole networks


def build(positions, **options):
    clock = FakeClock()
    network = MeshNetwork(positions, key=KEY, clock=clock, **options)
    controller = network.controller(network.gateway, clock=clock, timeout=8.0, max_retries=2)
    return clock, network, controller


def run(clock, network, controller, futures, limit=60.0) -> None:
    end = clock.now + limit
    while clock.now < end and not (all(f.done() for f in futures) and network.quiet()):
        clock.now += 0.005
        network.tick(clock.now)
        controller.tick(clock.now)


LINE = {node: (80.0 * node, 0.0) for node in range(1, 6)}  # 5 hops, range 100


def test_a_request_crosses_five_hops_and_runs_once():
    clock, network, controller = build(LINE, seed=1)
    future = controller.request(5, 3, 1)
    run(clock, network, controller, [future])

    assert future.result(timeout=0).ok
    assert network.devices[5].handled_count == 1
    assert [network.devices[n].handled_count for n in range(1, 5)] == [0, 0, 0, 0]


def test_a_rebooted_relay_keeps_only_the_controllers_mark_and_the_line_still_works():
    clock, network, controller = build(LINE, seed=1)
    run(clock, network, controller, [controller.request(5, 3, 1)])
    middle = network.nodes[3]
    gateway = middle.controller_id
    heard = middle.relay.mark(gateway)
    assert heard is not None and middle.secure.mark(gateway) is None  # it only relayed
    assert middle.relay.mark(5) is not None  # and the reply

    network.set_power(3, False)
    network.set_power(3, True)
    assert middle.relay.mark(gateway) == middle.secure.mark(gateway) == heard
    assert middle.relay.mark(5) is None

    future = controller.request(5, 3, 0)
    run(clock, network, controller, [future])
    assert future.result(timeout=0).ok and network.devices[5].handled_count == 2


def test_an_end_to_end_retry_floods_again():
    clock, network, controller = build(LINE, seed=1)
    network.medium.port(5).enabled = False  # the target is away for the first try
    future = controller.request(5, 3, 1)
    while clock.now < 9.0:
        clock.now += 0.005
        network.tick(clock.now)
        controller.tick(clock.now)
    network.medium.port(5).enabled = True
    run(clock, network, controller, [future])

    outcome = future.result(timeout=0)
    assert outcome.ok and outcome.attempts >= 2
    assert network.devices[5].handled_count == 1


def test_an_unreachable_node_fails_and_the_network_falls_quiet():
    positions = {**LINE, 9: (5000.0, 0.0)}
    clock, network, controller = build(positions, record=True)
    controller.confirm.window = None  # counts the request's own tries only
    future = controller.request(9, 3, 1)
    run(clock, network, controller, [future])

    assert future.result(timeout=0).result is None
    assert network.quiet()
    # three end-to-end tries, each at most one transmission per node plus
    # the controller's own repeat
    assert len(network.medium.log) <= 3 * (len(LINE) + 2)


def test_no_node_ever_resends_a_transmission_more_than_the_rules_allow():
    positions = scatter(40, 300.0, random.Random(3))  # dense: everyone hears many
    clock, network, controller = build(positions, record=True, controller_position=(150.0, 150.0))
    futures = []
    for node in list(positions)[:10]:
        futures.append(controller.request(node, 3, 1))
        run(clock, network, controller, futures[-1:])

    assert all(f.result(timeout=0).ok for f in futures)
    sends = Counter()
    for _, node, raw in network.medium.log:
        sends[(raw[1], int.from_bytes(raw[4:8], "little"), node)] += 1
    # 1 send per node and transmission, plus one repeat (own frames, replies)
    assert max(sends.values()) <= 2
    for (origin, _, node), count in sends.items():
        if count == 2:
            raw = next(r for _, n, r in network.medium.log if n == node and r[1] == origin)
            assert node == origin or raw[0] & 0x1F == 0x02


def test_managed_flooding_beats_a_naive_flood_in_a_dense_network():
    positions = scatter(40, 300.0, random.Random(5))
    naive = dict(backoff=(0.0, 0.02), suppress_after=None, guided_replies=False, hop_retries=0)
    results = {}
    for name, options in (("naive", naive), ("managed", {})):
        clock, network, controller = build(
            positions, relay_options=options, controller_position=(150.0, 150.0), drop=0.05, seed=5
        )
        controller.confirm.window = None  # compares the floods, not the status query
        futures = []
        for node in positions:
            futures.append(controller.request(node, 3, 1))
            run(clock, network, controller, futures[-1:])
        delivered = sum(f.result(timeout=0).ok for f in futures)
        results[name] = (delivered, network.medium.stats["transmissions"], network.medium.stats["collisions"])
        assert all(d.handled_count <= 1 for d in network.devices.values())

    assert results["managed"][0] == len(positions)
    assert results["managed"][0] > results["naive"][0]
    assert results["managed"][2] < results["naive"][2]


def test_reserved_ids_are_refused():
    with pytest.raises(ValueError):
        MeshNetwork({0x42: (0.0, 0.0)}, key=KEY)
    with pytest.raises(ValueError):
        MeshNetwork({0xFF: (0.0, 0.0)}, key=KEY)


def test_a_probe_crossing_a_weak_hop_waits_longer_than_one_over_a_strong_hop():
    from cairnmesh.frames import LinkQuality

    node, _, _ = relay()
    strong = (LinkQuality(-90, 40),)  # SNR +10 dB
    weak = (LinkQuality(-120, -24),)  # SNR −6 dB, just above the floor
    low, high = node.probe_backoff
    assert max(node._probe_delay(strong) for _ in range(50)) < low + 0.3 * (high - low)
    assert min(node._probe_delay(weak) for _ in range(50)) > low + 0.75 * (high - low)


def test_the_path_cost_counts_the_chance_of_breaking():
    from cairnmesh.frames import LinkQuality

    node, _, _ = relay()
    marginal = (LinkQuality(-118, -16),)  # SNR −4 dB
    plain, _, _ = relay(etx_break=0.0)
    assert node.path_etx(marginal) > plain.path_etx(marginal) + 10
    # Two strong hops now beat one marginal hop.
    strong = (LinkQuality(-90, 40), LinkQuality(-90, 40))
    assert node.path_etx(strong) < node.path_etx(marginal)
