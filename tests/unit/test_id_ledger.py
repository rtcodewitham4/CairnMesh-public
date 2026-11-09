"""IdLedger on its own: counter wrapping, protected ids, and device-window release."""

from __future__ import annotations

import pytest

from cairnmesh.id_ledger import IdLedger
from cairnmesh.state_store import PersistedState

EPOCH = 7


def ledger(*, keep_limit: int = 8, dedup_window: int = 4, initial_sequence=3, rng=None) -> IdLedger:
    return IdLedger(
        keep_limit=keep_limit,
        dedup_window=dedup_window,
        initial_sequence=initial_sequence,
        rng=rng,
    )


def recorded(book: IdLedger, *sequences: int, node: int = 2, epoch: int = EPOCH) -> None:
    for sequence in sequences:
        book.record_request(node, epoch << 8 | sequence)


# ------------------------------------------------------------- allocation


def test_counters_start_per_node_and_wrap_with_the_epoch_mark_only_in_protection():
    book = ledger(initial_sequence=254)
    assert book.take_sequence(1, EPOCH, set()) == 254
    assert book.take_sequence(1, EPOCH, set()) == 255
    assert book.take_sequence(1, EPOCH, set()) == 0
    assert book.take_sequence(2, EPOCH, set()) == 254
    assert book.next_sequence == {1: 1, 2: 255}


def test_a_random_start_is_drawn_once_per_node():
    class FixedRng:
        def __init__(self) -> None:
            self.calls = 0

        def randrange(self, stop: int) -> int:
            assert stop == 256
            self.calls += 1
            return 200

    rng = FixedRng()
    book = ledger(initial_sequence=None, rng=rng)
    assert book.take_sequence(1, EPOCH, set()) == 200
    assert book.take_sequence(2, EPOCH, set()) == 200
    assert book.take_sequence(1, EPOCH, set()) == 201
    assert rng.calls == 2


def test_taking_a_sequence_skips_protected_ids_and_wraps_at_255():
    book = ledger(initial_sequence=254)
    used = {EPOCH << 8 | 254, EPOCH << 8 | 255, EPOCH << 8 | 0}
    assert book.take_sequence(1, EPOCH, used) == 1
    assert book.next_sequence[1] == 2


def test_protection_is_per_node_and_epoch():
    book = ledger()
    recorded(book, 2, 9, node=2, epoch=EPOCH)
    recorded(book, 4, node=3, epoch=EPOCH)
    assert book.protected_ids(2, EPOCH) == {EPOCH << 8 | 2, EPOCH << 8 | 9}
    assert book.protected_ids(2, EPOCH + 1) == set()
    assert book.protected_ids(3, EPOCH) == {EPOCH << 8 | 4}


# --------------------------------------------------------- keeping, release


def test_request_ids_are_kept_in_send_order_and_the_oldest_and_its_settled_mark_are_dropped():
    book = ledger(keep_limit=3)
    recorded(book, 1, 2, 3)
    book.settle(2, EPOCH << 8 | 1)
    recorded(book, 4)
    assert book.request_ids[2] == [EPOCH << 8 | 2, EPOCH << 8 | 3, EPOCH << 8 | 4]
    assert book.closed_ids[2] == set()


def test_settling_an_untracked_id_changes_nothing():
    book = ledger()
    book.settle(2, EPOCH << 8 | 9)
    assert book.request_ids == {}
    assert book.closed_ids == {}


def test_a_sent_request_stays_protected_until_the_dedup_window_passes_it():
    book = ledger(dedup_window=4)
    recorded(book, 1, 2, 3, 4, 5, 6)
    for sequence in (2, 3, 4, 5):
        book.settle(2, EPOCH << 8 | sequence)
    assert book.closed_ids[2] == {EPOCH << 8 | sequence for sequence in (2, 3, 4, 5)}
    assert {EPOCH << 8 | sequence for sequence in (1, 2, 3, 4, 5, 6)} <= book.protected_ids(2, EPOCH)

    book.settle(2, EPOCH << 8 | 6)
    assert book.request_ids[2] == [EPOCH << 8 | sequence for sequence in (1, 3, 4, 5, 6)]
    assert book.closed_ids[2] == {EPOCH << 8 | sequence for sequence in (3, 4, 5, 6)}
    assert book.protected_ids(2, EPOCH) == {EPOCH << 8 | sequence for sequence in (1, 3, 4, 5, 6)}


def test_never_settled_ids_stay_even_when_the_device_window_passes_them():
    book = ledger(dedup_window=2, keep_limit=6)
    recorded(book, 1, 2, 3, 4, 5)
    book.settle(2, EPOCH << 8 | 2)
    book.settle(2, EPOCH << 8 | 4)
    book.settle(2, EPOCH << 8 | 5)
    assert book.request_ids[2] == [EPOCH << 8 | sequence for sequence in (1, 3, 4, 5)]
    assert book.protected_ids(2, EPOCH) == {EPOCH << 8 | sequence for sequence in (1, 3, 4, 5)}


def test_repeated_settlement_is_idempotent():
    book = ledger(dedup_window=3)
    recorded(book, 1, 2)
    book.settle(2, EPOCH << 8 | 1)
    book.settle(2, EPOCH << 8 | 1)
    assert book.closed_ids[2] == {EPOCH << 8 | 1}


# --------------------------------------------------------------- restore


def test_restore_takes_over_all_three_tables():
    saved = PersistedState(
        epoch=EPOCH,
        request_ids={2: [EPOCH << 8 | 5, EPOCH << 8 | 6]},
        closed_ids={2: {EPOCH << 8 | 5}},
        next_sequence={2: 7},
    )
    book = ledger()
    book.restore(saved)
    assert book.request_ids == saved.request_ids
    assert book.closed_ids == saved.closed_ids
    assert book.next_sequence == saved.next_sequence
    assert book.take_sequence(2, EPOCH, book.protected_ids(2, EPOCH)) == 7


@pytest.mark.parametrize("initial", [0, 255, 256, -1])
def test_the_initial_sequence_is_reduced_to_one_octet(initial):
    book = ledger(initial_sequence=initial)
    assert book.take_sequence(1, EPOCH, set()) == initial & 0xFF


def test_a_fully_used_epoch_probes_all_256_sequences_and_comes_back_to_the_start():
    book = ledger(initial_sequence=5)
    used = {EPOCH << 8 | sequence for sequence in range(256)}
    # every id is taken: the search goes round once (256 steps) and gives up at the start
    assert book.take_sequence(1, EPOCH, used) == 5
    assert book.take_sequence(1, EPOCH, used) == 6


def test_a_single_free_id_is_found_wherever_it_sits_in_the_round():
    book = ledger(initial_sequence=5)
    used = {EPOCH << 8 | sequence for sequence in range(256)} - {EPOCH << 8 | 4}
    assert book.take_sequence(1, EPOCH, used) == 4  # the last one probed (255 steps away)
