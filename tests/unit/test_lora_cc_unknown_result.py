"""Unknown result codes are unconfirmed, never DONE. 0x04 was the first unknown code; since wire v5 (2026-11-08) it is INTERRUPTED, so the tests use 0x05 and 0x7F."""

from __future__ import annotations

import pytest

from cairnmesh.controller import (
    ACCEPTED,
    DEDUP_WINDOW,
    DONE,
    NOT_RUN,
    UNCONFIRMED,
    Controller,
    RequestOutcome,
)
from cairnmesh.events import ReplyIgnored, RequestCompleted
from cairnmesh.frames import FrameKind, ReplyFrame, ReplyResult, ResultFrame, decode_frame

from lora_helpers import FakeClock, build, run


class Port:
    """A transport with no peer; tests inject the bytes they want to return."""

    def __init__(self) -> None:
        self.sent: list[bytes] = []
        self.receiver = None

    def send(self, data: bytes) -> None:
        self.sent.append(bytes(data))

    def set_receiver(self, callback) -> None:
        self.receiver = callback


def direct_link(initial_sequence: int = 7):
    clock = FakeClock()
    port = Port()
    controller = Controller(
        port,
        timeout=0.5,
        max_retries=0,
        epoch=9,
        initial_sequence=initial_sequence,
        clock=clock,
    )
    events = []
    controller.events.subscribe(events.append)
    return clock, port, controller, events


def reply(controller: Controller, sequence: int, result: int, epoch: int | None = None) -> ReplyFrame:
    return ReplyFrame(
        sender_id=1,
        target_id=controller.sender_id,
        sequence=sequence,
        epoch=controller.epoch if epoch is None else epoch,
        result=result,
    )


def ignored_reason(events) -> str:
    ignored = [event.reason for event in events if isinstance(event, ReplyIgnored)]
    return ignored[-1] if ignored else ""


def rewrite_next_reply(port, kind: FrameKind, result: int) -> None:
    """Change the result byte of the next outgoing frame of ``kind``."""

    real_send = port.send
    remaining = 1

    def send(data: bytes) -> None:
        nonlocal remaining
        if remaining and data[0] == kind:
            changed = bytearray(data)
            changed[5] = result
            data = bytes(changed)
            remaining -= 1
        real_send(data)

    port.send = send


@pytest.mark.parametrize("raw_result", [0x05, 0x7F])
@pytest.mark.parametrize(
    "frame_type",
    [
        lambda: ReplyFrame(sender_id=1, target_id=0x42, sequence=7, result=0x05, epoch=2),
        lambda: ResultFrame(sender_id=1, target_id=0x42, sequence=7, result=0x05, epoch=2),
    ],
)
def test_frame_decoding_keeps_an_unknown_result_as_an_integer(raw_result, frame_type):
    frame = frame_type()
    frame.result = raw_result
    decoded = decode_frame(frame.encode())
    assert decoded.result == raw_result
    assert type(decoded.result) is int


@pytest.mark.parametrize("raw_result", [0x05, 0x7F])
def test_an_unknown_direct_reply_is_unconfirmed_and_does_not_block_the_queue(raw_result):
    clock, medium, devices, controller, events = build()
    rewrite_next_reply(medium.node(1), FrameKind.REPLY, raw_result)

    first = controller.request(1, action=1, value=0)
    run(clock, medium, devices, controller, 0.2)
    outcome = first.result(timeout=0)

    assert outcome.status == UNCONFIRMED
    assert outcome.status != DONE
    assert outcome.result == raw_result
    assert f"0x{raw_result:02X}" in outcome.reason
    assert any(isinstance(event, ReplyIgnored) and f"0x{raw_result:02X}" in event.reason for event in events)
    assert controller.pending_count(1) == 0

    second = controller.request(1, action=2, value=0)
    run(clock, medium, devices, controller, 0.2)
    assert second.result(timeout=0).status == DONE
    assert devices[1].handled_count == 2


@pytest.mark.parametrize("raw_result", [0x05, 0x7F])
def test_an_unknown_result_after_accepted_has_an_unconfirmed_completion(raw_result):
    clock, medium, devices, controller, events = build()
    devices[1].action_time = 2.0
    first = controller.request(1, action=1, value=0)
    run(clock, medium, devices, controller, 0.2)
    outcome = first.result(timeout=0)
    assert outcome.status == ACCEPTED

    rewrite_next_reply(medium.node(1), FrameKind.RESULT, raw_result)
    run(clock, medium, devices, controller, 2.5)

    assert outcome.completion.result(timeout=0) is None
    completed = [event for event in events if isinstance(event, RequestCompleted)]
    assert len(completed) == 1 and completed[0].result is None
    assert any(isinstance(event, ReplyIgnored) and f"0x{raw_result:02X}" in event.reason for event in events)
    assert controller.pending_count(1) == 0

    devices[1].action_time = 0.0
    second = controller.request(1, action=2, value=0)
    run(clock, medium, devices, controller, 0.2)
    assert second.result(timeout=0).status == DONE
    assert devices[1].handled_count == 2


@pytest.mark.parametrize("raw_result", [0x05, 0x7F])
def test_an_unknown_query_answer_is_unconfirmed_and_does_not_block_the_queue(raw_result):
    clock, medium, devices, controller, events = build(uplink_drop=1.0)
    first = controller.request(1, action=1, value=0)
    run(clock, medium, devices, controller, 2.5)
    assert controller._queues[1][0].asked is not None

    medium.uplink_drop = 0.0
    rewrite_next_reply(medium.node(1), FrameKind.REPLY, raw_result)
    run(clock, medium, devices, controller, 2.0)
    outcome = first.result(timeout=0)

    assert outcome.status == UNCONFIRMED and outcome.queried
    assert outcome.result == raw_result
    assert any(isinstance(event, ReplyIgnored) and f"0x{raw_result:02X}" in event.reason for event in events)
    assert controller.pending_count(1) == 0

    second = controller.request(1, action=2, value=0)
    run(clock, medium, devices, controller, 0.2)
    assert second.result(timeout=0).status == DONE
    assert devices[1].handled_count == 2  # the original ran before its replies were lost


def test_an_unknown_result_before_accepted_is_ignored_and_settled():
    _, port, controller, events = direct_link()
    first = controller.request(1, action=1, value=0)
    pending = controller._queues[1][0]
    message_id = pending.frame.message_id

    unknown = ResultFrame(
        sender_id=1,
        target_id=controller.sender_id,
        sequence=pending.frame.sequence,
        epoch=controller.epoch,
        result=0x05,
    )
    controller.on_frame(unknown.encode())

    assert first.result(timeout=0).status == UNCONFIRMED
    assert "0x05" in ignored_reason(events)
    assert message_id in controller._id_ledger.closed_ids[1]
    assert decode_frame(port.sent[-1]).kind == FrameKind.REPLY  # the RESULT was acknowledged


def test_an_unknown_answer_for_a_parked_request_is_ignored_and_settled():
    clock, _, controller, events = direct_link()
    controller.confirm.window = 0.05
    first = controller.request(1, action=1, value=0)
    clock.now += 0.5
    controller.tick(clock.now)
    assert controller._queues[1][0].confirming

    forced = controller.request(1, action=2, value=0, force=True)
    parked = controller._parked[1][0]
    message_id = parked.original.message_id
    controller.on_frame(reply(controller, parked.query_sequences[-1], 0x05).encode())

    assert first.result(timeout=0).status == UNCONFIRMED
    assert "0x05" in ignored_reason(events)
    assert message_id in controller._id_ledger.closed_ids[1]
    assert not controller._parked.get(1) and not forced.done()


def test_an_a6_late_unknown_answer_has_an_unconfirmed_completion():
    _, _, controller, events = direct_link()
    first = controller.request(1, action=1, value=0)
    pending = controller._queues[1][0]
    message_id = pending.frame.message_id
    controller.on_frame(reply(controller, pending.frame.sequence, ReplyResult.ACCEPTED).encode())
    outcome = first.result(timeout=0)
    assert outcome.status == ACCEPTED

    second = controller.request(1, action=2, value=0)  # X is no longer the queue head
    controller.on_frame(reply(controller, pending.frame.sequence, 0x05).encode())

    assert outcome.completion.result(timeout=0) is None
    completed = [event for event in events if isinstance(event, RequestCompleted)]
    assert len(completed) == 1 and completed[0].result is None
    assert "0x05" in ignored_reason(events)
    assert message_id in controller._id_ledger.closed_ids[1]
    assert not second.done()


def test_an_unknown_completion_check_answer_is_unconfirmed():
    clock, _, controller, events = direct_link()
    first = controller.request(1, action=1, value=0)
    pending = controller._queues[1][0]
    controller.on_frame(reply(controller, pending.frame.sequence, ReplyResult.ACCEPTED).encode())
    outcome = first.result(timeout=0)

    completion = next(iter(controller._completions.values()))
    completion.next_check = clock.now
    controller.tick(clock.now)
    query = controller._queues[1][0]
    assert query.completion_check and query.asked is pending.frame

    controller.on_frame(reply(controller, query.frame.sequence, 0x05).encode())

    assert outcome.completion.result(timeout=0) is None
    (completed,) = [event for event in events if isinstance(event, RequestCompleted)]
    assert completed.result is None and completed.queried
    assert "0x05" in ignored_reason(events)
    assert pending.frame.message_id in controller._id_ledger.closed_ids[1]


def test_an_unknown_wake_answer_is_unconfirmed():
    _, _, controller, events = direct_link()
    wake = controller.wake(1)
    pending = controller._queues[1][0]
    controller.on_frame(reply(controller, pending.frame.sequence, 0x05).encode())

    outcome = wake.result(timeout=0)
    assert outcome.status == UNCONFIRMED and outcome.result == 0x05
    assert "0x05" in ignored_reason(events)


def test_an_unknown_result_is_settled_so_the_next_id_is_not_skipped_forever():
    _, _, controller, _ = direct_link(initial_sequence=0)
    for value in range(20):
        future = controller.request(1, action=1, value=value)
        pending = controller._queues[1][0]
        controller.on_frame(reply(controller, pending.frame.sequence, 0x05).encode())
        assert future.result(timeout=0).status == UNCONFIRMED

    assert len(controller._id_ledger.request_ids[1]) == DEDUP_WINDOW
    assert controller.epoch_moves == 0
    next_sequence = controller._id_ledger.next_sequence[1]
    assert controller.build_request(action=1, value=99, target_id=1).sequence == next_sequence


def test_a_newer_firmware_0x05_does_not_take_the_old_done_path():
    # The old status property returned DONE for every result other than
    # ACCEPTED and NOT_RUN; this pins the mixed old/new firmware case.
    outcome = RequestOutcome(
        node_id=1,
        sequence=7,
        kind=FrameKind.REQUEST,
        result=0x05,
        attempts=1,
        elapsed=0.1,
    )
    assert outcome.status == UNCONFIRMED
    assert outcome.status != DONE


@pytest.mark.parametrize(
    ("result", "status"),
    [
        (ReplyResult.OK, DONE),
        (ReplyResult.ERROR, DONE),
        (ReplyResult.NOT_RUN, NOT_RUN),
        (ReplyResult.ACCEPTED, ACCEPTED),
    ],
)
def test_known_result_statuses_are_unchanged(result, status):
    outcome = RequestOutcome(
        node_id=1,
        sequence=7,
        kind=FrameKind.REQUEST,
        result=result,
        attempts=1,
        elapsed=0.1,
    )
    assert outcome.status == status
