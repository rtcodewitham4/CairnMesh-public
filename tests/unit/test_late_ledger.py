"""LateLedger on its own: no Controller, no clock, only the records and what they promise.

The whole path through a ``Controller`` (limit, abandon, late REPLY / RESULT, restart) is covered by
``test_lora_cc_confirm_importance`` and the state file by ``test_controller_state_golden``; here only what the
ledger itself guarantees: which answers are taken, what stays protected and until when, what is published once.
"""

from __future__ import annotations

import pytest

from cairnmesh.events import LateConfirmed
from cairnmesh.frames import ReplyResult, RequestFrame
from cairnmesh.late_ledger import LateLedger

EPOCH = 7
FIRST_SENT = 100.0


def ledger(keep_limit: int = 256) -> LateLedger:
    return LateLedger(keep_limit=keep_limit)


def request(sequence: int = 9, node: int = 2, epoch: int = EPOCH, action: int = 3, value: int = 4) -> RequestFrame:
    return RequestFrame(sender_id=1, target_id=node, sequence=sequence, epoch=epoch, action=action, value=value)


def watched(book: LateLedger, sequence: int = 9, queries=(21, 22), node: int = 2, epoch: int = EPOCH, first_sent: float = FIRST_SENT):
    book.watch(request(sequence, node, epoch), first_sent, list(queries), important=False)
    return book.get(node, epoch << 8 | sequence)


# ------------------------------------------------------------------ watching


def test_a_watched_command_is_kept_under_its_message_id_with_all_it_needs_to_be_matched():
    book = ledger()
    queries = [21, 22]
    late = watched(book, queries=queries)
    assert (late.node, late.epoch, late.sequence, late.action, late.value) == (2, EPOCH, 9, 3, 4)
    assert (late.first_sent, late.queries, late.important) == (FIRST_SENT, [21, 22], False)
    assert (late.result, late.via, late.elapsed, late.acked) == (None, "", 0.0, False)
    queries.append(99)
    assert late.queries == [21, 22]  # a copy: the caller's list is not the ledger's
    assert book.get(2, EPOCH << 8 | 9) is late
    assert book.get(2, EPOCH << 8 | 10) is None and book.get(3, EPOCH << 8 | 9) is None


def test_watching_the_same_id_again_replaces_the_record_and_makes_it_the_newest():
    book = ledger(keep_limit=2)
    first = watched(book, sequence=1)
    watched(book, sequence=2)
    again = watched(book, sequence=1, queries=(5,))
    assert again is not first and again.queries == [5]
    watched(book, sequence=3)  # over the limit: the oldest goes, which is 2 now
    assert book.get(2, EPOCH << 8 | 2) is None
    assert book.get(2, EPOCH << 8 | 1) is again and book.get(2, EPOCH << 8 | 3) is not None


def test_the_keep_limit_is_per_node_and_drops_the_oldest():
    book = ledger(keep_limit=3)
    for sequence in range(1, 6):
        watched(book, sequence=sequence)
    watched(book, sequence=1, node=3)
    assert sorted(late.sequence for late in book.records() if late.node == 2) == [3, 4, 5]
    assert [late.node for late in book.records()].count(3) == 1


# ------------------------------------------------------ the protected ids


def test_ended_commands_keep_their_request_id_and_last_query_ids_protected_in_their_own_epoch_only():
    book = ledger()
    watched(book, sequence=9, queries=(21, 22))
    mark = EPOCH << 8
    assert book.protected_ids(2, EPOCH) == {mark | 9, mark | 21, mark | 22}
    assert book.protected_ids(2, EPOCH + 1) == set()  # another EPOCH: other ids
    assert book.protected_ids(3, EPOCH) == set()  # another node


def test_an_answer_and_its_acknowledgement_do_not_release_the_ids():
    book = ledger()
    late = watched(book)
    protected = book.protected_ids(2, EPOCH)
    assert book.take_answer(late, ReplyResult.OK, "reply", 150.0) is None
    assert book.protected_ids(2, EPOCH) == protected
    assert book.acknowledge(2, 9) is True
    assert book.protected_ids(2, EPOCH) == protected  # only ``expire`` lets go


def test_the_ids_are_released_only_when_the_record_is_forgotten():
    book = ledger()
    watched(book)
    assert book.expire(FIRST_SENT + 1000.0, 1000.0) is True
    assert book.protected_ids(2, EPOCH) == set()


# ------------------------------------------------------------- matching


def test_a_reply_matches_by_the_request_id_or_one_of_the_last_query_ids_in_the_same_epoch():
    book = ledger()
    late = watched(book, sequence=9, queries=(21, 22))
    assert book.match(2, EPOCH, 9) is late
    assert book.match(2, EPOCH, 21) is late and book.match(2, EPOCH, 22) is late
    assert book.match(2, EPOCH, 23) is None
    assert book.match(2, EPOCH + 1, 9) is None  # same numbers, another EPOCH: not this command
    assert book.match(3, EPOCH, 9) is None


# --------------------------------------------------------------- answers


@pytest.mark.parametrize("result", [ReplyResult.OK, ReplyResult.ERROR, ReplyResult.INTERRUPTED])
def test_ok_error_and_interrupted_are_taken_with_how_they_came(result):
    book = ledger()
    late = watched(book)
    assert book.take_answer(late, result, "result", 130.5) is None
    assert (late.result, late.via, late.elapsed, late.acked) == (int(result), "result", 30.5, False)


@pytest.mark.parametrize("result", [ReplyResult.NOT_RUN, ReplyResult.ACCEPTED, 0x77])
def test_not_run_accepted_and_unknown_codes_are_never_taken(result):
    book = ledger()
    late = watched(book)
    why = book.take_answer(late, result, "reply", 130.0)
    assert why is not None and "不采信" in why
    assert (late.result, late.via, late.elapsed, late.acked) == (None, "", 0.0, False)
    assert book.unacknowledged() == []  # still unconfirmed: nothing to publish


def test_a_second_answer_is_ignored_and_the_first_one_stays():
    book = ledger()
    late = watched(book)
    assert book.take_answer(late, ReplyResult.OK, "reply", 130.0) is None
    why = book.take_answer(late, ReplyResult.ERROR, "result", 140.0)
    assert why == "已记录过迟到结果"
    assert (late.result, late.via, late.elapsed) == (int(ReplyResult.OK), "reply", 30.0)


def test_the_elapsed_time_is_never_negative():
    book = ledger()
    late = watched(book, first_sent=500.0)
    book.take_answer(late, ReplyResult.OK, "reply", 499.0)  # the clock went back
    assert late.elapsed == 0.0


# ----------------------------------------------- publishing, acknowledging


def test_only_results_nobody_acknowledged_are_published():
    book = ledger()
    taken = watched(book, sequence=1)
    watched(book, sequence=2)  # no result yet
    done = watched(book, sequence=3)
    book.take_answer(taken, ReplyResult.INTERRUPTED, "reply", 110.0)
    book.take_answer(done, ReplyResult.OK, "result", 120.0)
    book.acknowledge(2, 3)
    published = book.unacknowledged()
    assert all(isinstance(event, LateConfirmed) for event in published)
    assert [(e.node_id, e.sequence, e.epoch, e.result, e.via, e.elapsed, e.replayed) for e in published] == [
        (2, 1, EPOCH, int(ReplyResult.INTERRUPTED), "reply", 10.0, False)
    ]
    assert book.unacknowledged(only=done) == []
    assert book.unacknowledged(replayed=True)[0].replayed is True


def test_acknowledging_needs_a_result_and_matches_the_epoch_when_one_is_given():
    book = ledger()
    late = watched(book)
    assert book.acknowledge(2, 9) is False and late.acked is False  # nothing to acknowledge yet
    book.take_answer(late, ReplyResult.OK, "reply", 110.0)
    assert book.acknowledge(2, 9, EPOCH + 1) is False and late.acked is False
    assert book.acknowledge(2, 8) is False and book.acknowledge(3, 9) is False
    assert book.acknowledge(2, 9, EPOCH) is True and late.acked is True
    assert book.unacknowledged() == []


def test_a_restart_replays_what_nobody_acknowledged_once_and_only_then():
    before = ledger()
    taken = watched(before, sequence=1)
    seen = watched(before, sequence=2)
    watched(before, sequence=3)  # never answered
    before.take_answer(taken, ReplyResult.OK, "reply", 110.0)
    before.take_answer(seen, ReplyResult.ERROR, "result", 120.0)
    before.acknowledge(2, 2)

    after = ledger()
    after.restore(before.records())
    first = after.replay()
    assert [(e.sequence, e.result, e.via, e.elapsed, e.replayed) for e in first] == [(1, 0, "reply", 10.0, True)]
    assert after.replay() == []  # the first call after a start only
    assert [e.replayed for e in after.unacknowledged()] == [False]  # asked for later: still unacknowledged, not a replay


def test_a_result_taken_after_the_replay_is_not_published_by_it():
    book = ledger()
    late = watched(book)
    assert book.replay() == []
    book.take_answer(late, ReplyResult.OK, "reply", 110.0)
    assert book.replay() == []


# ------------------------------------------------- keeping, forgetting


def test_a_record_is_forgotten_from_the_horizon_on_and_an_empty_node_goes_with_it():
    book = ledger()
    watched(book, sequence=1, first_sent=100.0)
    watched(book, sequence=2, first_sent=150.0)
    watched(book, sequence=3, node=5, first_sent=100.0)
    assert book.expire(199.0, 100.0) is False  # none is 100 s old
    assert len(book.records()) == 3
    assert book.expire(200.0, 100.0) is True  # exactly the horizon: forgotten
    assert [(late.node, late.sequence) for late in book.records()] == [(2, 2)]
    assert 5 not in book.records_by_node
    assert book.expire(200.0, 100.0) is False


# ------------------------------------------------------- state file side


def test_records_come_out_by_node_in_the_order_they_were_watched():
    book = ledger()
    watched(book, sequence=5, node=4)
    watched(book, sequence=2, node=2)
    watched(book, sequence=1, node=2)
    assert [(late.node, late.sequence) for late in book.records()] == [(2, 2), (2, 1), (4, 5)]


def test_restore_keys_records_by_message_id_so_a_late_reply_finds_them():
    book = ledger()
    late = watched(book, sequence=9, queries=(21,))
    answered = ledger()
    answered.restore([late])
    assert answered.get(2, EPOCH << 8 | 9) is late
    assert answered.match(2, EPOCH, 21) is late
    assert answered.protected_ids(2, EPOCH) == {EPOCH << 8 | 9, EPOCH << 8 | 21}
