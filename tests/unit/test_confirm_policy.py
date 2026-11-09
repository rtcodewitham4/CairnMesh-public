"""Table-driven checks of the confirmation rules extracted in PR1."""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from cairnmesh.confirm_policy import (
    CONFIRM_HEARD_FRESH,
    CONFIRM_NUDGE_GAP,
    DEFAULT_CONFIRM_LIMIT,
    DEFAULT_CONFIRM_QUIET_AFTER,
    DEFAULT_CONFIRM_SCHEDULE,
    DEFAULT_CONFIRM_WINDOW,
    RECORD_KEEP_MARGIN,
    ConfirmPolicy,
    HeardBook,
)
from cairnmesh.controller import (
    CONFIRM_HEARD_FRESH as CONTROLLER_CONFIRM_HEARD_FRESH,
    CONFIRM_NUDGE_GAP as CONTROLLER_CONFIRM_NUDGE_GAP,
    DEFAULT_CONFIRM_LIMIT as CONTROLLER_DEFAULT_CONFIRM_LIMIT,
    DEFAULT_CONFIRM_QUIET_AFTER as CONTROLLER_DEFAULT_CONFIRM_QUIET_AFTER,
    DEFAULT_CONFIRM_SCHEDULE as CONTROLLER_DEFAULT_CONFIRM_SCHEDULE,
    DEFAULT_CONFIRM_WINDOW as CONTROLLER_DEFAULT_CONFIRM_WINDOW,
    RECORD_KEEP_MARGIN as CONTROLLER_RECORD_KEEP_MARGIN,
    Controller,
)


def policy(**changes):
    options = {
        "window": 5.0,
        "forever": True,
        "schedule": (120.0, 1800.0, 1800.0),
        "record_keep": 1000.0,
        "quiet_after": 600.0,
        "limit": 30.0,
    }
    options.update(changes)
    return ConfirmPolicy(**options)


@pytest.mark.parametrize(
    ("important", "confirming", "limit", "expected"),
    [
        (False, True, 30.0, 130.0),
        (True, True, 30.0, None),
        (False, False, 30.0, None),
        (False, True, None, None),
    ],
)
def test_i1_limit_at_is_only_the_ordinary_confirming_hard_point(important, confirming, limit, expected):
    assert policy(limit=limit).limit_at(100.0, important=important, confirming=confirming) == expected


def test_i1_limit_includes_the_exact_limit_boundary():
    rule = policy()
    limit = rule.limit_at(100.0, important=False, confirming=True)
    assert limit == 100.0 + 30.0
    assert rule.limit_in(limit, limit) == 0.0


@pytest.mark.parametrize(
    ("record_keep", "limit", "expected"),
    [
        (1000.0, None, 1050.0),  # first_sent 100 + record_keep * 0.95
        (None, 30.0, 130.0),
        (1000.0, 30.0, 130.0),
    ],
)
def test_i2_next_round_never_passes_the_keep_or_limit_cap(record_keep, limit, expected):
    rule = policy(record_keep=record_keep, limit=limit)
    due = rule.next_round_due(
        first_sent=100.0,
        last_round=100.0,
        now=1050.0,  # age == record_keep * 0.95 exactly
        fresh=False,
        quiet=True,
        limit=rule.limit_at(100.0, important=False, confirming=True),
    )
    assert due == expected


def test_i3_constructor_validates_once_and_the_policy_stays_mutable():
    with pytest.raises(ValueError):
        policy(window=10.0, limit=9.0)

    rule = policy(window=10.0, limit=20.0)
    rule.limit = 1.0  # the old instance attribute was mutable too: no revalidation
    rule.window = 100.0
    assert rule.limit_at(5.0, important=False, confirming=True) == 6.0


@pytest.mark.parametrize(
    ("record_keep", "age", "past", "cap"),
    [
        (1000.0, 1000.0 * RECORD_KEEP_MARGIN - 0.001, False, 1050.0),
        (1000.0, 1000.0 * RECORD_KEEP_MARGIN, True, 1050.0),
        (None, 1e12, False, None),
    ],
)
def test_i4_keep_horizon_has_one_source_and_keeps_each_original_form(record_keep, age, past, cap):
    rule = policy(record_keep=record_keep)
    assert rule.keep_horizon() == (None if record_keep is None else record_keep * RECORD_KEEP_MARGIN)
    assert rule.past_keep(age) is past
    assert rule.keep_cap(100.0) == cap


@pytest.mark.parametrize(
    ("deadline", "now", "last_round", "nudged", "resumed"),
    [
        (100.0, 20.0, 15.0, 25.0, 25.0),
        (100.0, 30.0, 15.0, 30.0, 30.0),
        (20.0, 30.0, 15.0, 20.0, 30.0),
    ],
)
def test_i5_nudges_only_advance_and_never_pass_the_round_gap(deadline, now, last_round, nudged, resumed):
    rule = policy()
    assert rule.nudged(deadline, now, last_round) == nudged
    assert rule.resume_due(now, last_round) == resumed
    assert resumed == max(now, last_round + CONFIRM_NUDGE_GAP)


@pytest.mark.parametrize(
    ("node_heard", "area_heard", "since", "now", "last", "heard_at", "fresh"),
    [
        (100.0, None, 50.0, 150.0, 100.0, 100.0, True),
        (100.0, 200.0, 50.0, 250.0, 100.0, 200.0, True),
        (40.0, None, 50.0, 200.0, None, float("-inf"), False),
        (
            100.0,
            200.0,
            50.0,
            200.0 + CONFIRM_HEARD_FRESH,
            100.0,
            200.0,
            False,
        ),  # exactly CONFIRM_HEARD_FRESH is stale
    ],
)
def test_i7_heard_book_keeps_node_and_area_freshness(node_heard, area_heard, since, now, last, heard_at, fresh):
    book = HeardBook()
    book.note(1, node_heard)
    if area_heard is not None:
        book.note_area(area_heard)
    assert book.last_heard(1, since) == last
    assert book.heard_at(1, since) == heard_at
    assert book.fresh(1, since, now) is fresh


@pytest.mark.parametrize(
    ("quiet_after", "now", "expected_calls"),
    [
        (None, 10_000.0, 0),
        (600.0, 599.999, 0),
        (600.0, 600.0, 1),
    ],
)
def test_i10_round_wakes_keeps_the_short_circuit_order(quiet_after, now, expected_calls):
    controller = object.__new__(Controller)
    controller.confirm = policy(quiet_after=quiet_after, record_keep=None, limit=None)
    controller._heard_book = HeardBook()
    calls = []
    controller.round_wakes = lambda node: calls.append(node) or True
    pending = SimpleNamespace(
        first_sent=0.0,
        last_round=0.0,
        important=False,
        confirming=True,
        original=SimpleNamespace(target_id=1),
    )
    controller._next_round(pending, now)
    assert calls == [1] * expected_calls


@pytest.mark.parametrize(
    ("limit", "now", "expected"),
    [
        (130.0, 100.0, 30.0),
        (130.0, 130.0, 0.0),
        (130.0, 200.0, 0.0),
        (None, 100.0, None),
    ],
)
def test_i20_limit_in_is_clamped_at_zero(limit, now, expected):
    assert policy().limit_in(limit, now) == expected


def test_confirm_forever_false_never_enters_confirming():
    assert policy(forever=False).enters_confirming(True) is False
    assert policy(forever=True).enters_confirming(True) is True
    assert policy(forever=True, window=None).in_query_window(0.0, 1.0) is False


def test_controller_reexports_the_confirmation_constants():
    assert CONTROLLER_DEFAULT_CONFIRM_WINDOW == DEFAULT_CONFIRM_WINDOW == 90.0
    assert CONTROLLER_DEFAULT_CONFIRM_SCHEDULE == DEFAULT_CONFIRM_SCHEDULE == (120.0, 1800.0, 1800.0)
    assert CONTROLLER_DEFAULT_CONFIRM_QUIET_AFTER == DEFAULT_CONFIRM_QUIET_AFTER == 600.0
    assert CONTROLLER_DEFAULT_CONFIRM_LIMIT == DEFAULT_CONFIRM_LIMIT == 1800.0
    assert CONTROLLER_CONFIRM_NUDGE_GAP == CONFIRM_NUDGE_GAP == 10.0
    assert CONTROLLER_CONFIRM_HEARD_FRESH == CONFIRM_HEARD_FRESH == 600.0
    assert CONTROLLER_RECORD_KEEP_MARGIN == RECORD_KEEP_MARGIN == 0.95


def test_in_query_window_stops_at_the_exact_window_end():
    rule = policy(window=5.0)
    assert rule.in_query_window(100.0, 104.999) is True
    assert rule.in_query_window(100.0, 105.0) is False


def test_last_heard_keeps_a_record_from_exactly_when_the_command_was_sent():
    book = HeardBook()
    book.note(1, 100.0)
    assert book.last_heard(1, 100.0) == 100.0
    assert book.last_heard(1, 100.001) is None


def test_limit_equal_to_window_is_allowed_but_shorter_limit_is_rejected():
    rule = policy(window=10.0, limit=10.0)
    assert rule.limit_at(100.0, important=False, confirming=True) == 110.0
    with pytest.raises(ValueError):
        policy(window=10.0, limit=9.999)


@pytest.mark.parametrize(
    ("fresh", "quiet", "before_until", "expected"),
    [
        (False, False, True, 1220.0),
        (False, True, True, 2000.0),
        (True, False, True, 1220.0),
        (True, True, True, 1220.0),
        (False, False, False, 2000.0),
        (False, True, False, 2000.0),
        (True, False, False, 1220.0),
        (True, True, False, 1220.0),
    ],
)
def test_next_round_picks_fast_for_fresh_or_early_non_quiet_else_slow(fresh, quiet, before_until, expected):
    schedule = (120.0, 1800.0, 900.0)
    rule = policy(schedule=schedule, record_keep=None, limit=None)
    first_sent = 1000.0
    now = first_sent + (100.0 if before_until else 1800.0)
    due = rule.next_round_due(
        first_sent=first_sent,
        last_round=1100.0,
        now=now,
        fresh=fresh,
        quiet=quiet,
        limit=None,
    )
    assert due == expected


def test_slow_interval_is_the_schedules_third_entry():
    assert policy(schedule=(120.0, 1800.0, 900.0)).slow_interval() == 900.0


def test_confirm_policy_imports_no_locking_transport_events_or_controller_modules():
    source = (Path(__file__).resolve().parents[2] / "cairnmesh" / "confirm_policy.py").read_text(encoding="utf-8")
    imported = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported.update(alias.name.split("."))
        elif isinstance(node, ast.ImportFrom):
            if node.module is not None:
                imported.update(node.module.split("."))
            if node.level:
                imported.update(alias.name.split(".")[0] for alias in node.names)
    forbidden = {"threading", "transport", "events", "controller"}
    assert imported.isdisjoint(forbidden), f"forbidden imports: {sorted(imported & forbidden)}"
