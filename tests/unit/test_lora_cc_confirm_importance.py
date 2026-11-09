"""Ordinary commands are confirmed for a short, hard time; important ones keep the long confirmation.

Rules checked here (protocol_spec.md 4.2, "普通指令与重要指令"):

1. ordinary (default): confirm ``confirm_limit`` from the first send, a hard point on the clock the quiet rounds
   cannot push back; heard in the last 10 min → one last round only; else UNCONFIRMED at once, queue released,
   never sent again, "may have run";
2. important: confirmed on (kept in the state file; a file without the field counts as important);
3. nothing is asked after the limit; a late OK / ERROR / INTERRUPTED is published (``LateConfirmed``, persisted,
   replayed after a restart, the done Future untouched); a late NOT_RUN / ACCEPTED is never taken;
4. the REQUEST id and the last query ids of an ended command stay protected for the keep period, across restarts;
5. a restarted controller asks at least one round before judging the limit; parked ordinary commands end at it too.
"""

from __future__ import annotations

import json
import time

import pytest

from cairnmesh import controller as C
from cairnmesh.controller import DONE, UNCONFIRMED, UNCONFIRMED_NOTE, Controller, NodeConfirming
from cairnmesh.events import LateConfirmed, ReplyIgnored, RequestConfirming, RequestFailed, RequestSent
from cairnmesh.frames import QUERY_ACTION, ReplyFrame, ReplyResult, RequestFrame, ResultFrame

from lora_helpers import build, run
from test_lora_cc_layer3 import controller_on, go, link

G = 0x42
LIMIT = 100.0


def queries(controller, start=0):
    return [e for e in controller.events_seen[start:] if isinstance(e, RequestSent) and e.action == QUERY_ACTION]


def expired(controller):
    return [e for e in controller.events_seen if isinstance(e, RequestFailed) and e.expired]


def late_reply(controller, sequence, result, epoch=None):
    controller.on_frame(
        ReplyFrame(sender_id=1, target_id=G, sequence=sequence, result=result,
                   epoch=controller.epoch if epoch is None else epoch).encode()
    )


def ordinary(drop=1.0, **options):
    """A controller whose replies all get lost, confirm limit ``LIMIT`` s, and one command sent."""

    clock, medium, devices, _ = link(uplink_drop=drop)
    controller = controller_on(clock, medium, **{"confirm_limit": LIMIT, **options})
    return clock, medium, devices, controller


# ------------------------------------------------------------------- rule 1


def test_an_ordinary_command_ends_unconfirmed_exactly_at_the_limit_and_the_queue_goes_on():
    clock, medium, devices, controller = ordinary()
    start = clock.now
    future = controller.request(1, action=1, value=0)
    go(clock, medium, devices, controller, LIMIT - 1.0)
    assert not future.done() and controller.confirming()[0].important is False
    with pytest.raises(NodeConfirming):
        controller.request(1, action=1, value=1)  # still confirming: the node is held

    go(clock, medium, devices, controller, 1.3)
    assert clock.now - start < LIMIT + 1.0
    outcome = future.result(timeout=0)
    assert outcome.status == UNCONFIRMED and outcome.result is None and outcome.queried
    assert "可能已执行" in outcome.reason and UNCONFIRMED_NOTE in outcome.reason
    (failed,) = expired(controller)
    assert failed.status == UNCONFIRMED and not failed.abandoned
    assert controller.confirming() == []
    assert controller.request(1, action=1, value=1)  # the queue is free: no NodeConfirming
    assert devices[1].handled_count == 1  # it did run: "may have run" is true


def test_nothing_is_sent_for_an_ended_command_and_it_is_never_resent():
    clock, medium, devices, controller = ordinary()
    future = controller.request(1, action=1, value=0)
    go(clock, medium, devices, controller, LIMIT + 2.0)
    assert future.done()
    mark = len(controller.events_seen)
    go(clock, medium, devices, controller, 3600.0)  # an hour of silence
    assert controller.events_seen[mark:] == [] or not [
        e for e in controller.events_seen[mark:] if isinstance(e, RequestSent)
    ]
    assert devices[1].handled_count == 1


def test_the_limit_is_a_hard_point_whatever_the_quiet_rounds_say():
    # Defaults: 90 s window, 30 min limit, 2 min rounds, then (quiet network, 10 min on) 30 min rounds:
    # the next round would be at about 40 min. The command must end at 30 min.
    clock, medium, devices, controller = build(uplink_drop=1.0)[0:4]
    controller.round_wakes = lambda node: True  # every round would wake the whole network
    assert controller.confirm.limit == C.DEFAULT_CONFIRM_LIMIT == 1800.0
    controller.events_seen = []
    controller.events.subscribe(controller.events_seen.append)
    start = clock.now
    future = controller.request(1, action=1, value=0)
    run(clock, medium, devices, controller, 1800.0 - 2.0, step=0.05)
    assert not future.done()
    due = controller.confirming()[0]
    assert due.limit_in is not None and due.limit_in <= 2.1  # scheduled up to the limit, not past it
    run(clock, medium, devices, controller, 3.0, step=0.05)
    assert future.result(timeout=0).status == UNCONFIRMED
    assert clock.now - start < 1800.0 + 2.0
    assert [e for e in controller.events_seen if isinstance(e, RequestConfirming) and e.limit_in is not None]


def test_a_node_heard_lately_gets_one_last_round_and_then_the_end():
    clock, medium, devices, controller = ordinary()
    future = controller.request(1, action=1, value=0)
    go(clock, medium, devices, controller, LIMIT - 4.0)
    controller.nudge(1)  # heard at 96 s
    go(clock, medium, devices, controller, 3.9)
    assert not future.done()
    mark = len(controller.events_seen)
    go(clock, medium, devices, controller, 0.5)  # now past the limit
    assert not future.done()  # the last round is on the air
    go(clock, medium, devices, controller, 4.0)
    assert future.result(timeout=0).status == UNCONFIRMED
    last_round = queries(controller, mark)
    assert len(last_round) == controller.max_retries + 1  # one round, with its retries, no more
    assert expired(controller)


def test_the_last_round_may_still_find_the_answer():
    clock, medium, devices, controller = ordinary()
    future = controller.request(1, action=1, value=0)
    go(clock, medium, devices, controller, LIMIT - 4.0)
    controller.nudge(1)
    go(clock, medium, devices, controller, 3.8)
    medium.uplink_drop = 0.0  # the way back is up again before the last round
    go(clock, medium, devices, controller, 4.0)
    outcome = future.result(timeout=0)
    assert outcome.status == DONE and outcome.queried and outcome.ok
    assert not expired(controller)


def test_an_area_wide_power_up_counts_as_heard():
    clock, medium, devices, controller = ordinary()
    future = controller.request(1, action=1, value=0)
    go(clock, medium, devices, controller, LIMIT - 4.0)
    controller.nudge_all()
    go(clock, medium, devices, controller, 4.3)
    assert not future.done()  # last round in flight
    go(clock, medium, devices, controller, 3.0)
    assert future.result(timeout=0).status == UNCONFIRMED


# ------------------------------------------------------------------- rule 2


def test_an_important_command_keeps_confirming_past_the_limit():
    clock, medium, devices, controller = ordinary()
    future = controller.request(1, action=1, value=0, important=True)
    go(clock, medium, devices, controller, 10 * LIMIT)
    assert not future.done() and controller.confirming()[0].important
    assert controller.confirming()[0].limit_in is None
    mark = len(controller.events_seen)
    go(clock, medium, devices, controller, 300.0)
    assert queries(controller, mark)  # still asking
    medium.uplink_drop = 0.0
    controller.nudge(1)
    go(clock, medium, devices, controller, 15.0)
    assert future.result(timeout=0).status == DONE


def test_an_important_command_still_ends_by_the_operator_or_the_keep_period():
    clock, medium, devices, controller = ordinary(record_keep=500.0)
    future = controller.request(1, action=1, value=0, important=True)
    go(clock, medium, devices, controller, 470.0)
    assert not future.done()
    go(clock, medium, devices, controller, 40.0)
    assert "记忆期限" in future.result(timeout=0).reason


def test_the_limit_is_configurable_and_can_be_switched_off():
    clock, medium, devices, _ = link(uplink_drop=1.0)
    with pytest.raises(ValueError):
        controller_on(clock, medium, confirm_limit=1.0)  # shorter than the 5 s window
    controller = controller_on(clock, medium, confirm_limit=None)
    future = controller.request(1, action=1, value=0)
    go(clock, medium, devices, controller, 3 * LIMIT)
    assert not future.done() and controller.confirming()[0].limit_in is None
    assert Controller(medium.gateway, confirm_limit=900.0).confirm.limit == 900.0


# ------------------------------------------------------- the state file, restarts


def snapshot(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def test_importance_is_kept_in_the_state_file_and_a_file_without_it_means_important(tmp_path):
    state = str(tmp_path / "controller.json")
    clock, medium, devices, _ = link(uplink_drop=1.0)
    first = controller_on(clock, medium, state, confirm_limit=LIMIT)
    first.request(1, action=1, value=0)
    first.request(2, action=1, value=0, important=True)
    go(clock, medium, devices, first, 20.0)
    assert {entry["node"]: entry["important"] for entry in snapshot(state)["confirming"]} == {1: False, 2: True}

    second = controller_on(clock, medium, state, confirm_limit=LIMIT)
    assert {c.node_id: c.important for c in second.confirming()} == {1: False, 2: True}
    assert second.state_warning is None

    data = snapshot(state)
    for entry in data["confirming"]:
        del entry["important"]  # written by a version before this one
    with open(state, "w", encoding="utf-8") as handle:
        json.dump(data, handle)
    third = controller_on(clock, medium, state, confirm_limit=LIMIT)
    assert [c.important for c in third.confirming()] == [True, True]
    assert "重要" in third.state_warning and "2" in third.state_warning


def test_a_restored_ordinary_command_past_its_limit_is_asked_one_round_before_it_ends(tmp_path, monkeypatch):
    state = str(tmp_path / "controller.json")
    clock, medium, devices, _ = link(uplink_drop=1.0)
    first = controller_on(clock, medium, state, confirm_limit=LIMIT)
    first.request(1, action=1, value=0)
    go(clock, medium, devices, first, 20.0)

    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() + 3600.0)  # the controller was down for an hour
    second = controller_on(clock, medium, state, confirm_limit=LIMIT)
    (restored,) = second.confirming()
    assert not restored.important
    go(clock, medium, devices, second, 30.0)
    sent = queries(second)
    assert len(sent) >= second.max_retries + 1  # at least one full round before the verdict
    (failed,) = expired(second)
    assert failed.status == UNCONFIRMED
    assert second.confirming() == []


def test_a_restored_command_is_asked_even_when_the_limit_falls_soon_and_the_node_is_unheard(tmp_path, monkeypatch):
    state = str(tmp_path / "controller.json")
    clock, medium, devices, _ = link(uplink_drop=1.0)
    monkeypatch.setattr(time, "time", lambda: clock.now + 1e9)  # wall time follows the fake clock
    first = controller_on(clock, medium, state, confirm_limit=LIMIT)
    first.request(1, action=1, value=0)
    go(clock, medium, devices, first, LIMIT - 0.5)

    second = controller_on(clock, medium, state, confirm_limit=LIMIT)  # restarted half a second before its limit
    go(clock, medium, devices, second, 0.2)
    assert queries(second)  # a round went out before any judgement
    go(clock, medium, devices, second, 5.0)
    assert expired(second)


def test_a_restored_command_gets_its_answer_not_a_verdict(tmp_path, monkeypatch):
    state = str(tmp_path / "controller.json")
    clock, medium, devices, _ = link(uplink_drop=1.0)
    first = controller_on(clock, medium, state, confirm_limit=LIMIT)
    first.request(1, action=1, value=0)
    go(clock, medium, devices, first, 20.0)
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() + 3600.0)
    medium.uplink_drop = 0.0
    second = controller_on(clock, medium, state, confirm_limit=LIMIT)
    go(clock, medium, devices, second, 10.0)
    assert not expired(second)
    assert [e for e in second.events_seen if getattr(e, "queried", False)]
    assert second.confirming() == []


# -------------------------------------------------- parked ordinary commands


def test_a_parked_ordinary_command_ends_at_its_limit_too():
    clock, medium, devices, controller = ordinary()
    parked = controller.request(1, action=1, value=0)
    go(clock, medium, devices, controller, 30.0)  # confirming, asked a few times
    passer = controller.request(1, action=1, value=1, force=True, important=True)  # goes past it; stays in flight
    go(clock, medium, devices, controller, 10.0)  # the passer is past its window: confirming too
    assert not parked.done()
    assert len(controller.confirming()) == 2  # the head and the parked one
    go(clock, medium, devices, controller, LIMIT - 40.0 + 1.0)  # the parked one's limit, counted from its first send
    outcome = parked.result(timeout=0)
    assert outcome.status == UNCONFIRMED and "可能已执行" in outcome.reason
    assert not passer.done()  # the important one goes on
    assert [c.important for c in controller.confirming()] == [True]


# ------------------------------------------------- late results (ruling A)


def ended(**options):
    """An ordinary command ended at its limit, device ran it; returns what a late answer needs."""

    clock, medium, devices, controller = ordinary(**options)
    future = controller.request(1, action=1, value=0)
    go(clock, medium, devices, controller, LIMIT + 3.0)
    outcome = future.result(timeout=0)
    late = controller._late[1][controller.epoch << 8 | outcome.sequence]
    return clock, medium, devices, controller, future, outcome, late


def test_a_late_positive_answer_is_published_and_the_done_future_is_left_alone():
    clock, medium, devices, controller, future, outcome, late = ended()
    assert late.queries  # its last query ids are kept
    late_reply(controller, late.queries[-1], ReplyResult.OK)
    (event,) = [e for e in controller.events_seen if isinstance(e, LateConfirmed)]
    assert (event.node_id, event.sequence, event.epoch, event.result, event.via) == (
        1, outcome.sequence, controller.epoch, ReplyResult.OK, "reply")
    assert event.elapsed >= LIMIT and not event.replayed
    assert future.result(timeout=0) is outcome and outcome.status == UNCONFIRMED  # untouched
    assert [(c.sequence, c.result) for c in controller.late_confirmations()] == [(outcome.sequence, ReplyResult.OK)]
    late_reply(controller, late.sequence, ReplyResult.OK)  # a second copy: not published again
    assert len([e for e in controller.events_seen if isinstance(e, LateConfirmed)]) == 1


@pytest.mark.parametrize("result", [ReplyResult.ERROR, ReplyResult.INTERRUPTED])
def test_a_late_error_or_interrupted_is_taken_too(result):
    *_, controller, future, outcome, late = ended()
    late_reply(controller, late.sequence, result)
    (event,) = [e for e in controller.events_seen if isinstance(e, LateConfirmed)]
    assert event.result == result and future.result(timeout=0).status == UNCONFIRMED


def test_a_late_result_frame_is_taken_and_acknowledged():
    *_, controller, future, outcome, late = ended()
    sent = []
    controller.transport.send = lambda data: sent.append(bytes(data))
    controller.on_frame(
        ResultFrame(sender_id=1, target_id=G, sequence=outcome.sequence, result=ReplyResult.OK, epoch=controller.epoch).encode()
    )
    (event,) = [e for e in controller.events_seen if isinstance(e, LateConfirmed)]
    assert event.via == "result" and event.result == ReplyResult.OK
    assert sent and sent[0][0] == C.FrameKind.REPLY  # every RESULT copy is acknowledged
    assert future.result(timeout=0).status == UNCONFIRMED


@pytest.mark.parametrize("result", [ReplyResult.NOT_RUN, ReplyResult.ACCEPTED, 0x7F])
def test_after_the_limit_not_run_accepted_and_unknown_codes_are_never_taken(result):
    *_, controller, future, outcome, late = ended()
    for sequence in (late.sequence, late.queries[-1]):
        late_reply(controller, sequence, result)
    assert not [e for e in controller.events_seen if isinstance(e, LateConfirmed)]
    assert [e for e in controller.events_seen if isinstance(e, ReplyIgnored)]
    assert controller.late_confirmations() == []
    assert future.result(timeout=0).status == UNCONFIRMED
    assert not [e for e in controller.events_seen if isinstance(e, RequestFailed) and e.status == C.NOT_RUN]


def test_a_pushed_out_record_cannot_turn_an_ended_command_into_not_run():
    # Review paths: X ran, its answers were lost, it ended UNCONFIRMED. The device then lost X's
    # record (four commands after it, or a third sender pushing the controller's whole group out). A stray query
    # copy still on the air reaches the device and is answered NOT_RUN: that answer must not be taken.
    clock, medium, devices, controller, future, outcome, late = ended()
    device = devices[1]
    for sender in (0x50, 0x51):  # a third sender: the device forgets the least recently seen group
        device.handle_frame(RequestFrame(sender_id=sender, target_id=1, sequence=7, action=1, value=0))
    stray = RequestFrame(sender_id=G, target_id=1, sequence=late.queries[-1], epoch=controller.epoch,
                         action=QUERY_ACTION, value=outcome.sequence)
    answer = device.handle_frame(stray)
    assert answer.result == ReplyResult.NOT_RUN  # the device really says so
    controller.on_frame(answer.encode())
    assert future.result(timeout=0).status == UNCONFIRMED
    assert not [e for e in controller.events_seen if isinstance(e, LateConfirmed)]
    assert devices[1].handled_count == 3  # X ran once


def test_late_results_are_persisted_replayed_after_a_restart_and_stop_once_acknowledged(tmp_path):
    state = str(tmp_path / "controller.json")
    clock, medium, devices, _ = link(uplink_drop=1.0)
    first = controller_on(clock, medium, state, confirm_limit=LIMIT)
    future = first.request(1, action=1, value=0)
    go(clock, medium, devices, first, LIMIT + 3.0)
    outcome = future.result(timeout=0)
    late_reply(first, outcome.sequence, ReplyResult.OK)
    (entry,) = snapshot(state)["late"]
    assert (entry["sequence"], entry["result"], entry["via"], entry["acked"]) == (outcome.sequence, 0, "reply", False)

    second = controller_on(clock, medium, state, confirm_limit=LIMIT)  # restart: nobody acknowledged it
    assert [c.sequence for c in second.late_confirmations()] == [outcome.sequence]
    go(clock, medium, devices, second, 0.1)
    (replayed,) = [e for e in second.events_seen if isinstance(e, LateConfirmed)]
    assert replayed.replayed and replayed.sequence == outcome.sequence and replayed.epoch == first.epoch

    assert second.acknowledge_late(1, outcome.sequence)
    assert second.late_confirmations() == [] and not second.acknowledge_late(1, (outcome.sequence + 1) & 0xFF)
    third = controller_on(clock, medium, state, confirm_limit=LIMIT)
    go(clock, medium, devices, third, 0.1)
    assert not [e for e in third.events_seen if isinstance(e, LateConfirmed)]  # acknowledged: silence
    assert snapshot(state)["late"][0]["acked"] is True


def test_an_operator_abandon_leaves_the_same_watch():
    clock, medium, devices, controller = ordinary()
    future = controller.request(1, action=1, value=0, important=True)
    go(clock, medium, devices, controller, 40.0)
    assert controller.abandon(1) == 1
    outcome = future.result(timeout=0)
    late = controller._late[1][controller.epoch << 8 | outcome.sequence]
    late_reply(controller, late.sequence, ReplyResult.OK)
    assert [e for e in controller.events_seen if isinstance(e, LateConfirmed)]
    assert outcome.status == UNCONFIRMED


# ------------------------------------------- ids of ended commands stay protected


def test_the_query_ids_of_an_ended_command_are_never_taken_by_a_new_one():
    # Reproduction: after the limit the counter wraps, a new command is dealt an old query id, and the
    # old query's late answer arrives: the new command must not be judged by it.
    clock, medium, devices, controller, future, outcome, late = ended()
    old_query = late.queries[-1]
    controller._id_ledger.next_sequence[1] = old_query  # the counter is back on it
    medium.uplink_drop = 1.0
    fresh = controller.request(1, action=1, value=7, important=True)
    go(clock, medium, devices, controller, 1.0)
    new_sequence = controller.confirming()[0].sequence if controller.confirming() else controller._queues[1][0].frame.sequence
    protected = {late.sequence, *late.queries}
    assert new_sequence not in protected
    late_reply(controller, old_query, ReplyResult.OK)  # the old query's answer, late
    assert not fresh.done()  # the new command is not DONE
    assert [e for e in controller.events_seen if isinstance(e, LateConfirmed)][0].sequence == outcome.sequence


def test_without_the_watch_the_same_wrap_would_hand_out_the_old_query_id():
    # The control of the test above: the ids are protected by the watch and by nothing else.
    clock, medium, devices, controller, future, outcome, late = ended()
    old_query = late.queries[-1]
    controller._late.clear()
    controller._id_ledger.request_ids.pop(1, None)
    controller._id_ledger.next_sequence[1] = old_query
    assert controller.build_request(1, 0, 1).sequence == old_query


def test_the_ids_stay_protected_across_a_restart_and_until_the_keep_period_is_over(tmp_path):
    state = str(tmp_path / "controller.json")
    clock, medium, devices, _ = link(uplink_drop=1.0)
    first = controller_on(clock, medium, state, confirm_limit=LIMIT, record_keep=1000.0)
    future = first.request(1, action=1, value=0)
    go(clock, medium, devices, first, LIMIT + 3.0)
    outcome = future.result(timeout=0)
    late = first._late[1][first.epoch << 8 | outcome.sequence]
    ids = {first.epoch << 8 | late.sequence, *(first.epoch << 8 | q for q in late.queries)}
    assert ids <= first._protected_ids(1, first.epoch)

    second = controller_on(clock, medium, state, confirm_limit=LIMIT, record_keep=1000.0)
    assert ids <= second._protected_ids(1, first.epoch)  # across the restart (the old EPOCH)
    assert ids.isdisjoint(second._protected_ids(1, second.epoch))
    go(clock, medium, devices, second, 0.1)
    clock.now += 0.95 * 1000.0
    second.tick()
    assert not ids & second._protected_ids(1, first.epoch) - {first.epoch << 8 | outcome.sequence}  # forgotten past the keep period
    assert second._late == {}


def test_the_late_watch_is_released_exactly_at_95_percent_of_the_record_keep():
    record_keep = 1000.0
    clock, medium, devices, controller = ordinary(record_keep=record_keep)
    first_sent = clock.now
    future = controller.request(1, action=1, value=0)
    go(clock, medium, devices, controller, LIMIT + 3.0)
    outcome = future.result(timeout=0)
    assert outcome.status == UNCONFIRMED

    late_reply(controller, outcome.sequence, ReplyResult.OK)
    assert [(c.sequence, c.result) for c in controller.late_confirmations()] == [(outcome.sequence, ReplyResult.OK)]
    query = [
        event for event in controller.events_seen
        if isinstance(event, RequestSent) and event.action == QUERY_ACTION and event.value == outcome.sequence
    ][-1]
    query_id = controller.epoch << 8 | query.sequence
    assert controller.awaits_request_answer(1, query_id)

    horizon = first_sent + record_keep * 0.95
    clock.now = horizon - 0.01
    controller.tick()
    assert controller.late_confirmations()
    assert controller.awaits_request_answer(1, query_id)

    clock.now = horizon
    controller.tick()
    assert controller.late_confirmations() == []
    assert not controller.awaits_request_answer(1, query_id)


def test_an_ended_command_is_not_settled_so_its_request_id_stays_skipped():
    clock, medium, devices, controller, future, outcome, late = ended()
    message_id = controller.epoch << 8 | outcome.sequence
    assert message_id in controller._id_ledger.request_ids[1]
    assert message_id not in controller._id_ledger.closed_ids.get(1, set())  # `_fail`, never `_settled`


def test_the_counter_wrap_after_many_ended_commands_never_reuses_their_ids():
    clock, medium, devices, controller = ordinary(confirm_limit=20.0, confirm_window=5.0)
    guarded: dict[int, set[int]] = {}  # EPOCH -> ids of ended commands (REQUEST and last query ids)
    for value in range(70):  # enough for the 8-bit counter to go round more than once, with the keepalives' draws
        future = controller.request(1, action=1, value=value & 0xFF)
        sequence, epoch = controller._queues[1][0].frame.sequence, controller.epoch
        assert sequence not in guarded.get(epoch, set())
        go(clock, medium, devices, controller, 25.0)
        outcome = future.result(timeout=0)
        assert outcome.status == UNCONFIRMED
        late = controller._late[1][epoch << 8 | outcome.sequence]
        guarded.setdefault(epoch, set()).update({late.sequence, *late.queries})
        # keepalives and probes turn the counter too: every draw skips the guarded ids of its EPOCH
        for _ in range(40):
            drawn, draw_epoch = controller.allocate_id(1)
            assert drawn not in guarded.get(draw_epoch, set())


# ---------------------------------------------------------- plumbing


def test_importance_is_passed_down_the_whole_call_chain():
    from cairnmesh import session_bench as sb

    clock, network, controller = sb.build(
        {1: (0.0, 0.0), 2: (50.0, 0.0)}, seed=1, drop=0.0, gateway_options={"idle_timeout": None},
        confirm_window=C.DEFAULT_CONFIRM_WINDOW, controller_position=(0.0, 30.0),
    )
    network.sessions.request(2, 3, 1, important=True)
    assert controller._queues[2][0].important is True
    sb.run(clock, network, controller, 30.0)
    network.sessions.request(2, 3, 2)
    assert controller._queues[2][0].important is False


# ------------------------------------------------------------ additional review


def test_a_late_answer_of_another_epoch_with_the_same_numbers_is_not_a_confirmation():
    *_, controller, future, outcome, late = ended()
    other = (controller.epoch + 1) & 0xFF
    for sequence in (late.sequence, late.queries[-1]):
        late_reply(controller, sequence, ReplyResult.OK, epoch=other)
    controller.on_frame(ResultFrame(sender_id=1, target_id=G, sequence=outcome.sequence, result=ReplyResult.OK, epoch=other).encode())
    assert not [e for e in controller.events_seen if isinstance(e, LateConfirmed)]
    assert controller.late_confirmations() == []
    assert future.result(timeout=0).status == UNCONFIRMED


def test_abandoning_a_parked_command_leaves_the_watch_too():
    clock, medium, devices, controller = ordinary()
    parked = controller.request(1, action=1, value=0)
    go(clock, medium, devices, controller, 30.0)
    passer = controller.request(1, action=1, value=1, force=True, important=True)
    go(clock, medium, devices, controller, 10.0)
    assert not parked.done() and len(controller.confirming()) == 2
    assert controller.abandon(1, controller.confirming()[1].sequence) == 1  # the parked one (listed after the head)
    outcome = parked.result(timeout=0)
    assert outcome.status == UNCONFIRMED and not passer.done()
    late = controller._late[1][controller.epoch << 8 | outcome.sequence]
    assert late.queries  # ids guarded, as for the head
    late_reply(controller, late.queries[-1], ReplyResult.OK)
    assert [e.sequence for e in controller.events_seen if isinstance(e, LateConfirmed)] == [outcome.sequence]


def test_a_restored_parked_command_is_not_ended_before_it_has_been_asked(tmp_path, monkeypatch):
    state = str(tmp_path / "controller.json")
    clock, medium, devices, _ = link(uplink_drop=1.0)
    first = controller_on(clock, medium, state, confirm_limit=LIMIT)
    first.request(1, action=1, value=0)
    go(clock, medium, devices, first, 30.0)
    first.request(1, action=1, value=1, force=True)  # the first goes aside: two entries in the file
    go(clock, medium, devices, first, 1.0)
    assert len(snapshot(state)["confirming"]) == 2

    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() + 3600.0)  # both are far past their limit
    second = controller_on(clock, medium, state, confirm_limit=LIMIT)
    assert len(second.confirming()) == 2 and len(second._parked[1]) == 1
    go(clock, medium, devices, second, 0.1)
    assert expired(second) == []  # the parked one, never asked in this run, waits for its turn at the head
    assert len(second._parked[1]) == 1
    go(clock, medium, devices, second, 30.0)
    assert len(expired(second)) == 2  # each was asked one round first, then ended
    assert len(queries(second)) >= 2 * (second.max_retries + 1)
    assert second.confirming() == []


def test_a_damaged_state_file_costs_only_the_damaged_entries(tmp_path):
    state = str(tmp_path / "controller.json")
    clock, medium, devices, _ = link(uplink_drop=1.0)
    first = controller_on(clock, medium, state, confirm_limit=LIMIT)
    future = first.request(1, action=1, value=0)
    go(clock, medium, devices, first, LIMIT + 3.0)
    outcome = future.result(timeout=0)
    first.request(2, action=1, value=0)
    go(clock, medium, devices, first, 20.0)
    data = snapshot(state)
    good = data["late"][0]
    data["late"] = [
        good,
        {"node": 1},  # keys missing
        {**good, "epoch": "x"},  # wrong type
        {**good, "sequence": 999},  # out of range
        {**good, "queries": [None]},
        {**good, "important": ["no"], "sequence": (good["sequence"] + 1) & 0xFF, "queries": []},  # a field of the wrong type
        "garbage",
    ]
    for entry in data["confirming"]:
        entry["important"] = "no"  # not a boolean
    with open(state, "w", encoding="utf-8") as handle:
        json.dump(data, handle)

    second = controller_on(clock, medium, state, confirm_limit=LIMIT)  # must not raise
    assert "损坏" in second.state_warning and "5" in second.state_warning
    kept = second._late[1]
    assert {late.sequence for late in kept.values()} == {outcome.sequence, (good["sequence"] + 1) & 0xFF}
    assert all(late.important for late in kept.values() if late.sequence != outcome.sequence)  # the safe side
    assert [c.important for c in second.confirming()] == [True]  # not a boolean: important
    assert "重要" in second.state_warning

    data["late"] = "not a list"
    with open(state, "w", encoding="utf-8") as handle:
        json.dump(data, handle)
    assert "损坏" in controller_on(clock, medium, state, confirm_limit=LIMIT).state_warning
