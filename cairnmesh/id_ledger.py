"""The controller's ledger of per-node message-id counters and the ids they must not reuse.

``IdLedger`` is the one place that allocates from a node's counter, records a
REQUEST id once its first copy goes out, and releases settled ids when the
device's dedup window pushes their records out. The controller keeps which
other frames and late records an answer may still settle; it hands that set
to ``take_sequence`` and merges the ledger's protected request ids into
``Controller._protected_ids``. The ledger never holds the controller, only
the two limits and the random start it is given; every method is called with
the controller's lock held.
"""

from __future__ import annotations

import random
from typing import Optional

from .state_store import PersistedState


class IdLedger:
    """Per-node counters, REQUEST ids the device may keep, and those settled."""

    def __init__(
        self,
        *,
        keep_limit: int,
        dedup_window: int,
        initial_sequence: Optional[int] = None,
        rng: Optional[random.Random] = None,
    ) -> None:
        self._keep_limit = keep_limit
        self._dedup_window = dedup_window
        self._initial_sequence = initial_sequence
        self._rng = rng or random.Random()
        # [MEM] per-node sequence counters | 端: controller | 上限: 255 个节点 | 每项: 1 B
        self._next_sequence: dict[int, int] = {}
        # [MEM] REQUEST ids the device may still keep, in send order (persisted) | 端: controller | 上限: KEPT_IDS_MAX(256) × 节点数 | 每项: 2 B
        self._request_ids: dict[int, list[int]] = {}
        # [MEM] those of them settled (the device's record closed) | 端: controller | 上限: 同上 | 每项: 2 B
        self._closed_ids: dict[int, set[int]] = {}

    # ----------------------------------------------------------- read views

    @property
    def next_sequence(self) -> dict[int, int]:
        """The per-node counters, for state snapshots and tests."""

        return self._next_sequence

    @property
    def request_ids(self) -> dict[int, list[int]]:
        """REQUEST ids the device may still keep, per node, in send order."""

        return self._request_ids

    @property
    def closed_ids(self) -> dict[int, set[int]]:
        """Those REQUEST ids whose device record is settled."""

        return self._closed_ids

    # ------------------------------------------------------------- allocation

    def take_sequence(self, target_id: int, epoch: int, used: set[int]) -> int:
        """Take the next sequence for ``target_id`` in ``epoch``, skipping every id in ``used``.

        ``used`` contains EPOCH | SEQ message ids that an answer may still
        settle. Every id from this counter (REQUEST, query, WAKE, and the
        gateway's keepalives, probes, traces) skips the ids an answer from the
        node may still settle something by (``Controller._protected_ids``). A
        REQUEST id the device may still keep would be taken for a retry and
        answered without running; any frame taking the id of a waiting one
        would have its answer taken for that one's (2026-10-21 review A1, A4).
        A query carries its request's EPOCH (an older one after a restart):
        checked in that EPOCH, or a late reply to an old request with the same
        id would be taken for the answer.
        """

        sequence = self._next_sequence.get(target_id)
        if sequence is None:
            sequence = (
                self._rng.randrange(256)
                if self._initial_sequence is None
                else self._initial_sequence & 0xFF
            )
        mark = epoch << 8
        for _ in range(256):
            if mark | sequence not in used:
                break
            sequence = (sequence + 1) & 0xFF
        self._next_sequence[target_id] = (sequence + 1) & 0xFF
        return sequence

    def protected_ids(self, target_id: int, epoch: int) -> set[int]:
        """REQUEST ids (EPOCH | SEQ, this ``epoch``) of ``target_id`` the device may still keep."""

        return {mid for mid in self._request_ids.get(target_id, ()) if mid >> 8 == epoch}

    # ------------------------------------------------------- keeping, release

    def record_request(self, target_id: int, message_id: int) -> None:
        """Keep a REQUEST id from its first send until the device's dedup window releases it."""

        kept = self._request_ids.setdefault(target_id, [])
        kept.append(message_id)
        if len(kept) > self._keep_limit:
            self._closed_ids.get(target_id, set()).discard(kept.pop(0))

    def settle(self, target_id: int, message_id: int) -> None:
        """The device's record of ``message_id`` is closed: drop the ids it no longer keeps (lock held).

        A closed record is pushed out once ``DEDUP_WINDOW`` records made after
        it are closed: every settled id before the ``DEDUP_WINDOW``-th last
        settled one is gone there. One never settled stays (it may be open).
        """

        kept = self._request_ids.get(target_id)
        if not kept or message_id not in kept:
            return
        closed = self._closed_ids.setdefault(target_id, set())
        closed.add(message_id)
        seen = 0
        for index in range(len(kept) - 1, -1, -1):
            if kept[index] in closed:
                seen += 1
                if seen == self._dedup_window:
                    gone = {mid for mid in kept[:index] if mid in closed}
                    if gone:
                        kept[:index] = [mid for mid in kept[:index] if mid not in gone]
                        closed -= gone
                    return

    # -------------------------------------------------------------- restore

    def restore(self, saved: PersistedState) -> None:
        """Take over the tables the state file held."""

        self._request_ids.update(saved.request_ids)
        self._closed_ids.update(saved.closed_ids)
        self._next_sequence.update(saved.next_sequence)
