"""Deterministic tests for the link protocol.

Everything is driven by a fake clock and explicit ``tick()`` calls, so no
test sleeps or depends on thread timing.
"""

from __future__ import annotations

import pytest

from cairnmesh.channel import SimulatedMedium
from cairnmesh.controller import Controller
from cairnmesh.device import Device
from cairnmesh.events import (
    FrameRejected,
    HeartbeatReceived,
    ReplyIgnored,
    ReplyReceived,
    RequestFailed,
    RequestSent,
)
from cairnmesh.frames import (
    BROADCAST_ID,
    FrameKind,
    HeartbeatFrame,
    NodeState,
    ReplyFrame,
    ReplyResult,
    RequestFrame,
    WakeFrame,
    decode_frame,
)
from cairnmesh.sim import SimulatedNetwork

from lora_helpers import FakeClock, ScriptedRng, build, run


def test_heartbeat_frame_roundtrip():
    frame = HeartbeatFrame(sender_id=3, target_id=0x42, sequence=7, state=NodeState.READY)
    assert decode_frame(frame.encode()) == frame


def test_frame_fields_are_range_checked():
    with pytest.raises(ValueError):
        RequestFrame(sender_id=1, target_id=2, sequence=3, action=4, value=256)


def test_request_ack_resolves_future():
    clock, medium, devices, controller, events = build()
    future = controller.request(1, action=3, value=100)
    run(clock, medium, devices, controller, 0.2)

    outcome = future.result(timeout=0)
    assert outcome.ok and outcome.attempts == 1
    assert devices[1].handled_count == 1
    assert any(isinstance(e, ReplyReceived) and e.ok for e in events)


def test_lost_reply_is_retried_but_handled_once():
    clock, medium, devices, controller, events = build(uplink_drop=0.5)
    # Deterministic loss: the 1st draw is the downlink copy (kept), the 2nd is
    # the first uplink reply (dropped); everything after is kept.
    medium.rng = ScriptedRng([1.0, 0.0])
    future = controller.request(1, action=3, value=100)
    run(clock, medium, devices, controller, 1.5)

    outcome = future.result(timeout=0)
    assert outcome.ok and outcome.attempts == 2
    assert devices[1].handled_count == 1
    retries = [e for e in events if isinstance(e, RequestSent) and e.attempt > 1]
    assert len(retries) == 1


def test_request_fails_after_retries_exhausted():
    clock, medium, devices, controller, events = build(downlink_drop=1.0)
    controller.confirm.window = None  # no status query: plain retry exhaustion
    future = controller.request(1, action=1, value=0)
    run(clock, medium, devices, controller, 3.0)

    outcome = future.result(timeout=0)
    assert not outcome.ok and outcome.result is None
    assert outcome.attempts == 4
    assert any(isinstance(e, RequestFailed) for e in events)


def test_requests_to_one_node_are_serialised_per_node():
    clock, medium, devices, controller, events = build(node_ids=(1, 2))
    first = controller.request(1, action=1, value=1)
    second = controller.request(1, action=2, value=2)
    other = controller.request(2, action=3, value=3)
    assert controller.pending_count(1) == 2
    sent_now = [e.node_id for e in events if isinstance(e, RequestSent)]
    assert sent_now == [1, 2]  # second request to node 1 waits its turn

    run(clock, medium, devices, controller, 0.3)
    assert all(f.result(timeout=0).ok for f in (first, second, other))
    assert devices[1].handled_count == 2 and devices[2].handled_count == 1


def test_wake_switches_sleeping_device_to_ready():
    clock, medium, devices, controller, events = build(awake=False)
    # A sleeping device answers after 1~3 s; give the wake enough retries.
    future = controller.wake(1, max_retries=8)
    run(clock, medium, devices, controller, 5.0)

    assert future.result(timeout=0).ok
    assert devices[1].state == NodeState.READY


def test_late_wake_does_not_reset_request_dedup():
    clock, medium, devices, controller, events = build()
    device = devices[1]
    request = RequestFrame(sender_id=0x42, target_id=1, sequence=1, action=3, value=100)

    device.handle_frame(WakeFrame(sender_id=0x42, target_id=1, sequence=0))
    device.handle_frame(request)
    # A WAKE retry deferred while sleeping is processed after the request ...
    device.handle_frame(WakeFrame(sender_id=0x42, target_id=1, sequence=0))
    # ... and must not make the request retry look new.
    device.handle_frame(request)

    assert device.handled_count == 1
    assert device.last_sequence == 1


def test_heartbeats_reach_controller():
    clock, medium, devices, controller, events = build(heartbeat_interval=1.0)
    run(clock, medium, devices, controller, 3.5)

    beats = [e for e in events if isinstance(e, HeartbeatReceived)]
    assert len(beats) >= 2
    assert [b.sequence for b in beats] == list(range(len(beats)))


def test_unmatched_reply_and_garbage_are_reported_not_raised():
    clock, medium, devices, controller, events = build()
    controller.on_frame(ReplyFrame(sender_id=1, target_id=0x42, sequence=9, result=0).encode())
    controller.on_frame(b"\x99\x00")
    controller.on_frame(b"")

    assert isinstance(events[0], ReplyIgnored)
    assert sum(isinstance(e, FrameRejected) for e in events) == 2


def test_powered_off_node_goes_silent():
    clock = FakeClock()
    network = SimulatedNetwork([1], heartbeat_interval=1.0, downlink_drop=0, uplink_drop=0, seed=1, clock=clock)
    controller = Controller(network.gateway, clock=clock)
    beats = []
    controller.events.subscribe(lambda e: isinstance(e, HeartbeatReceived) and beats.append(e))

    network.set_power(1, False)
    for _ in range(300):
        clock.now += 0.01
        network.tick()
    assert beats == []


def test_sequences_are_per_node_so_other_traffic_cannot_cause_false_duplicates():
    clock, medium, devices, controller, events = build(node_ids=(1, 2))
    controller.request(1, action=1, value=1)
    run(clock, medium, devices, controller, 0.2)
    # Exactly one wrap-around of traffic to another node ...
    for _ in range(255):
        controller.build_request(action=0, value=0, target_id=2)
    # ... must not make the next request to node 1 reuse its last sequence.
    future = controller.request(1, action=1, value=2)
    run(clock, medium, devices, controller, 0.2)

    assert future.result(timeout=0).ok
    assert devices[1].handled_count == 2


def test_out_of_order_old_retry_is_not_executed_again():
    clock, medium, devices, controller, events = build()
    device = devices[1]
    first = RequestFrame(sender_id=0x42, target_id=1, sequence=0, action=1, value=1)
    second = RequestFrame(sender_id=0x42, target_id=1, sequence=1, action=1, value=2)

    device.handle_frame(first)
    device.handle_frame(second)
    device.handle_frame(first)  # a delayed retry of the first request

    assert device.handled_count == 2


def test_duplicate_request_replays_original_result():
    class FailingDevice(Device):
        def perform_action(self, frame):
            super().perform_action(frame)
            return ReplyResult.ERROR

    clock = FakeClock()
    medium = SimulatedMedium(clock=clock)
    device = FailingDevice(medium.node(1), 1, awake=True, clock=clock)
    request = RequestFrame(sender_id=0x42, target_id=1, sequence=4, action=1, value=0)

    assert device.handle_frame(request).result == ReplyResult.ERROR
    assert device.handle_frame(request).result == ReplyResult.ERROR
    assert device.handled_count == 1


def test_dedup_cache_survives_a_reboot():
    # A retry still queued at a relay can arrive after the target rebooted
    # (measured at 30 % loss): it must be answered, not run again.
    clock = FakeClock()
    medium = SimulatedMedium(clock=clock)
    device = Device(medium.node(1), 1, awake=True, clock=clock)
    request = RequestFrame(sender_id=0x42, target_id=1, sequence=4, action=1, value=0)

    first = device.handle_frame(request)
    device.reset()
    clock.now += 5.0
    assert device.handle_frame(request).result == first.result
    assert device.handled_count == 0  # reset zeroes the counter: nothing ran since

    clock.now += device.dedup_ttl + 1.0
    device.handle_frame(request)  # past the TTL the id is free again
    assert device.handled_count == 1


def test_dedup_cache_is_bounded():
    clock = FakeClock()
    medium = SimulatedMedium(clock=clock)
    device = Device(medium.node(1), 1, awake=True, dedup_window=2, clock=clock)
    for sequence in range(5):
        device.handle_frame(
            RequestFrame(sender_id=0x42, target_id=1, sequence=sequence, action=1, value=0)
        )

    assert len(device._recent[0x42]) == 2
    assert device.last_sequence == 4


def test_reliable_broadcast_is_rejected():
    clock, medium, devices, controller, events = build()
    with pytest.raises(ValueError):
        controller.request(BROADCAST_ID, action=1, value=0)
    with pytest.raises(ValueError):
        controller.wake(BROADCAST_ID)
    with pytest.raises(ValueError):
        controller.submit(WakeFrame(sender_id=0x42, target_id=BROADCAST_ID, sequence=0))


def test_device_ignores_broadcast_requests():
    clock, medium, devices, controller, events = build()
    request = RequestFrame(sender_id=0x42, target_id=BROADCAST_ID, sequence=0, action=1, value=0)
    devices[1].on_frame(request.encode())

    assert devices[1].handled_count == 0


def test_futures_cannot_be_cancelled_and_resolve_normally():
    clock, medium, devices, controller, events = build(node_ids=(1, 2))
    first = controller.request(1, action=1, value=1)
    second = controller.request(2, action=1, value=1)

    assert first.cancel() is False
    run(clock, medium, devices, controller, 0.2)
    assert first.result(timeout=0).ok and second.result(timeout=0).ok


def test_heartbeat_state_is_validated():
    with pytest.raises(ValueError):
        HeartbeatFrame(sender_id=1, target_id=0x42, sequence=0, state=7)
    with pytest.raises(ValueError):
        decode_frame(bytes([FrameKind.HEARTBEAT, 1, 0x42, 0, 7]))
    frame = HeartbeatFrame(sender_id=1, target_id=0x42, sequence=0, state=1)
    assert frame.state is NodeState.READY


def test_frame_fields_reject_non_integers():
    with pytest.raises(ValueError):
        WakeFrame(sender_id=True, target_id=1, sequence=0)


def test_deferred_frames_are_handled_in_due_order():
    clock = FakeClock()
    medium = SimulatedMedium(clock=clock)
    device = Device(medium.node(1), 1, sleep_delay=(1.0, 3.0), clock=clock, rng=ScriptedRng([]))
    handled = []
    device.handle_frame = lambda frame: handled.append(frame.sequence)
    delays = iter([2.0, 1.0])
    device.rng.uniform = lambda low, high: next(delays, low)

    device.on_frame(RequestFrame(sender_id=0x42, target_id=1, sequence=0, action=1, value=0).encode())
    device.on_frame(RequestFrame(sender_id=0x42, target_id=1, sequence=1, action=1, value=0).encode())
    clock.now += 3.0
    device.tick()

    assert handled == [1, 0]
