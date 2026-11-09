"""Module events (EVENT, wire format v4; ``uplink.py``, spec section 19).

Unit tests drive one ``EventReporter`` with fakes; the network tests run a
small line, dormant until the gateway wakes it, on a fake clock.
"""

from __future__ import annotations

import random

import pytest

pytest.importorskip("cryptography")

from cairnmesh.airtime import airtime_for
from cairnmesh.controller import Controller
from cairnmesh.events import NodeEvent
from cairnmesh.frames import (
    APP_KINDS,
    EventFrame,
    FrameKind,
    ReplyFrame,
    ReplyResult,
    decode_frame,
)
from cairnmesh.liveness import NeighbourWatch
from cairnmesh.mesh_sim import MeshNetwork, line
from cairnmesh.uplink import EventReporter

KEY = bytes(range(16))


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class Sent:
    """Stands in for the secure layer: records plain frames."""

    def __init__(self) -> None:
        self.frames = []
        self.receiver = None

    def send(self, data: bytes) -> None:
        self.frames.append(decode_frame(data))

    def set_receiver(self, callback) -> None:
        self.receiver = callback

    def events(self):
        return [f for f in self.frames if isinstance(f, EventFrame)]


class Relay:
    def __init__(self) -> None:
        self.observer = None
        self.observers = []


def reporter(**options):
    clock, sent, relay = FakeClock(), Sent(), Relay()
    node = EventReporter(
        sent,
        relay,
        5,
        epoch=9,
        event_delay=(1.0, 1.0),
        wake_spread=(2.0, 2.0),
        event_timeout=10.0,
        event_backoff_max=40.0,
        clock=clock,
        rng=random.Random(1),
        **options,
    )
    return node, sent, relay, clock


def advance(node, clock, seconds, step=0.1):
    for _ in range(int(round(seconds / step))):
        clock.now = round(clock.now + step, 6)
        node.tick(clock.now)


def ack(sequence: int, epoch: int = 9, target: int = 5) -> bytes:
    return ReplyFrame(sender_id=0x42, target_id=target, sequence=sequence, epoch=epoch, result=ReplyResult.OK).encode()


# ------------------------------------------------------------- frame


def test_the_event_frame_round_trips_and_carries_the_application_layer():
    frame = EventFrame(sender_id=3, target_id=0x42, sequence=7, event_id=0xA5, epoch=1)
    assert frame.encode() == bytes([0x13, 3, 0x42, 7, 1, 0xA5])
    assert decode_frame(frame.encode()) == frame
    assert frame.message_id == 1 << 8 | 7
    assert APP_KINDS[FrameKind.EVENT] == 6
    with pytest.raises(ValueError):
        decode_frame(bytes([0x13, 3, 0x42, 7, 1]))  # one byte short
    with pytest.raises(ValueError):
        EventFrame(sender_id=3, target_id=0x42, sequence=7, event_id=256)


# ------------------------------------------------------------- the queue


def test_an_event_goes_out_after_its_delay_and_stops_once_acknowledged():
    node, sent, relay, clock = reporter()
    assert node.notify(7)
    advance(node, clock, 0.9)
    assert sent.events() == []
    advance(node, clock, 0.2)
    [frame] = sent.events()
    assert (frame.sender_id, frame.target_id, frame.event_id, frame.epoch) == (5, 0x42, 7, 9)
    relay.observers[0](ack(frame.sequence), None)
    assert node.pending() == () and node.stats["events_acked"] == 1
    advance(node, clock, 100.0)
    assert len(sent.events()) == 1


def test_unanswered_it_goes_again_doubling_up_to_the_cap_and_is_never_given_up():
    node, sent, _, clock = reporter()
    node.notify(7)
    times = []
    for _ in range(int(400 / 0.1)):
        clock.now = round(clock.now + 0.1, 6)
        before = len(sent.frames)
        node.tick(clock.now)
        if len(sent.frames) > before:
            times.append(clock.now)
    gaps = [b - a for a, b in zip(times, times[1:])]
    # 10, 20, 40, 40, ... s, each ±25 %.
    assert 7.4 <= gaps[0] <= 12.6 and 14.9 <= gaps[1] <= 25.1
    assert all(29.9 <= gap <= 50.1 for gap in gaps[2:]) and len(gaps) >= 8
    assert node.pending() == (7,)


def test_the_same_id_is_merged_and_a_full_queue_refuses():
    node, sent, _, clock = reporter(max_events=2)
    assert node.notify(1) and node.notify(1) and node.notify(2)
    assert not node.notify(3)
    assert node.pending() == (1, 2)
    assert node.stats["events_merged"] == 1 and node.stats["events_refused"] == 1
    advance(node, clock, 1.5)
    assert sorted(f.event_id for f in sent.events()) == [1, 2]
    with pytest.raises(ValueError):
        node.notify(256)


def test_an_event_raised_again_after_its_acknowledgement_is_a_new_message():
    node, sent, relay, clock = reporter()
    node.notify(7)
    advance(node, clock, 1.5)
    first = sent.events()[0]
    relay.observers[0](ack(first.sequence), None)
    node.notify(7)
    advance(node, clock, 1.5)
    second = sent.events()[1]
    assert second.event_id == 7 and second.sequence != first.sequence


def test_only_the_controllers_acknowledgement_of_a_sent_event_counts():
    node, sent, relay, clock = reporter()
    node.notify(7)
    sequence = node._events[7].sequence
    relay.observers[0](ack(sequence), None)  # not sent yet: an old id's acknowledgement
    assert node.pending() == (7,)
    advance(node, clock, 1.5)
    observer = relay.observers[0]
    observer(ack(sequence, epoch=8), None)  # another run of this node
    observer(ack(sequence, target=6), None)  # another node
    observer(ReplyFrame(sender_id=0x43, target_id=5, sequence=sequence, epoch=9, result=0).encode(), None)
    observer(bytes([FrameKind.REPLY | 0x20]) + ack(sequence)[1:] + bytes(5), None)  # a RESULT's (app layer)
    assert node.pending() == (7,)
    observer(ack(sequence), None)
    assert node.pending() == ()


def test_waking_sends_what_waited_within_the_spread_at_the_fast_pace_again():
    node, sent, _, clock = reporter()
    node.notify(7)
    advance(node, clock, 200.0)  # unanswered: well into the backoff
    count = len(sent.events())
    node.resume(clock.now)
    advance(node, clock, 1.9)
    assert len(sent.events()) == count
    advance(node, clock, 0.2)
    assert len(sent.events()) == count + 1
    advance(node, clock, 12.6)  # the first retry again (10 s ±25 %)
    assert len(sent.events()) == count + 2


def test_a_reboot_loses_the_queue():
    node, _, _, _ = reporter()
    node.notify(7)
    node.forget()
    assert node.pending() == () and node.next_due() == float("inf")


def test_events_and_reports_share_the_nodes_counter():
    clock, sent, relay = FakeClock(), Sent(), Relay()
    watch = NeighbourWatch(sent, relay, 5, epoch=9, clock=clock, rng=random.Random(1))
    node = EventReporter(sent, relay, 5, epoch=9, next_sequence=watch.next_sequence, clock=clock)
    first = watch.next_sequence()
    node.notify(7)
    assert node._events[7].sequence == (first + 1) & 0xFF
    assert watch.next_sequence() == (first + 2) & 0xFF


# ------------------------------------------------------------- controller


def test_the_controller_acknowledges_every_copy_and_publishes_once():
    sent = Sent()
    controller = Controller(sent, timeout=0.1, max_retries=0)
    published = []
    controller.events.subscribe(published.append)
    frame = EventFrame(sender_id=5, target_id=0x42, sequence=3, event_id=7, epoch=9)
    controller.on_frame(frame.encode())
    controller.on_frame(frame.encode())
    assert [f for f in sent.frames] == [ReplyFrame(0x42, 5, 3, result=ReplyResult.OK, epoch=9)] * 2
    events = [e for e in published if isinstance(e, NodeEvent)]
    assert [(e.node_id, e.event_id) for e in events] == [(5, 7)]
    controller.on_frame(EventFrame(sender_id=5, target_id=0x41, sequence=4, event_id=7, epoch=9).encode())
    assert len(sent.frames) == 2  # for another controller


def test_a_week_without_an_acknowledgement_does_not_overflow_the_backoff():
    node, sent, _, clock = reporter()
    node.notify(7)
    advance(node, clock, 1.1)
    node._events[7].sent = 2000  # about a week of unanswered retries at the cap
    clock.now += 100.0
    node.tick(clock.now)
    assert len(sent.events()) == 2 and node.pending() == (7,)
    assert 29.9 <= node.next_due() - clock.now <= 50.1  # still at the cap


def test_a_retry_after_many_other_nodes_reported_is_still_published_once():
    sent = Sent()
    controller = Controller(sent, timeout=0.1, max_retries=0)
    published = []
    controller.events.subscribe(published.append)
    first = EventFrame(sender_id=5, target_id=0x42, sequence=3, event_id=7, epoch=9)
    controller.on_frame(first.encode())
    for node in range(10, 90):  # a whole network flushing on a wake
        controller.on_frame(EventFrame(sender_id=node, target_id=0x42, sequence=1, event_id=1, epoch=2).encode())
    controller.on_frame(first.encode())  # its acknowledgement was lost
    events = [(e.node_id, e.event_id) for e in published if isinstance(e, NodeEvent)]
    assert events.count((5, 7)) == 1 and len(events) == 81
    assert len(sent.frames) == 82  # every copy acknowledged


# ------------------------------------------------------------- network


def run(network, clock, seconds, step=0.01):
    end = clock.now + seconds
    while clock.now < end:
        clock.now = round(clock.now + step, 6)
        network.tick(clock.now)


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_events_raised_in_a_sleeping_network_arrive_once_the_gateway_wakes_it(seed):
    clock = FakeClock()
    network = MeshNetwork(line(5), key=KEY, clock=clock, airtime=airtime_for(7), seed=seed,
                          wake_options={"period": 0.5})
    controller = network.controller(network.gateway, clock=clock)
    got = []
    controller.events.subscribe(lambda e: got.append(e) if isinstance(e, NodeEvent) else None)
    run(network, clock, 5.0)
    assert network.nodes[5].notify(7) and network.nodes[3].notify(9)
    run(network, clock, 60.0)
    assert got == [] and all(not agent.awake for agent in network.wakers.values())  # held, nothing sent
    assert network.nodes[5].events.stats["events_sent"] == 0
    network.waker.wake(60)
    run(network, clock, 40.0)
    assert sorted((e.node_id, e.event_id) for e in got) == [(3, 9), (5, 7)]
    assert network.nodes[5].events.pending() == () and network.nodes[3].events.pending() == ()
