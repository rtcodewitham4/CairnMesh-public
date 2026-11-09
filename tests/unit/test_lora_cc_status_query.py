"""Status query and three-state outcome (Q18): a request whose replies were all lost is asked about, not guessed."""

from __future__ import annotations

import pytest

from cairnmesh.controller import DONE, NOT_RUN, UNCONFIRMED
from cairnmesh.events import RequestConfirming, RequestFailed, RequestQuerying
from cairnmesh.frames import QUERY_ACTION, ReplyResult, RequestFrame

from lora_helpers import build, run

G = 0x42


def query(sequence: int, own: int = 200) -> RequestFrame:
    return RequestFrame(sender_id=G, target_id=1, sequence=own, action=QUERY_ACTION, value=sequence)


# ---------------------------------------------------------------- device side


def test_a_query_answers_with_the_stored_result_and_runs_nothing():
    _, _, devices, _, _ = build()
    device = devices[1]
    device.handle_frame(RequestFrame(sender_id=G, target_id=1, sequence=7, action=1, value=0))
    before = {sender: list(records) for sender, records in device._recent.items()}

    assert device.handle_frame(query(7)).result == ReplyResult.OK
    assert device.handle_frame(query(7, own=201)).result == ReplyResult.OK
    assert device.handled_count == 1
    assert {sender: list(records) for sender, records in device._recent.items()} == before  # queries are not cached: they push nothing out


def test_not_run_is_final_a_late_copy_is_refused():
    _, _, devices, _, _ = build()
    device = devices[1]
    assert device.handle_frame(query(9)).result == ReplyResult.NOT_RUN

    late = RequestFrame(sender_id=G, target_id=1, sequence=9, action=1, value=0)
    assert device.handle_frame(late).result == ReplyResult.NOT_RUN
    assert device.handled_count == 0


def test_a_query_for_another_senders_request_does_not_see_it():
    _, _, devices, _, _ = build()
    device = devices[1]
    device.handle_frame(RequestFrame(sender_id=0x43, target_id=1, sequence=7, action=1, value=0))
    assert device.handle_frame(query(7)).result == ReplyResult.NOT_RUN


# ------------------------------------------------------------ controller side


def test_ran_but_every_reply_lost_is_confirmed_by_the_query():
    clock, medium, devices, controller, events = build(uplink_drop=1.0)
    future = controller.request(1, action=1, value=0)
    run(clock, medium, devices, controller, 2.5)  # 4 tries × 0.5 s: retries used up
    assert any(isinstance(e, RequestQuerying) for e in events) and not future.done()

    medium.uplink_drop = 0.0  # the way back recovers
    run(clock, medium, devices, controller, 2.0)
    outcome = future.result(timeout=0)
    assert outcome.status == DONE and outcome.ok and outcome.queried
    assert devices[1].handled_count == 1


def test_never_arrived_is_confirmed_not_run():
    clock, medium, devices, controller, events = build(downlink_drop=1.0)
    future = controller.request(1, action=1, value=0)
    run(clock, medium, devices, controller, 2.5)

    medium.downlink_drop = 0.0
    run(clock, medium, devices, controller, 2.0)
    outcome = future.result(timeout=0)
    assert outcome.status == NOT_RUN and not outcome.ok and outcome.result == ReplyResult.NOT_RUN
    assert any(isinstance(e, RequestFailed) and e.status == NOT_RUN for e in events)
    assert devices[1].handled_count == 0


def test_no_answer_at_all_ordinary_command_ends_unconfirmed_at_the_confirm_limit():
    # Ordinary (default): confirmed 90 s of queries, then CONFIRMING up to ``confirm_limit`` (30 min), then
    # UNCONFIRMED by the controller itself: "may have run", queue free, never resent.
    clock, medium, devices, controller, events = build(uplink_drop=1.0)
    future = controller.request(1, action=1, value=0)
    run(clock, medium, devices, controller, controller.confirm.window - 1.0, step=0.05)
    assert not future.done()  # still asking

    run(clock, medium, devices, controller, 2.0, step=0.05)
    assert not future.done()  # past the window: CONFIRMING, not given up
    assert [e for e in events if isinstance(e, RequestConfirming)]

    run(clock, medium, devices, controller, controller.confirm.limit - controller.confirm.window - 10.0, step=0.05)
    assert not future.done()  # 10 s before the limit
    run(clock, medium, devices, controller, 12.0, step=0.05)
    outcome = future.result(timeout=0)
    assert outcome.status == UNCONFIRMED and outcome.result is None and outcome.queried
    assert "可能已执行" in outcome.reason
    failed = [e for e in events if isinstance(e, RequestFailed)]
    assert len(failed) == 1 and failed[0].status == UNCONFIRMED and failed[0].expired and not failed[0].abandoned
    assert controller.confirming() == [] and controller.request(1, action=1, value=1)  # the queue is free
    assert devices[1].handled_count == 1  # it did run: that is why it is "unconfirmed", not "failed"


def test_no_answer_at_all_important_command_keeps_confirming_until_the_operator_abandons():
    clock, medium, devices, controller, events = build(uplink_drop=1.0)
    future = controller.request(1, action=1, value=0, important=True)
    run(clock, medium, devices, controller, controller.confirm.window - 1.0, step=0.05)
    assert not future.done()  # still asking

    run(clock, medium, devices, controller, 2.0, step=0.05)
    assert not future.done()  # layer 3: not given up
    assert [e for e in events if isinstance(e, RequestConfirming)]
    run(clock, medium, devices, controller, 2 * controller.confirm.limit, step=0.05)
    assert not future.done()  # nor at twice the ordinary limit
    assert controller.abandon(1) == 1
    outcome = future.result(timeout=0)
    assert outcome.status == UNCONFIRMED and outcome.result is None and outcome.queried
    failed = [e for e in events if isinstance(e, RequestFailed)]
    assert len(failed) == 1 and failed[0].status == UNCONFIRMED and failed[0].abandoned and not failed[0].expired
    assert devices[1].handled_count == 1  # it did run: that is why it is "unconfirmed", not "failed"


def test_the_next_request_waits_for_the_query():
    clock, medium, devices, controller, _ = build(uplink_drop=1.0)
    first = controller.request(1, action=1, value=0)
    second = controller.request(1, action=1, value=1)
    run(clock, medium, devices, controller, 2.5)
    assert devices[1].handled_count == 1  # the second one has not gone out

    medium.uplink_drop = 0.0
    run(clock, medium, devices, controller, 3.0)
    assert first.result(timeout=0).status == DONE and second.result(timeout=0).ok
    assert devices[1].handled_count == 2


def test_the_query_action_is_reserved():
    _, _, _, controller, _ = build()
    with pytest.raises(ValueError, match="reserved"):
        controller.request(1, action=QUERY_ACTION, value=0)
