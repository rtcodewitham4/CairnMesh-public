"""INTERRUPTED = 0x04 (2026-11-08, wire format v5).

A device that resets with an action open has no final result: the action may
have completed or not, the safe state is applied, and it never runs again.
It reports INTERRUPTED, not ERROR (an old ERROR keeps meaning "the action
failed"), and the controller never turns that into DONE or "not done".
"""

from __future__ import annotations

from cairnmesh.controller import (
    ACCEPTED,
    DONE,
    INTERRUPTED,
    INTERRUPTED_NOTE,
    UNCONFIRMED,
    Controller,
    RequestOutcome,
)
from cairnmesh.device import Device
from cairnmesh.events import ReplyIgnored, RequestCompleted
from cairnmesh.frames import (
    QUERY_ACTION,
    EventFrame,
    FrameKind,
    ReplyFrame,
    ReplyResult,
    ReportFrame,
    RequestFrame,
    ResultFrame,
    decode_frame,
)

from lora_helpers import FakeClock, build, run

G = 0x42


class Pipe:
    def __init__(self) -> None:
        self.sent: list[bytes] = []

    def send(self, data: bytes) -> None:
        self.sent.append(bytes(data))

    def set_receiver(self, callback) -> None:
        pass

    def frames(self, kind):
        return [decode_frame(data) for data in self.sent if data[0] == kind]


def device(action_time=2.0, **options):
    clock = FakeClock()
    pipe = Pipe()
    return clock, pipe, Device(pipe, 1, awake=True, clock=clock, action_time=action_time, **options)


def request(sequence: int) -> RequestFrame:
    return RequestFrame(sender_id=G, target_id=1, sequence=sequence, epoch=4, action=1, value=0)


def query(sequence: int, own: int = 200) -> RequestFrame:
    return RequestFrame(sender_id=G, target_id=1, sequence=own, epoch=4, action=QUERY_ACTION, value=sequence)


# ------------------------------------------------------------ the code itself


def test_the_code_is_0x04_and_the_old_codes_are_unchanged():
    assert [int(r) for r in (ReplyResult.OK, ReplyResult.ERROR, ReplyResult.NOT_RUN, ReplyResult.ACCEPTED, ReplyResult.INTERRUPTED)] == [0, 1, 2, 3, 4]
    for kind, frame in (
        (FrameKind.REPLY, ReplyFrame(sender_id=1, target_id=G, sequence=7, epoch=4, result=ReplyResult.INTERRUPTED)),
        (FrameKind.RESULT, ResultFrame(sender_id=1, target_id=G, sequence=7, epoch=4, result=ReplyResult.INTERRUPTED)),
    ):
        raw = frame.encode()
        assert len(raw) == 6 and raw[0] == kind and raw[5] == 0x04
        decoded = decode_frame(raw)
        assert decoded == frame and decoded.result == ReplyResult.INTERRUPTED


# ----------------------------------------------------------------- device side


def test_a_reboot_during_a_slow_action_reports_interrupted_not_error():
    clock, pipe, dev = device()
    assert dev.handle_frame(request(7)).result == ReplyResult.ACCEPTED
    clock.now += 1.0
    dev.reset()
    clock.now += dev.result_timeout + 1.0
    dev.tick()
    (result,) = pipe.frames(FrameKind.RESULT)
    assert result.result == ReplyResult.INTERRUPTED
    assert dev.handle_frame(request(7)).result == ReplyResult.INTERRUPTED
    assert dev.handled_count == 0  # never run again


def test_a_reboot_during_a_short_action_answers_the_retry_interrupted():
    clock, pipe, dev = device(action_time=0.3)
    assert dev.handle_frame(request(7)) is None  # the answer waits for the action
    clock.now += 0.1
    dev.reset()
    assert dev.handle_frame(request(7)).result == ReplyResult.INTERRUPTED
    assert dev.handle_frame(query(7)).result == ReplyResult.INTERRUPTED
    assert dev.handled_count == 0


def test_a_reboot_during_a_pending_action_is_interrupted_and_a_late_completion_is_ignored():
    clock, pipe, dev = device(action_time=5.0)
    dev.perform_action = lambda frame: -1  # PENDING: the application completes it later
    dev.handle_frame(request(7))
    dev.reset()
    assert dev.complete(G, 4 << 8 | 7, ReplyResult.OK) is False
    assert dev.handle_frame(query(7)).result == ReplyResult.INTERRUPTED


def test_a_result_whose_flash_write_failed_reads_back_accepted_and_is_interrupted_never_done():
    # Firmware: a result that could not be written still goes out, but what the journal holds after a reset is
    # the earlier ACCEPTED (spec 4.1). Modelled by putting the record back as the journal would have it.
    clock, pipe, dev = device()
    dev.handle_frame(request(7))
    clock.now += 2.0
    dev.tick()
    assert dev.handle_frame(query(7)).result == ReplyResult.OK  # the controller was told OK before the cut
    key_records = dev._recent[G]
    index = next(i for i, record in enumerate(key_records) if record[0] == 4 << 8 | 7)
    message_id, _, logged, _ = key_records[index]
    key_records[index] = (message_id, ReplyResult.ACCEPTED, logged, True)  # the stale journal state
    dev.reset()
    assert dev.handle_frame(request(7)).result == ReplyResult.INTERRUPTED
    assert dev.handled_count == 0


def test_an_error_stays_error_across_a_reboot_and_is_not_rewritten():
    clock, pipe, dev = device(action_time=0.0)
    dev.perform_action = lambda frame: ReplyResult.ERROR
    assert dev.handle_frame(request(7)).result == ReplyResult.ERROR
    dev.reset()
    assert dev.handle_frame(request(7)).result == ReplyResult.ERROR  # a failed action is still a failure
    assert dev.handle_frame(query(7)).result == ReplyResult.ERROR


def test_an_ok_stays_ok_across_a_reboot():
    clock, pipe, dev = device(action_time=0.0)
    assert dev.handle_frame(request(7)).result == ReplyResult.OK
    dev.reset()
    assert dev.handle_frame(request(7)).result == ReplyResult.OK


# -------------------------------------------------------------- controller side


class Port:
    def __init__(self) -> None:
        self.sent: list[bytes] = []

    def send(self, data: bytes) -> None:
        self.sent.append(bytes(data))

    def set_receiver(self, callback) -> None:
        pass


def direct_link():
    clock = FakeClock()
    port = Port()
    controller = Controller(port, timeout=0.5, max_retries=0, epoch=9, initial_sequence=7, clock=clock)
    events = []
    controller.events.subscribe(events.append)
    return clock, port, controller, events


def reply(controller, sequence, result):
    return ReplyFrame(sender_id=1, target_id=controller.sender_id, sequence=sequence, epoch=controller.epoch, result=result)


def test_an_interrupted_reply_is_a_final_state_that_is_not_done():
    _, _, controller, events = direct_link()
    future = controller.request(1, action=1, value=0)
    pending = controller._queues[1][0]
    controller.on_frame(reply(controller, pending.frame.sequence, ReplyResult.INTERRUPTED).encode())
    outcome = future.result(timeout=0)
    assert outcome.status == INTERRUPTED and outcome.interrupted
    assert outcome.status not in (DONE, UNCONFIRMED) and not outcome.ok
    assert outcome.reason == INTERRUPTED_NOTE
    assert "动作可能已完成" in INTERRUPTED_NOTE and "未完成" not in INTERRUPTED_NOTE.replace("动作可能已完成", "")
    assert not any(isinstance(event, ReplyIgnored) for event in events)  # a known code, not an unknown one
    assert controller.pending_count(1) == 0  # the queue moves on
    assert pending.frame.message_id in controller._id_ledger.closed_ids[1]


def test_an_error_reply_is_still_done_with_ok_false():
    _, _, controller, _ = direct_link()
    future = controller.request(1, action=1, value=0)
    pending = controller._queues[1][0]
    controller.on_frame(reply(controller, pending.frame.sequence, ReplyResult.ERROR).encode())
    outcome = future.result(timeout=0)
    assert outcome.status == DONE and not outcome.ok and not outcome.interrupted and outcome.reason == ""


def test_the_status_of_a_stored_outcome():
    outcome = RequestOutcome(node_id=1, sequence=7, kind=FrameKind.REQUEST, result=ReplyResult.INTERRUPTED, attempts=1, elapsed=0.1)
    assert outcome.status == INTERRUPTED and not outcome.ok


def test_a_result_after_accepted_can_be_interrupted():
    # The device answered ACCEPTED, reset, and reports INTERRUPTED in the RESULT after the boot.
    clock, medium, devices, controller, events = build()
    devices[1].action_time = 2.0
    first = controller.request(1, action=1, value=0)
    run(clock, medium, devices, controller, 0.2)
    outcome = first.result(timeout=0)
    assert outcome.status == ACCEPTED
    devices[1].reset()
    run(clock, medium, devices, controller, 15.0)
    assert outcome.completion.result(timeout=0) == ReplyResult.INTERRUPTED
    (completed,) = [event for event in events if isinstance(event, RequestCompleted)]
    assert completed.result == ReplyResult.INTERRUPTED
    assert not any(isinstance(event, ReplyIgnored) for event in events)
    assert devices[1].handled_count == 0  # nothing ran after the boot
    devices[1].action_time = 0.0
    second = controller.request(1, action=2, value=0)
    run(clock, medium, devices, controller, 0.2)
    assert second.result(timeout=0).status == DONE


def test_a_retry_after_a_reboot_with_a_stale_journal_ends_interrupted():
    clock, medium, devices, controller, events = build(uplink_drop=1.0)
    devices[1].action_time = 0.0
    first = controller.request(1, action=1, value=0)
    run(clock, medium, devices, controller, 0.3)  # the device runs it; every answer is lost
    assert devices[1].handled_count == 1
    # The journal holds only what was written before the cut: model the stale ACCEPTED.
    records = devices[1]._recent[controller.sender_id]
    records[0] = (records[0][0], ReplyResult.ACCEPTED, records[0][2], True)
    devices[1].reset()
    medium.uplink_drop = 0.0
    run(clock, medium, devices, controller, 3.0)
    outcome = first.result(timeout=0)
    assert outcome.status == INTERRUPTED and outcome.status != DONE
    assert devices[1].handled_count == 0


# ----------------------------------- a device EPOCH says nothing about a restart


def test_a_device_epoch_change_or_collision_does_not_change_a_conclusion():
    # Ruling B: no "device EPOCH changed = it restarted" inference. The conclusion about a command comes from
    # its result code alone: an OK RESULT stays OK whatever EPOCH the node's own frames carry, and an
    # INTERRUPTED one stays INTERRUPTED even when the node's EPOCH is the very same before and after.
    for node_epoch_after in (4, 5, 9):  # same as the command's, different, same as the controller's
        _, port, controller, events = direct_link()
        controller.request(1, action=1, value=0)
        pending = controller._queues[1][0]
        controller.on_frame(reply(controller, pending.frame.sequence, ReplyResult.ACCEPTED).encode())
        completion = next(iter(controller._completions.values())).future
        controller.on_frame(ReportFrame(sender_id=1, target_id=controller.sender_id, sequence=1, epoch=4, subject=2, event=0).encode())
        controller.on_frame(ReportFrame(sender_id=1, target_id=controller.sender_id, sequence=2, epoch=node_epoch_after, subject=2, event=0).encode())
        controller.on_frame(EventFrame(sender_id=1, target_id=controller.sender_id, sequence=3, epoch=node_epoch_after, event_id=1).encode())
        assert not completion.done()  # nothing inferred from the node's EPOCH
        controller.on_frame(
            ResultFrame(sender_id=1, target_id=controller.sender_id, sequence=pending.frame.sequence, epoch=controller.epoch, result=ReplyResult.OK).encode()
        )
        assert completion.result(timeout=0) == ReplyResult.OK

        _, _, controller, events = direct_link()
        controller.request(1, action=1, value=0)
        pending = controller._queues[1][0]
        controller.on_frame(reply(controller, pending.frame.sequence, ReplyResult.ACCEPTED).encode())
        completion = next(iter(controller._completions.values())).future
        controller.on_frame(
            ReportFrame(sender_id=1, target_id=controller.sender_id, sequence=2, epoch=node_epoch_after, subject=2, event=0).encode()
        )
        controller.on_frame(
            ResultFrame(
                sender_id=1, target_id=controller.sender_id, sequence=pending.frame.sequence, epoch=controller.epoch, result=ReplyResult.INTERRUPTED
            ).encode()
        )
        assert completion.result(timeout=0) == ReplyResult.INTERRUPTED


# ----------------- INTERRUPTED through the paths that are not the plain reply


def test_interrupted_for_a_confirming_parked_request_ends_it_interrupted():
    # The request went confirming, a forced one parked it; the parked request's query is answered INTERRUPTED.
    clock, _, controller, events = direct_link()
    controller.confirm.window = 0.05
    first = controller.request(1, action=1, value=0)
    clock.now += 0.5
    controller.tick(clock.now)
    assert controller._queues[1][0].confirming

    forced = controller.request(1, action=2, value=0, force=True)
    parked = controller._parked[1][0]
    message_id = parked.original.message_id
    controller.on_frame(reply(controller, parked.query_sequences[-1], ReplyResult.INTERRUPTED).encode())

    outcome = first.result(timeout=0)
    assert outcome.status == INTERRUPTED and outcome.queried and outcome.status != DONE and not outcome.ok
    assert outcome.reason == INTERRUPTED_NOTE
    assert not any(isinstance(event, ReplyIgnored) for event in events)
    assert not controller._parked.get(1) and not forced.done()  # the forced request keeps the queue
    assert message_id in controller._id_ledger.closed_ids[1]  # the device's record is closed: its id is not skipped for ever


def test_interrupted_in_a_completion_check_query_completes_with_interrupted():
    # ACCEPTED, then no RESULT: the completion check (a status query, queried=True) is answered INTERRUPTED.
    clock, _, controller, events = direct_link()
    first = controller.request(1, action=1, value=0)
    pending = controller._queues[1][0]
    message_id = pending.frame.message_id
    controller.on_frame(reply(controller, pending.frame.sequence, ReplyResult.ACCEPTED).encode())
    outcome = first.result(timeout=0)
    assert outcome.status == ACCEPTED

    completion = next(iter(controller._completions.values()))
    completion.next_check = clock.now
    controller.tick(clock.now)
    query = controller._queues[1][0]
    assert query.completion_check and query.asked is pending.frame
    controller.on_frame(reply(controller, query.frame.sequence, ReplyResult.INTERRUPTED).encode())

    assert outcome.completion.result(timeout=0) == ReplyResult.INTERRUPTED
    (completed,) = [event for event in events if isinstance(event, RequestCompleted)]
    assert completed.result == ReplyResult.INTERRUPTED and completed.queried
    assert not any(isinstance(event, ReplyIgnored) for event in events)
    assert controller.pending_count(1) == 0  # the queue is free again
    assert message_id in controller._id_ledger.closed_ids[1]
    nxt = controller.request(1, action=2, value=0)
    assert controller._queues[1][0].frame.kind == FrameKind.REQUEST and not nxt.done()


def test_a_late_interrupted_answer_to_an_accepted_request_completes_it():
    # A6: the request is no longer the queue head when its final answer (INTERRUPTED) arrives.
    _, _, controller, events = direct_link()
    first = controller.request(1, action=1, value=0)
    pending = controller._queues[1][0]
    message_id = pending.frame.message_id
    controller.on_frame(reply(controller, pending.frame.sequence, ReplyResult.ACCEPTED).encode())
    outcome = first.result(timeout=0)
    second = controller.request(1, action=2, value=0)
    controller.on_frame(reply(controller, pending.frame.sequence, ReplyResult.INTERRUPTED).encode())

    assert outcome.completion.result(timeout=0) == ReplyResult.INTERRUPTED
    # (This A6 branch settles ids only for unknown codes, as it does for OK and ERROR: the id is just kept longer.)
    assert not second.done() and message_id
    assert not any(isinstance(event, ReplyIgnored) for event in events)


def test_the_note_never_claims_a_safe_state_was_applied():
    # Only the device knows whether its action registered a safe state.
    assert "安全态已应用" not in INTERRUPTED_NOTE and "动作可能已完成" in INTERRUPTED_NOTE
