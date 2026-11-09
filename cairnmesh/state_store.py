"""The controller's state file: what is written, read back, and how it gets to disk.

``StateStore`` is the one place that knows the file (format version 1): it turns
the ``PersistedState`` the controller hands it into a snapshot, writes
snapshots one at a time and only ever the newest, and reads a file back into
a ``PersistedState`` (damaged late entries skipped, with a warning). The
controller keeps everything about its queues: it decides what is persisted
and rebuilds its own requests from what is read. The store never holds the
controller, only the clock and the two limits it is given.

This module also holds the records the file keeps (``PersistedRequest``,
``LateRecord``, ``PersistedState``); ``controller`` uses ``LateRecord`` as the
entry of its late table.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from .durable import write_json


@dataclass
class LateRecord:
    """A command that ended ``UNCONFIRMED``: its ids stay protected and a late positive result is taken."""

    node: int
    epoch: int
    sequence: int
    action: int
    value: int
    first_sent: float
    #: The last query sequences sent about it (``QUERY_IDS_KEPT``).
    queries: list
    important: bool
    #: The late result (``OK`` / ``ERROR`` / ``INTERRUPTED``) and how it came; ``None`` until then.
    result: Optional[int] = None
    via: str = ""
    elapsed: float = 0.0
    #: The consumer took note (``Controller.acknowledge_late``): no replay after a restart.
    acked: bool = False


@dataclass
class PersistedRequest:
    """A REQUEST on the air (or put aside by ``force``) with no outcome yet: file entry of ``confirming``.

    ``first_sent`` is on the controller's clock; the file keeps it as wall time.
    """

    node: int
    epoch: int
    sequence: int
    action: int
    value: int
    first_sent: float
    rounds: int
    passed: int
    important: bool
    queries: list


@dataclass
class PersistedState:
    """What the state file holds, field for field (version 1)."""

    #: As the file has it: ``load`` checks it is a number in 0..255 but hands it back unchanged.
    epoch: int
    request_ids: dict[int, list[int]] = field(default_factory=dict)
    closed_ids: dict[int, set[int]] = field(default_factory=dict)
    next_sequence: dict[int, int] = field(default_factory=dict)
    confirming: list[PersistedRequest] = field(default_factory=list)
    late: list[LateRecord] = field(default_factory=list)
    #: Set by ``load`` only: the controller clock reading the ``first_sent`` values were converted with.
    restored_at: Optional[float] = None


class StateStore:
    """Snapshots, durable writes and reads of one state file.

    ``snapshot`` numbers snapshots in the order taken, so the caller takes
    them under its own lock; ``write`` may then run outside it, from several
    threads: it writes one at a time and never an older snapshot over a newer
    one. ``failed`` is set by a write that raised and cleared by the next one
    that succeeds; the controller holds REQUESTs back while it is set.
    ``warning`` is what ``load`` had to say (``None``: nothing).
    """

    def __init__(
        self,
        path: str,
        clock: Callable[[], float],
        *,
        kept_ids: int,
        kept_queries: int,
    ) -> None:
        self.path = path
        self._clock = clock
        #: Request ids kept per node (``KEPT_IDS_MAX``) and query ids per command (``QUERY_IDS_KEPT``).
        self._kept_ids = kept_ids
        self._kept_queries = kept_queries
        self.warning: Optional[str] = None
        # State file writes (review D2): taken in order (``seq``), written one at a
        # time outside the controller's ``_lock``, an older snapshot never over a newer one; after a
        # failed write REQUESTs wait until one succeeds (``tick`` writes again).
        self._write_lock = threading.Lock()
        self._seq = 0
        self._written = 0
        self.failed = False

    # ------------------------------------------------------------- writing

    def snapshot(self, state: PersistedState) -> dict:
        """``state`` as the file's document, numbered (call under the controller's lock)."""

        wall = time.time()
        now = self._clock()
        confirming = [
            {
                "node": request.node,
                "epoch": request.epoch,
                "sequence": request.sequence,
                "action": request.action,
                "value": request.value,
                "since": wall - (now - request.first_sent),
                "rounds": request.rounds,
                "passed": request.passed,
                "important": request.important,
                "queries": list(request.queries),
            }
            for request in state.confirming
        ]
        late = [
            {
                "node": entry.node,
                "epoch": entry.epoch,
                "sequence": entry.sequence,
                "action": entry.action,
                "value": entry.value,
                "since": wall - (now - entry.first_sent),
                "queries": list(entry.queries),
                "important": entry.important,
                "result": entry.result,
                "via": entry.via,
                "elapsed": entry.elapsed,
                "acked": entry.acked,
            }
            for entry in state.late
        ]
        self._seq += 1
        return {
            "version": 1,
            "seq": self._seq,
            "epoch": state.epoch,
            "saved_at": wall,
            "request_ids": {str(node): list(ids) for node, ids in state.request_ids.items()},
            "closed_ids": {str(node): sorted(ids) for node, ids in state.closed_ids.items() if ids},
            "next_sequence": {str(node): sequence for node, sequence in state.next_sequence.items()},
            "confirming": confirming,
            "late": late,
        }

    def write(self, snapshot: dict) -> None:
        """Put ``snapshot`` on disk unless a newer one is there already; ``OSError`` if it cannot be written."""

        with self._write_lock:
            if snapshot["seq"] <= self._written:
                return  # a newer snapshot is on disk already
            try:
                write_json(self.path, snapshot)  # fsynced, the rename too (review B3)
            except OSError:
                self.failed = True
                raise
            self._written = snapshot["seq"]
            self.failed = False

    # ------------------------------------------------------------- reading

    def load(self) -> Optional[PersistedState]:
        """The state in the file, or ``None`` (and ``warning`` says why) when it cannot be read."""

        try:
            with open(self.path, encoding="utf-8") as handle:
                state = json.load(handle)
            if state.get("version") != 1 or not 0 <= int(state["epoch"]) <= 0xFF:
                raise ValueError("unknown version")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            self.warning = (
                f"控制端状态文件 {self.path} 读不到（{type(exc).__name__}）：首次运行，或文件丢失。"
                "消息编号改由时钟推出，防撞车只能靠设备的记录保险期限"
            )
            return None
        return self._parse(state)

    def _warn(self, note: str) -> None:
        self.warning = note if self.warning is None else f"{self.warning}；{note}"

    def _parse(self, state: dict) -> PersistedState:
        request_ids: dict[int, list[int]] = {}
        for node, ids in state.get("request_ids", {}).items():
            request_ids[int(node)] = [int(i) for i in ids][-self._kept_ids :]
        closed_ids: dict[int, set[int]] = {}
        for node, ids in state.get("closed_ids", {}).items():  # absent (older file): none settled
            kept = set(request_ids.get(int(node), ()))
            closed_ids[int(node)] = {int(i) for i in ids} & kept
        next_sequence: dict[int, int] = {}
        for node, sequence in state.get("next_sequence", {}).items():
            next_sequence[int(node)] = int(sequence) & 0xFF
        now = self._clock()
        wall = time.time()
        late: list[LateRecord] = []
        bad_late = 0
        late_entries = state.get("late", [])  # absent (older file): none
        if not isinstance(late_entries, list):
            bad_late += 1
            late_entries = []
        for entry in late_entries:
            try:
                node = int(entry["node"])
                record = LateRecord(
                    node=node,
                    epoch=int(entry["epoch"]),
                    sequence=int(entry["sequence"]),
                    action=int(entry["action"]),
                    value=int(entry["value"]),
                    first_sent=now - max(0.0, wall - float(entry["since"])),
                    queries=[int(q) for q in entry.get("queries", [])][-self._kept_queries :],
                    # Anything but a real boolean: the safe side (important), as for a missing field.
                    important=entry.get("important") if isinstance(entry.get("important"), bool) else True,
                    result=None if entry.get("result") is None else int(entry["result"]),
                    via=str(entry.get("via", "")),
                    elapsed=float(entry.get("elapsed", 0.0)),
                    acked=entry.get("acked") is True,
                )
                if not all(0 <= value <= 0xFF for value in (record.epoch, record.sequence, *record.queries)) or not 0 <= node <= 0xFF:
                    raise ValueError("id out of range")
            except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
                bad_late += 1  # a damaged entry costs only itself
                continue
            late.append(record)
        if bad_late:
            self._warn(f"状态文件的 late 表有 {bad_late} 个损坏条目，已跳过（对应命令的迟到确认与编号保护丢失，保险期限内请留意）")
        unmarked = 0
        confirming: list[PersistedRequest] = []
        for entry in state.get("confirming", []):
            node = int(entry["node"])
            if not isinstance(entry.get("important"), bool):
                unmarked += 1
            confirming.append(
                PersistedRequest(
                    node=node,
                    epoch=int(entry["epoch"]),
                    sequence=int(entry["sequence"]),
                    action=int(entry["action"]),
                    value=int(entry["value"]),
                    first_sent=now - max(0.0, wall - float(entry["since"])),
                    rounds=int(entry.get("rounds", 0)),
                    passed=int(entry.get("passed", 0)),
                    # A file from before the importance property: confirm on, as before (safe side).
                    important=entry["important"] if isinstance(entry.get("important"), bool) else True,
                    queries=[int(q) for q in entry.get("queries", [])],
                )
            )
        if unmarked:
            self._warn(
                f"状态文件里有 {unmarked} 条命令没有重要性标记（旧版本写的）：按“重要”处理，继续长期确认，"
                "不在到点时结束；需要时请操作员放弃"
            )
        return PersistedState(
            epoch=state["epoch"],
            request_ids=request_ids,
            closed_ids=closed_ids,
            next_sequence=next_sequence,
            confirming=confirming,
            late=late,
            restored_at=now,
        )
