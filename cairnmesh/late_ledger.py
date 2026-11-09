"""The controller's ledger of commands that ended ``UNCONFIRMED``, and the late answers taken for them.

``LateLedger`` is the one place that adds, changes or removes a ``LateRecord``
(the entry the state file keeps under ``late``). It knows when a late answer
is taken and when it is not, which results are still to be published or
acknowledged, when a record is forgotten, and which message ids its records
keep protected. The controller keeps everything else about the command: it
decides which ended commands are watched, closes the device's record (the
settled ids) after a taken answer, and decides when the state file is written;
the ledger returns what happened and the controller turns it into events and
snapshots. The ledger never holds the controller, only the one limit it is given;
every method is called with the controller's lock held.
"""

from __future__ import annotations

from typing import Optional

from .events import LateConfirmed
from .frames import ReplyResult, RequestFrame
from .state_store import LateRecord

#: Results a late answer is taken for, once a command has ended ``UNCONFIRMED``. Not ``NOT_RUN``
#: (the device's record may have been pushed out by what came after: a wrong "never ran" would get it sent twice),
#: not ``ACCEPTED`` (its RESULT will come on its own), not an unknown code.
_LATE_RESULTS = frozenset({int(ReplyResult.OK), int(ReplyResult.ERROR), int(ReplyResult.INTERRUPTED)})


class LateLedger:
    """Ended commands per node, keyed by their message id (EPOCH | SEQ), the oldest first."""

    def __init__(self, *, keep_limit: int) -> None:
        #: Records kept per node (``KEPT_IDS_MAX``): the oldest dropped past it.
        self._keep_limit = keep_limit
        # [MEM] commands ended UNCONFIRMED: ids kept protected, late result taken (persisted) | 端: controller | 上限: KEPT_IDS_MAX(256) × 节点数 | 每项: 节点、EPOCH、SEQ、动作、值、首发时刻、≤4 个查询编号、迟到结果
        self._records: dict[int, dict[int, LateRecord]] = {}
        #: The first ``replay`` after a start publishes the late results nobody acknowledged yet.
        self._replay_due = True

    @property
    def records_by_node(self) -> dict[int, dict[int, LateRecord]]:
        """The table itself (node → message id → record), to look into: only this class writes it."""

        return self._records

    # ------------------------------------------------------------- watching

    def watch(self, original: RequestFrame, first_sent: float, queries: list, important: bool) -> None:
        """A command ends ``UNCONFIRMED``: keep its ids protected and take a late positive result (lock held)."""

        entries = self._records.setdefault(original.target_id, {})
        entries.pop(original.message_id, None)
        entries[original.message_id] = LateRecord(
            node=original.target_id,
            epoch=original.epoch,
            sequence=original.sequence,
            action=original.action,
            value=original.value,
            first_sent=first_sent,
            queries=list(queries),
            important=important,
        )
        while len(entries) > self._keep_limit:
            del entries[next(iter(entries))]

    def protected_ids(self, target_id: int, epoch: int) -> set[int]:
        """Message ids (EPOCH | SEQ, this ``epoch``) of ``target_id``'s ended commands: their REQUEST id and last query ids.

        Commands ended UNCONFIRMED: their REQUEST id and last query ids stay "not settled" for the keep period,
        so a late answer is never taken for a new command's. This set is part of what
        ``Controller._protected_ids`` hands out; nothing may be dropped from it before ``expire`` forgets the record.
        """

        mark = epoch << 8
        used: set[int] = set()
        for late in self._records.get(target_id, {}).values():
            if late.epoch == epoch:
                used.add(mark | late.sequence)
                used.update(mark | query for query in late.queries)
        return used

    # ------------------------------------------------------------- answers

    def get(self, node: int, message_id: int) -> Optional[LateRecord]:
        return self._records.get(node, {}).get(message_id)

    def match(self, node: int, epoch: int, sequence: int) -> Optional[LateRecord]:
        """The ended command a REPLY with this EPOCH / SEQ answers: its own id or one of its last query ids."""

        for late in self._records.get(node, {}).values():
            if late.epoch == epoch and (sequence == late.sequence or sequence in late.queries):
                return late
        return None

    def take_answer(self, late: LateRecord, result: int, via: str, now: float) -> Optional[str]:
        """A REPLY / RESULT for a command that ended ``UNCONFIRMED`` (lock held): ``None`` when taken, else why not.

        Only ``OK`` / ``ERROR`` / ``INTERRUPTED`` count. ``NOT_RUN`` is never taken here (after the limit nothing
        keeps the device's record in place: what came after can push it out, and a wrong "never ran" would have
        the operator send it twice); ``ACCEPTED`` and unknown codes are not either. The command stays unconfirmed.
        The reason is the text of the ``ReplyIgnored`` the controller publishes.
        """

        if int(result) not in _LATE_RESULTS or late.result is not None:
            return "已记录过迟到结果" if late.result is not None else "到点后的 NOT_RUN / ACCEPTED / 未知结果码不采信，命令保持未确认"
        late.result = int(result)
        late.via = via
        late.elapsed = max(0.0, now - late.first_sent)
        late.acked = False
        return None

    # ----------------------------------------------- publishing, acknowledging

    def unacknowledged(self, replayed: bool = False, only: Optional[LateRecord] = None) -> list[LateConfirmed]:
        """``LateConfirmed`` for every record with a result nobody acknowledged (``only``: just that record)."""

        found = []
        for entries in self._records.values():
            for late in entries.values():
                if late.result is None or late.acked or (only is not None and late is not only):
                    continue
                found.append(
                    LateConfirmed(
                        node_id=late.node,
                        sequence=late.sequence,
                        epoch=late.epoch,
                        result=late.result,
                        via=late.via,
                        elapsed=late.elapsed,
                        replayed=replayed,
                    )
                )
        return found

    def replay(self) -> list[LateConfirmed]:
        """The late results nobody acknowledged, the first time it is called after a start; nothing after that."""

        if not self._replay_due:
            return []
        self._replay_due = False
        return self.unacknowledged(replayed=True)

    def acknowledge(self, node_id: int, sequence: int, epoch: Optional[int] = None) -> bool:
        """The consumer took note of a late result: not published again after a restart. The ids stay protected."""

        found = False
        for late in self._records.get(node_id, {}).values():
            if late.sequence == sequence and (epoch is None or late.epoch == epoch) and late.result is not None:
                late.acked = found = True
        return found

    # ------------------------------------------------------- keeping, forgetting

    def expire(self, now: float, horizon: float) -> bool:
        """Forget ended commands past the devices' keep period (their records may be gone: nothing is left to guard).

        ``horizon`` is the age from which a record is forgotten (the keep period less its margin); ``True``: something was.
        """

        changed = False
        for node in list(self._records):
            entries = self._records[node]
            for key in [k for k, late in entries.items() if now - late.first_sent >= horizon]:
                del entries[key]
                changed = True
            if not entries:
                del self._records[node]
        return changed

    def records(self) -> list[LateRecord]:
        """Every record, by node, for the state file."""

        return [entry for node in sorted(self._records) for entry in self._records[node].values()]

    def restore(self, saved: list[LateRecord]) -> None:
        """Take over the records the state file held."""

        for late in saved:
            self._records.setdefault(late.node, {})[late.epoch << 8 | late.sequence] = late
