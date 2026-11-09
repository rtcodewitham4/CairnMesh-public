"""HELLO beacons, neighbour tables and LOST / RECOVERED reports (``liveness.py``).

Unit tests drive one ``NeighbourWatch`` with fakes; the network tests run a
small multi-hop line on a fake clock.
"""

from __future__ import annotations

import random

import pytest

pytest.importorskip("cryptography")

from cryptography.hazmat.primitives.ciphers.aead import AESCCM

from cairnmesh.controller import Controller
from cairnmesh.events import NodeReported
from cairnmesh.frames import (
    HelloFrame,
    NodeState,
    ReplyFrame,
    ReportEvent,
    ReportFrame,
    RequestFrame,
    decode_frame,
)
from cairnmesh.liveness import NeighbourWatch
from cairnmesh.mesh import MeshRelay
from cairnmesh.mesh_sim import MeshNetwork
from cairnmesh.secure import MIC_LEN, seal

KEY = bytes(range(16))
AEAD = AESCCM(KEY, tag_length=MIC_LEN)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class Sent:
    """Stands in for the secure layer: records plain frames."""

    def __init__(self) -> None:
        self.frames = []

    def send(self, data: bytes) -> None:
        self.frames.append(decode_frame(data))

    def set_receiver(self, callback) -> None:
        pass

    def reports(self):
        return [f for f in self.frames if isinstance(f, ReportFrame)]


class Relay:
    observer = None


def watch(**options):
    clock, sent, relay = FakeClock(), Sent(), Relay()
    node = NeighbourWatch(
        sent,
        relay,
        5,
        hello_interval=10.0,
        miss_limit=3,
        report_delay=(1.0, 1.0),
        report_timeout=5.0,
        report_retries=2,
        epoch=9,
        clock=clock,
        rng=random.Random(1),
        **options,
    )
    return node, sent, relay, clock


def hello(sender: int) -> bytes:
    return HelloFrame(sender_id=sender, sequence=0, state=NodeState.READY).encode()


def advance(node, clock, seconds, step=0.1):
    for _ in range(int(seconds / step)):
        clock.now += step
        node.tick(clock.now)


# ------------------------------------------------------------- frames


def test_hello_and_report_frames_round_trip():
    beat = HelloFrame(sender_id=3, sequence=7, state=NodeState.SLEEP)
    report = ReportFrame(
        sender_id=3, target_id=0x42, sequence=7, epoch=1, subject=9, event=ReportEvent.RECOVERED
    )
    assert len(beat.encode()) == 5 and decode_frame(beat.encode()) == beat
    assert len(report.encode()) == 7 and decode_frame(report.encode()) == report
    with pytest.raises(ValueError):
        decode_frame(bytes([0x06, 3, 0x42, 7, 1, 9, 5]))  # unknown event


# ------------------------------------------------------- relay rules


class Radio:
    def __init__(self) -> None:
        self.sent: list[bytes] = []
        self.receiver = None

    def send(self, data: bytes) -> None:
        self.sent.append(data)

    def set_receiver(self, callback) -> None:
        self.receiver = callback


def relay():
    radio, clock = Radio(), FakeClock()
    return MeshRelay(radio, KEY, 5, clock=clock, rng=random.Random(1)), radio, clock


def settle(node, clock, seconds=3.0):
    for _ in range(int(seconds / 0.01)):
        clock.now += 0.01
        node.tick()


def test_a_hello_is_never_relayed_nor_repeated():
    node, radio, clock = relay()
    radio.receiver(seal(AEAD, hello(9), 1), -100.0)
    node.send(seal(AEAD, hello(5), 1))
    settle(node, clock, 5.0)
    assert radio.sent == [seal(AEAD, hello(5), 1)]  # our own, once
    assert node.pending() == 0


def test_frames_heading_for_the_controller_are_never_suppressed():
    node, radio, clock = relay()
    report = ReportFrame(sender_id=9, target_id=0x42, sequence=1, subject=7, event=ReportEvent.LOST)
    sealed = seal(AEAD, report.encode(), 1)
    for _ in range(6):  # five more copies heard while waiting
        radio.receiver(sealed, -100.0)
    settle(node, clock)
    assert radio.sent == [seal(AEAD, report.encode(), 1, tx=5)]  # relayed as node 5

    outward = seal(AEAD, RequestFrame(0x42, 9, 1, 3, 1).encode(), 2)
    for _ in range(6):
        radio.receiver(outward, -100.0)
    settle(node, clock)
    assert len(radio.sent) == 1  # a request spreading out is suppressed


# ------------------------------------------------------- the watch


def test_a_silent_neighbour_is_reported_lost_and_then_recovered():
    node, sent, relay, clock = watch()
    relay.observer(hello(7), -80.0)
    assert node.neighbour_count() == 1
    advance(node, clock, 30.0 + 1.0 + 0.5)  # 3 intervals of silence + report delay

    (lost,) = sent.reports()
    assert (lost.subject, lost.event, lost.epoch) == (7, ReportEvent.LOST, 9)
    assert node.neighbour_count() == 0

    relay.observer(hello(7), -80.0)
    advance(node, clock, 1.5)
    assert sent.reports()[-1].event == ReportEvent.RECOVERED


def test_a_weak_neighbour_going_silent_is_not_reported():
    # Below watch_rssi a link misses HELLOs on its own: its silence proves nothing.
    node, sent, relay, clock = watch()
    relay.observer(hello(7), -121.0)
    advance(node, clock, 30.0 + 1.0 + 0.5)
    assert sent.reports() == [] and node.stats["weak_silent"] == 1
    assert node.neighbour_count() == 0
    relay.observer(hello(7), -121.0)
    advance(node, clock, 1.5)
    assert sent.reports() == [] and node.neighbour_count() == 1


def _heard_every(node, relay, clock, nth: int, intervals: int) -> None:
    for index in range(intervals):
        if index % nth == 0:
            relay.observer(hello(7), -121.0)
        advance(node, clock, 10.0, step=0.5)


def _silent_intervals_until_reported(node, sent, clock, most: int):
    for silent in range(1, most + 1):
        advance(node, clock, 10.0, step=0.5)
        if sent.reports():
            return silent
    return None


def test_a_weak_neighbour_heard_long_enough_is_watched_on_a_longer_fuse():
    node, sent, relay, clock = watch()
    _heard_every(node, relay, clock, 2, 60)  # half its HELLOs get through, the last one 20 s ago
    silent = _silent_intervals_until_reported(node, sent, clock, 40)
    # (1 − rate)^n ≤ weak_false_alarm, the rate counting the silence too:
    # about 20 intervals here, where a strong link needs miss_limit (3).
    assert silent is not None and 14 <= silent <= 25
    lost = sent.reports()[0]  # repeated until acknowledged
    assert (lost.subject, lost.event) == (7, ReportEvent.LOST)
    assert node.stats["lost"] == 1 and node.stats["weak_silent"] == 0


def test_a_neighbour_heard_too_rarely_to_judge_is_never_reported():
    node, sent, relay, clock = watch()
    _heard_every(node, relay, clock, 4, 60)  # a quarter: no fuse up to max_miss is safe
    assert _silent_intervals_until_reported(node, sent, clock, 40) is None
    assert node.stats["lost"] == 0 and node.stats["weak_silent"] >= 1


def test_without_weak_window_a_weak_neighbour_is_never_reported():
    node, sent, relay, clock = watch(weak_window=None)
    _heard_every(node, relay, clock, 1, 30)
    assert _silent_intervals_until_reported(node, sent, clock, 40) is None
    assert node.stats["weak_silent"] == 1


def test_a_neighbour_is_watched_by_its_smoothed_rssi():
    node, sent, relay, clock = watch()
    relay.observer(hello(7), -121.0)
    relay.observer(hello(7), -100.0)  # one strong frame: level ≈ -116
    advance(node, clock, 30.0 + 1.0 + 0.5)
    assert [r.event for r in sent.reports()] == [ReportEvent.LOST]


def test_a_report_is_repeated_until_acknowledged():
    node, sent, relay, clock = watch()
    relay.observer(hello(7), -80.0)
    advance(node, clock, 61.5)
    assert len(sent.reports()) == 3  # first try + report_retries
    assert node.stats["reports_abandoned"] == 1

    node, sent, relay, clock = watch()
    relay.observer(hello(7), -80.0)
    advance(node, clock, 31.5)
    first = sent.reports()[0]
    ack = ReplyFrame(sender_id=0x42, target_id=5, sequence=first.sequence, epoch=9, result=0)
    relay.observer(ack.encode(), -80.0)
    advance(node, clock, 30.0)
    assert len(sent.reports()) == 1 and node.stats["reports_acked"] == 1


def test_another_nodes_report_cancels_ours():
    node, sent, relay, clock = watch()
    relay.observer(hello(7), -80.0)
    advance(node, clock, 30.5)  # lost; our report waits for its delay
    other = ReportFrame(sender_id=8, target_id=0x42, sequence=4, subject=7, event=ReportEvent.LOST)
    relay.observer(other.encode(), -80.0)
    advance(node, clock, 5.0)
    assert sent.reports() == [] and node.stats["reports_suppressed"] == 1


def test_a_flap_shorter_than_the_report_delay_reports_nothing():
    node, sent, relay, clock = watch()
    relay.observer(hello(7), -80.0)
    advance(node, clock, 30.5)
    relay.observer(hello(7), -80.0)  # back before anyone was told
    advance(node, clock, 5.0)
    assert sent.reports() == []


def test_hellos_go_out_every_interval():
    node, sent, relay, clock = watch()
    advance(node, clock, 100.0)
    hellos = [f for f in sent.frames if isinstance(f, HelloFrame)]
    assert 8 <= len(hellos) <= 12  # 10 s ± 15 %


def test_the_neighbour_table_is_bounded():
    node, sent, relay, clock = watch(max_neighbours=4)
    for sender in range(10, 20):
        clock.now += 0.1
        relay.observer(hello(sender), -80.0)
    assert set(node.neighbours()) == {16, 17, 18, 19}


# --------------------------------------------------------- networks

LINE = {node: (80.0 * node, 0.0) for node in range(1, 6)}  # 0x42 at the origin


def build():
    clock = FakeClock()
    network = MeshNetwork(
        # Seeded: with random HELLO phases two neighbours' beacons now and
        # then collide at node 2 in the first 20 s, and the test flaked.
        LINE, key=KEY, clock=clock, hello_interval=5.0, watch_options={"miss_limit": 3}, seed=1
    )
    controller = network.controller(network.gateway, clock=clock)
    reports = []
    controller.events.subscribe(lambda e: reports.append(e) if isinstance(e, NodeReported) else None)
    return clock, network, controller, reports


def run(clock, network, controller, seconds):
    for _ in range(int(seconds / 0.01)):
        clock.now += 0.01
        network.tick(clock.now)
        controller.tick(clock.now)


def test_a_switched_off_node_is_reported_once_and_its_return_too():
    clock, network, controller, reports = build()
    run(clock, network, controller, 20.0)
    assert network.watches[2].neighbours().keys() == {1, 3}
    assert reports == []  # no news while all is well

    network.set_power(3, False)
    run(clock, network, controller, 40.0)
    # 2 reports it; 4 noticed too but is cut off behind 3, like 5
    assert [(r.node_id, r.event) for r in reports] == [(3, ReportEvent.LOST)]

    network.set_power(3, True)
    run(clock, network, controller, 30.0)
    assert (3, ReportEvent.RECOVERED) in [(r.node_id, r.event) for r in reports]
    assert all(r.node_id == 3 for r in reports)  # the reboot raised no false alarm


def test_the_controller_acknowledges_every_copy_but_publishes_once():
    sent = []

    class Wire:
        def send(self, data):
            sent.append(decode_frame(data))

        def set_receiver(self, callback):
            pass

    controller = Controller(Wire())
    events = []
    controller.events.subscribe(events.append)
    report = ReportFrame(
        sender_id=3, target_id=0x42, sequence=5, epoch=2, subject=9, event=ReportEvent.LOST
    )
    controller.on_frame(report.encode())
    controller.on_frame(report.encode())  # a retry: our first ack was lost

    assert [type(e) for e in events] == [NodeReported]
    assert [(f.target_id, f.sequence, f.epoch) for f in sent] == [(3, 5, 2), (3, 5, 2)]
