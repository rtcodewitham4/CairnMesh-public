"""Outcome leaves and pure builders extracted from the controller."""

from __future__ import annotations

import ast
from concurrent.futures import Future
from dataclasses import fields
from pathlib import Path

import pytest

import cairnmesh.controller as controller
from cairnmesh.events import RequestConfirming, RequestFailed
from cairnmesh.frames import QUERY_ACTION, FrameKind, ReplyResult, RequestFrame, WakeFrame
from cairnmesh.outcomes import (
    ACCEPTED,
    DONE,
    INTERRUPTED,
    INTERRUPTED_NOTE,
    NOT_RUN,
    NOT_SENT,
    UNCONFIRMED,
    RequestOutcome,
    _unknown_reply_result_reason,
    confirming_event,
    failed_event,
    make_outcome,
    not_sent_pair,
)
from cairnmesh.pending import Pending


ORIGINAL_CONTROLLER_NAMES = (
    "ACCEPTED",
    "BROADCAST_ID",
    "COMPLETION_CHECK",
    "CONFIRM_HEARD_FRESH",
    "CONFIRM_NUDGE_GAP",
    "Callable",
    "ConfirmPolicy",
    "Confirming",
    "Controller",
    "DEDUP_WINDOW",
    "DEFAULT_CONFIRM_LIMIT",
    "DEFAULT_CONFIRM_QUIET_AFTER",
    "DEFAULT_CONFIRM_SCHEDULE",
    "DEFAULT_CONFIRM_WINDOW",
    "DEFAULT_CONTROLLER_ID",
    "DEFAULT_MAX_RETRY_WINDOW",
    "DEFAULT_RECORD_KEEP",
    "DONE",
    "EventBus",
    "EventFrame",
    "FrameKind",
    "FrameRejected",
    "Future",
    "HeardBook",
    "HeartbeatFrame",
    "HeartbeatReceived",
    "INTERRUPTED",
    "INTERRUPTED_NOTE",
    "IdLedger",
    "KEPT_IDS_LIMIT",
    "KEPT_IDS_MAX",
    "LateConfirmation",
    "LateLedger",
    "LateRecord",
    "LinkError",
    "LinkEvent",
    "MAX_PAST_CONFIRMING",
    "MIN_WAKE_RETRIES",
    "NOT_RUN",
    "NOT_SENT",
    "NoStateFile",
    "NodeConfirming",
    "NodeEvent",
    "NodeReported",
    "Optional",
    "OrderedDict",
    "OutgoingFrame",
    "PUBLISHED_KEPT",
    "PersistedRequest",
    "PersistedState",
    "QUERY_ACTION",
    "QUERY_IDS_KEPT",
    "QueueFull",
    "RECORD_KEEP_MARGIN",
    "Random",
    "ReplyFrame",
    "ReplyIgnored",
    "ReplyReceived",
    "ReplyResult",
    "ReportFrame",
    "RequestCompleted",
    "RequestConfirming",
    "RequestFailed",
    "RequestFrame",
    "RequestOutcome",
    "RequestQuerying",
    "RequestSent",
    "ResultFrame",
    "StateStore",
    "Transport",
    "UNCONFIRMED",
    "UNCONFIRMED_NOTE",
    "Union",
    "VOLATILE_EPOCH_ALLOWED",
    "WakeFrame",
    "_Completion",
    "_KNOWN_REPLY_RESULTS",
    "_Pending",
    "_SideEffects",
    "_is_unknown_reply_result",
    "_require_unicast",
    "_uncancellable_future",
    "_unknown_reply_result_reason",
    "annotations",
    "dataclass",
    "decode_frame",
    "deque",
    "field",
    "math",
    "threading",
    "time",
)


def make_pending(**values) -> Pending:
    frame = values.pop(
        "frame",
        RequestFrame(sender_id=9, target_id=3, sequence=7, epoch=4, action=1, value=2),
    )
    defaults = {
        "frame": frame,
        "timeout": 0.5,
        "max_retries": 3,
        "attempts": 2,
        "first_sent": 10.0,
        "deadline": 130.0,
    }
    defaults.update(values)
    return Pending(**defaults)


def event_payload(event) -> dict:
    return {item.name: getattr(event, item.name) for item in fields(event) if item.name != "at"}


@pytest.mark.parametrize(
    ("result", "not_sent", "status"),
    [
        (None, False, UNCONFIRMED),
        (ReplyResult.OK, False, DONE),
        (ReplyResult.ERROR, False, DONE),
        (ReplyResult.NOT_RUN, False, NOT_RUN),
        (ReplyResult.ACCEPTED, False, ACCEPTED),
        (ReplyResult.INTERRUPTED, False, INTERRUPTED),
        (0x05, False, UNCONFIRMED),
        (ReplyResult.OK, True, NOT_SENT),
    ],
)
def test_request_outcome_status_covers_every_mapping(result, not_sent, status):
    outcome = RequestOutcome(
        node_id=3,
        sequence=7,
        kind=FrameKind.REQUEST,
        result=result,
        attempts=2,
        elapsed=1.5,
        not_sent=not_sent,
    )

    assert outcome.status == status


@pytest.mark.parametrize(
    ("result", "reason", "expected"),
    [
        (0x7F, "显式原因", "显式原因"),
        (ReplyResult.INTERRUPTED, "显式原因", "显式原因"),
        (0x7F, "", _unknown_reply_result_reason(0x7F)),
        (ReplyResult.INTERRUPTED, "", INTERRUPTED_NOTE),
        (ReplyResult.OK, "", ""),
    ],
)
def test_make_outcome_reason_precedence(result, reason, expected):
    pending = make_pending(frame=RequestFrame(sender_id=9, target_id=3, sequence=7, epoch=4, action=1, value=2))

    assert make_outcome(pending, result, now=12.5, reason=reason).reason == expected


def test_make_outcome_copies_all_pending_fields():
    completion = Future()
    pending = make_pending(completion=completion)

    outcome = make_outcome(pending, ReplyResult.OK, now=12.5, reason="")

    assert outcome == RequestOutcome(
        node_id=3,
        sequence=7,
        kind=FrameKind.REQUEST,
        result=ReplyResult.OK,
        attempts=2,
        elapsed=2.5,
        queried=False,
        reason="",
        completion=completion,
    )
    assert outcome.completion is completion


def test_make_outcome_uses_asked_request_and_marks_queried():
    asked = RequestFrame(sender_id=9, target_id=5, sequence=11, epoch=6, action=1, value=2)
    query = RequestFrame(sender_id=9, target_id=5, sequence=12, epoch=6, action=QUERY_ACTION, value=0)
    pending = make_pending(frame=query, asked=asked)

    outcome = make_outcome(pending, ReplyResult.NOT_RUN, now=12.5, reason="")

    assert outcome == RequestOutcome(
        node_id=5,
        sequence=11,
        kind=FrameKind.REQUEST,
        result=ReplyResult.NOT_RUN,
        attempts=2,
        elapsed=2.5,
        queried=True,
        reason="",
        completion=None,
    )
    assert outcome.queried is True


def test_failed_event_fields_are_unchanged():
    pending = make_pending()

    assert event_payload(failed_event(pending, "未确认", abandoned=True, expired=True)) == event_payload(
        RequestFailed(
            node_id=3,
            sequence=7,
            kind=FrameKind.REQUEST,
            attempts=2,
            reason="未确认",
            abandoned=True,
            epoch=4,
            expired=True,
        )
    )


@pytest.mark.parametrize("epoch", [None, 4])
def test_failed_event_wake_frame_epoch_and_watchable(epoch):
    wake = WakeFrame(sender_id=9, target_id=3, sequence=7, epoch=4)
    wake.epoch = epoch  # type: ignore[assignment]  # a legacy WAKE may carry no epoch
    pending = make_pending(frame=wake)

    assert pending.watchable is False
    assert event_payload(failed_event(pending, "未确认")) == event_payload(
        RequestFailed(
            node_id=3,
            sequence=7,
            kind=FrameKind.WAKE,
            attempts=2,
            reason="未确认",
            epoch=epoch,
        )
    )


def test_confirming_event_fields_are_unchanged():
    pending = make_pending(rounds=2, important=False)

    assert event_payload(confirming_event(pending, now=100.0, heard=95.0, limit_in=17.5)) == event_payload(
        RequestConfirming(
            node_id=3,
            sequence=7,
            epoch=4,
            age=90.0,
            heard_ago=5.0,
            rounds=2,
            next_in=30.0,
            important=False,
            limit_in=17.5,
        )
    )


def test_confirming_event_without_a_heard_time_clamps_next_in_at_zero():
    pending = make_pending(first_sent=10.0, deadline=95.0, rounds=3, important=True)

    assert event_payload(confirming_event(pending, now=100.0, heard=None, limit_in=None)) == event_payload(
        RequestConfirming(
            node_id=3,
            sequence=7,
            epoch=4,
            age=90.0,
            heard_ago=None,
            rounds=3,
            next_in=0.0,
            important=True,
            limit_in=None,
        )
    )


def test_not_sent_pair_fields_are_unchanged():
    frame = RequestFrame(sender_id=9, target_id=3, sequence=8, epoch=4, action=2, value=5)

    event, outcome = not_sent_pair(frame)

    assert event_payload(event) == event_payload(
        RequestFailed(
            node_id=3,
            sequence=8,
            kind=FrameKind.REQUEST,
            attempts=0,
            reason="未发出：前面的命令进入确认中",
            status=NOT_SENT,
            epoch=4,
        )
    )
    assert outcome == RequestOutcome(
        node_id=3,
        sequence=8,
        kind=FrameKind.REQUEST,
        result=None,
        attempts=0,
        elapsed=0.0,
        reason="未发出：前面的命令进入确认中",
        not_sent=True,
    )


@pytest.mark.parametrize(
    ("frame", "completion_check", "watchable"),
    [
        (RequestFrame(sender_id=9, target_id=3, sequence=7, epoch=4, action=1, value=0), False, True),
        (RequestFrame(sender_id=9, target_id=3, sequence=7, epoch=4, action=QUERY_ACTION, value=1), False, False),
        (RequestFrame(sender_id=9, target_id=3, sequence=7, epoch=4, action=1, value=0), True, False),
    ],
)
def test_pending_watchable(frame, completion_check, watchable):
    assert make_pending(frame=frame, completion_check=completion_check).watchable is watchable


@pytest.mark.parametrize("name", ORIGINAL_CONTROLLER_NAMES)
def test_original_controller_import_surface_is_available(name):
    imported = {}

    exec(f"from cairnmesh.controller import {name}", imported)

    assert imported[name] is getattr(controller, name)


@pytest.mark.parametrize("module_name", ["pending", "outcomes"])
def test_new_leaf_modules_do_not_import_controller_stack(module_name):
    path = Path(__file__).resolve().parents[2] / "cairnmesh" / f"{module_name}.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            imported.add(module)
            imported.add(module.lstrip("."))
            imported.update(alias.name for alias in node.names)

    # ``from .controller import ...`` has module == "controller"; an absolute
    # ``import cairnmesh.controller`` keeps the dotted name.
    assert imported.isdisjoint({"threading", "transport", "controller", "EventBus", "Controller"})
    assert "cairnmesh.controller" not in imported
