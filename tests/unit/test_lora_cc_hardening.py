"""Regression tests for protocol robustness fixes.

Same style as ``test_link_demo.py``: a fake clock and explicit ``tick()``
calls, no sleeping.
"""

from __future__ import annotations

import pytest

from cairnmesh.controller import Controller, QueueFull
from cairnmesh.device import Device
from cairnmesh.events import LinkError, RequestSent, ReplyReceived
from cairnmesh.frames import (
    NodeState,
    ReplyResult,
    RequestFrame,
    WakeFrame,
    decode_frame,
)
from cairnmesh.session import LinkSession
from cairnmesh.sim import SimulatedNetwork


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class RecordingTransport:
    def __init__(self) -> None:
        self.sent: list[bytes] = []
        self.fail = False
        self.receiver = None

    def send(self, data: bytes) -> None:
        if self.fail:
            raise OSError("port gone")
        self.sent.append(data)

    def set_receiver(self, callback) -> None:
        self.receiver = callback


def request(sender=0x42, sequence=1, value=0) -> RequestFrame:
    return RequestFrame(sender_id=sender, target_id=1, sequence=sequence, action=1, value=value)


# ------------------------------------------------------- controller: sending


def test_transport_failure_never_strands_a_future():
    clock, transport = FakeClock(), RecordingTransport()
    controller = Controller(transport, timeout=1.0, max_retries=0, confirm_window=None, clock=clock)
    errors = []
    controller.events.subscribe(lambda e: isinstance(e, LinkError) and errors.append(e))
    first = controller.request(1, 3, 1)
    second = controller.request(1, 3, 2)

    transport.fail = True
    clock.now += 2.0
    controller.tick()  # first runs out of retries, second is sent -> send fails

    assert first.done() and not first.result(timeout=0).ok
    assert errors and "send failed" in errors[0].message
    assert controller.pending_count(1) == 1  # the second frame stays queued


def test_submit_survives_a_failing_transport_and_retries_later():
    clock, transport = FakeClock(), RecordingTransport()
    transport.fail = True
    controller = Controller(transport, timeout=1.0, max_retries=3, clock=clock)

    future = controller.request(1, 3, 1)  # must not raise
    assert not future.done() and controller.pending_count(1) == 1

    transport.fail = False
    clock.now += 1.0
    controller.tick()
    assert len(transport.sent) == 1  # the retry went out


def test_request_sent_is_published_before_a_synchronous_reply():
    class Loopback(RecordingTransport):
        def send(self, data: bytes) -> None:
            frame = decode_frame(data)
            reply = bytes([0x02, frame.target_id, frame.sender_id, frame.sequence, frame.epoch, 0])
            self.receiver(reply)  # reply arrives inside send()

    events = []
    controller = Controller(Loopback(), clock=FakeClock())
    controller.events.subscribe(events.append)
    controller.request(1, 3, 1)

    kinds = [type(e) for e in events]
    assert kinds.index(RequestSent) < kinds.index(ReplyReceived)


def test_queue_per_node_is_bounded():
    controller = Controller(RecordingTransport(), max_queue=2, clock=FakeClock())
    controller.request(1, 1, 0)
    controller.request(1, 1, 1)
    with pytest.raises(QueueFull):
        controller.request(1, 1, 2)
    controller.request(2, 1, 0)  # other nodes are unaffected


def test_invalid_fields_do_not_consume_a_sequence_number():
    controller = Controller(RecordingTransport(), initial_sequence=10, clock=FakeClock())
    with pytest.raises(ValueError):
        controller.build_request(action=1, value=999, target_id=1)
    assert controller.build_request(action=1, value=1, target_id=1).sequence == 10


def test_sequence_start_is_random_per_node_unless_pinned():
    pinned = Controller(RecordingTransport(), initial_sequence=7, clock=FakeClock())
    assert [pinned.build_wake(1).sequence, pinned.build_wake(1).sequence] == [7, 8]

    starts = {
        Controller(RecordingTransport(), clock=FakeClock()).build_wake(1).sequence
        for _ in range(40)
    }
    assert len(starts) > 1


def test_wake_gets_more_retries_than_a_request_by_default():
    controller = Controller(RecordingTransport(), max_retries=5, clock=FakeClock())
    assert controller.wake_max_retries >= 10
    sent = []
    controller.events.subscribe(lambda e: isinstance(e, RequestSent) and sent.append(e))
    controller.wake(1)
    controller.request(2, 1, 0)
    assert [e.max_attempts for e in sent] == [controller.wake_max_retries + 1, 6]


def test_wake_to_a_sleeping_node_succeeds_on_a_lossy_link():
    failures = 0
    for seed in range(60):
        clock = FakeClock()
        network = SimulatedNetwork(
            [1], downlink_drop=0.1, uplink_drop=0.1, seed=seed, clock=clock
        )
        controller = Controller(network.gateway, clock=clock)
        future = controller.wake(1)
        for _ in range(3000):
            clock.now += 0.01
            network.tick()
            controller.tick()
            if future.done():
                break
        failures += not (future.done() and future.result(timeout=0).ok)
    assert failures == 0


# ------------------------------------------------------------ device: dedup


def test_dedup_is_keyed_by_sender_as_well_as_sequence():
    clock = FakeClock()
    device = Device(RecordingTransport(), 1, awake=True, clock=clock)
    device.handle_frame(request(sender=0x42, sequence=7))
    device.handle_frame(request(sender=0x43, sequence=7))  # another controller
    assert device.handled_count == 2

    device.handle_frame(request(sender=0x43, sequence=7))  # its own retry
    assert device.handled_count == 2


def test_dedup_entries_expire_so_a_wrapped_or_restarted_sequence_is_new():
    clock = FakeClock()
    device = Device(RecordingTransport(), 1, awake=True, dedup_ttl=30.0, clock=clock)
    device.handle_frame(request(sequence=5))
    clock.now += 10.0
    device.handle_frame(request(sequence=5))  # inside the retry window: duplicate
    assert device.handled_count == 1

    clock.now += 40.0  # a restarted controller reuses sequence 5
    device.handle_frame(request(sequence=5))
    assert device.handled_count == 2


def test_dedup_ttl_none_disables_expiry():
    clock = FakeClock()
    device = Device(RecordingTransport(), 1, awake=True, dedup_ttl=None, clock=clock)
    device.handle_frame(request(sequence=5))
    clock.now += 10_000.0
    device.handle_frame(request(sequence=5))
    assert device.handled_count == 1


def test_failing_action_replies_error_and_is_not_rerun():
    class Exploding(Device):
        def perform_action(self, frame):
            self.handled_count += 1
            raise RuntimeError("boom")

    transport = RecordingTransport()
    device = Exploding(transport, 1, awake=True, clock=FakeClock())
    first = device.handle_frame(request(sequence=3))
    retry = device.handle_frame(request(sequence=3))

    assert first.result == ReplyResult.ERROR and retry.result == ReplyResult.ERROR
    assert device.handled_count == 1
    assert len(transport.sent) == 2  # both got a reply


# ------------------------------------------------ device: bounded sleep queue


def sleeping_device(**options):
    clock = FakeClock()
    device = Device(
        RecordingTransport(), 1, sleep_delay=(1.0, 1.0), clock=clock, **options
    )
    return clock, device


def test_retransmission_of_a_waiting_frame_is_not_queued_twice():
    clock, device = sleeping_device()
    for _ in range(4):
        device.on_frame(request(sequence=2).encode())

    assert len(device._deferred) == 1
    assert device.deferred_dropped == 3


def test_sleep_queue_is_capped_and_drops_like_a_lost_frame():
    clock, device = sleeping_device(max_deferred=3)
    for sequence in range(10):
        device.on_frame(request(sequence=sequence).encode())

    assert len(device._deferred) == 3
    clock.now += 2.0
    device.tick()
    assert device.handled_count == 3


def test_sleeping_device_still_wakes():
    clock, device = sleeping_device()
    device.on_frame(WakeFrame(sender_id=0x42, target_id=1, sequence=0).encode())
    clock.now += 1.5
    device.tick()
    assert device.state == NodeState.READY


# ------------------------------------------------------------------ session


def test_loop_steps_are_isolated_so_a_bad_poller_cannot_stop_retransmission():
    transport = RecordingTransport()
    controller = Controller(transport, timeout=0.05, max_retries=3, confirm_window=None)

    def bad_poller(now: float) -> None:
        raise RuntimeError("poller broke")

    session = LinkSession(controller, pollers=[bad_poller], tick_interval=0.005)
    errors = []
    session.events.subscribe(lambda e: isinstance(e, LinkError) and errors.append(e))
    with session:
        future = session.request(1, 3, 1)
        outcome = future.result(timeout=3.0)  # gives up after its retries

    assert outcome.result is None and outcome.attempts == 4
    assert errors
