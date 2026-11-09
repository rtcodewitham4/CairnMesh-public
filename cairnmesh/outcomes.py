"""Outcome values and pure event/outcome constructors for controller requests."""

from __future__ import annotations

from concurrent.futures import Future
from dataclasses import dataclass, field
from typing import Optional

from .events import RequestConfirming, RequestFailed
from .frames import FrameKind, ReplyResult, RequestFrame
from .pending import Pending

#: ``RequestOutcome.status`` values (Q18).
DONE = "done"  # the device answered: ``result`` is what the action returned
NOT_RUN = "not_run"  # the device confirmed it never ran it, and never will
UNCONFIRMED = "unconfirmed"  # unknown: operator, keep period, or an unknown result code
ACCEPTED = "accepted"  # the device logged it and runs it (once); ``completion`` brings the action's result
NOT_SENT = "not_sent"  # never sent: a command before it went confirming (review A3b); certainly not run
INTERRUPTED = "interrupted"  # the device reset with the action open: no final result; it may or may not have completed (2026-11-08)

#: What a display says about an ``INTERRUPTED`` command (spec 4.2): the device reset,
#: no final result exists, the action may have completed, the device handles it by the safe state its action table registers
#: (it is not claimed applied: an unregistered action has none). Never "not done".
INTERRUPTED_NOTE = "被中断：复位时无最终结果，动作可能已完成；设备按登记的安全态处理"

#: What a display says about an ordinary command that reached its confirm limit (spec 4.2): it may have run.
UNCONFIRMED_NOTE = "未确认：可能已执行；控制端不会自动重发，请先查询或现场核查再决定是否重发"

_NOT_SENT_NOTE = "未发出：前面的命令进入确认中"

#: Reply results this controller understands. Anything else is kept as its
#: raw integer and treated as an unconfirmed outcome, never as DONE.
_KNOWN_REPLY_RESULTS = frozenset(int(result) for result in ReplyResult)


def _is_unknown_reply_result(result: Optional[int]) -> bool:
    return result is not None and result not in _KNOWN_REPLY_RESULTS


def _unknown_reply_result_reason(result: int) -> str:
    return f"未知结果码 0x{int(result):02X}：设备是否已执行未确认"


@dataclass(frozen=True)
class RequestOutcome:
    """Final result of one request, delivered through its ``Future``."""

    node_id: int
    sequence: int
    kind: FrameKind
    #: ``None`` when no reply arrived before retries ran out.
    result: Optional[int]
    attempts: int
    elapsed: float
    #: The result came from (or was sought by) a status query (Q18).
    queried: bool = False
    #: Why an ``UNCONFIRMED`` outcome is unknown (operator, keep period,
    #: or a result code this controller does not know).
    reason: str = ""
    #: Never sent (``NOT_SENT``, review A3b): queued behind a request that
    #: went confirming; it certainly did not run, the operator sends it again.
    not_sent: bool = False
    #: ``ACCEPTED`` only: resolves with the action's result (``ReplyResult``)
    #: once its RESULT comes, or ``None`` if it can no longer be learnt
    #: (``RequestCompleted`` says the same).
    completion: Optional[Future] = field(default=None, compare=False)

    @property
    def ok(self) -> bool:
        return self.result == ReplyResult.OK

    @property
    def interrupted(self) -> bool:
        """The device reset with the action open: no final result (``INTERRUPTED_NOTE``)."""

        return self.result == ReplyResult.INTERRUPTED

    @property
    def accepted(self) -> bool:
        """The device logged it and runs it, exactly once; the result follows (``completion``)."""

        return self.result == ReplyResult.ACCEPTED

    @property
    def status(self) -> str:
        """``DONE``, ``ACCEPTED``, ``INTERRUPTED``, ``NOT_RUN``, ``NOT_SENT`` or ``UNCONFIRMED``: whether the device runs it is known in all but the last.

        ``INTERRUPTED`` is final and is not ``DONE``: the device reset with
        the action open, so no result exists (it may have completed, and it
        never runs again). ``ok`` is true for ``OK`` only.
        """

        if self.not_sent:
            return NOT_SENT
        if self.result is None:
            return UNCONFIRMED
        if self.result == ReplyResult.ACCEPTED:
            return ACCEPTED
        if self.result == ReplyResult.NOT_RUN:
            return NOT_RUN
        if self.result == ReplyResult.INTERRUPTED:
            return INTERRUPTED
        if _is_unknown_reply_result(self.result):
            return UNCONFIRMED
        return DONE


@dataclass(frozen=True)
class Confirming:
    """A request whose outcome is still being confirmed (``Controller.confirming``)."""

    node_id: int
    sequence: int
    epoch: int
    action: int
    value: int
    first_sent: float
    last_heard: Optional[float]
    rounds: int
    #: ``None`` while it waits behind requests sent past it with ``force``.
    next_query: Optional[float]
    #: Ordinary commands end at ``limit_in`` seconds from now (the hard confirm limit); important ones never by themselves.
    important: bool = True
    limit_in: Optional[float] = None


@dataclass(frozen=True)
class LateConfirmation:
    """A late positive result for a command that ended ``UNCONFIRMED`` (``Controller.late_confirmations``)."""

    node_id: int
    sequence: int
    epoch: int
    result: int
    via: str
    elapsed: float


def make_outcome(
    pending: Pending,
    result: Optional[int],
    now: float,
    reason: str,
) -> RequestOutcome:
    """Build a request's final outcome without mutating controller state."""

    original = pending.original
    if not reason and _is_unknown_reply_result(result):
        reason = _unknown_reply_result_reason(result)
    elif not reason and result == ReplyResult.INTERRUPTED:
        reason = INTERRUPTED_NOTE
    return RequestOutcome(
        node_id=original.target_id,
        sequence=original.sequence,
        kind=original.kind,
        result=result,
        attempts=pending.attempts,
        elapsed=now - pending.first_sent,
        queried=pending.asked is not None,
        reason=reason,
        completion=pending.completion,
    )


def failed_event(
    pending: Pending,
    reason: str,
    *,
    abandoned: bool = False,
    expired: bool = False,
) -> RequestFailed:
    """Build a ``RequestFailed`` event without appending it anywhere."""

    original = pending.original
    return RequestFailed(
        node_id=original.target_id,
        sequence=original.sequence,
        kind=original.kind,
        attempts=pending.attempts,
        reason=reason,
        abandoned=abandoned,
        epoch=getattr(original, "epoch", None),
        expired=expired,
    )


def confirming_event(
    pending: Pending,
    now: float,
    heard: Optional[float],
    limit_in: Optional[float],
) -> RequestConfirming:
    """Build a ``RequestConfirming`` event from a pending request."""

    original = pending.original
    return RequestConfirming(
        node_id=original.target_id,
        sequence=original.sequence,
        epoch=original.epoch,
        age=now - pending.first_sent,
        heard_ago=None if heard is None else now - heard,
        rounds=pending.rounds,
        next_in=max(0.0, pending.deadline - now),
        important=pending.important,
        limit_in=limit_in,
    )


def not_sent_pair(frame: RequestFrame) -> tuple[RequestFailed, RequestOutcome]:
    """Build the event and outcome for a request dropped before its first send."""

    return (
        RequestFailed(
            node_id=frame.target_id,
            sequence=frame.sequence,
            kind=frame.kind,
            attempts=0,
            reason=_NOT_SENT_NOTE,
            status=NOT_SENT,
            epoch=frame.epoch,
        ),
        RequestOutcome(
            node_id=frame.target_id,
            sequence=frame.sequence,
            kind=frame.kind,
            result=None,
            attempts=0,
            elapsed=0.0,
            reason=_NOT_SENT_NOTE,
            not_sent=True,
        ),
    )
