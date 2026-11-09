"""State rules for the link-QA books."""

from __future__ import annotations

from collections import deque

import pytest

from cairnmesh.events import HealthChanged, LinkTestFinished
from cairnmesh.frames import LinkQuality
from cairnmesh.gateway import Health, HealthBook, LinkTest, LinkTestBook
from cairnmesh.session_book import CLOSED, OPEN, Session


class Clock:
    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def health_events(seen: list) -> list[tuple]:
    return [
        (type(event).__name__, event.node_id, event.ok, event.reasons)
        for event in seen
        if isinstance(event, HealthChanged)
    ]


def test_interference_waits_for_30_samples_keeps_100_and_needs_the_lower_bound_over_design():
    health = Health(5)
    health.attempts.extend([False] * 29)
    assert health.interference() is None
    health.attempts.append(False)
    assert health.interference() is not None

    for _ in range(130):
        health.attempts.append(True)
    assert len(health.attempts) == 100

    at_design = Health(5)
    at_design.attempts.extend([True] * 49 + [False] * 51)
    loss, low = at_design.interference()
    assert loss == pytest.approx(0.30, abs=0.01)
    assert low < 0.30

    beyond_design = Health(5)
    beyond_design.attempts.extend([True] * 35 + [False] * 65)
    loss, low = beyond_design.interference()
    assert loss > 0.30 and low > 0.30


def test_interference_clears_at_design_loss_and_only_warns_when_the_bound_is_over():
    clock, seen = Clock(1.0), []
    book = HealthBook(clock, seen.append)
    book._warn(5, "interference", "old")
    for _ in range(30):
        book._note_attempts(5, 1, True)
    assert book.health[5].ok

    clock.now = 2.0
    book.health[5].attempts = deque([True] * 50 + [False] * 50, maxlen=100)
    book._warn(5, "interference", "old")
    book._note_attempts(5, 1, True)
    assert book.health[5].ok

    clock.now = 3.0
    book.health[5].attempts = deque([True] * 34 + [False] * 65, maxlen=100)
    book._note_attempts(5, 1, True)
    assert not book.health[5].ok
    assert health_events(seen)[-1][2:] == (
        False,
        ("干扰超出设计：网关这一跳丢包约 41%（99 % 把握 > 30%）",),
    )


def test_interference_between_the_estimate_and_the_bound_neither_warns_nor_clears():
    clock, seen = Clock(1.0), []
    book = HealthBook(clock, seen.append)
    health = book._health(5)
    book._warn(5, "interference", "old")
    seen.clear()
    health.attempts.extend([False] * 59 + [True] * 40)

    book._note_attempts(5, 1, False)
    assert health.interference() == pytest.approx((0.3675444679663241, 0.2815025061555557))
    assert health.reasons == {"interference": "old"}
    assert health.since == 1.0
    assert seen == []


def test_health_since_tracks_ok_edges_and_each_actual_change_publishes_once():
    clock, seen = Clock(1.0), []
    book = HealthBook(clock, seen.append)
    book.health[5] = Health(5, since=0.5)

    book._warn(5, "path", "path bad")
    assert book.health[5].since == 1.0
    clock.now = 2.0
    book._warn(5, "path", "path bad")
    assert book.health[5].since == 1.0

    book._warn(5, "unreachable", "no answer")
    assert book.health[5].since == 1.0
    clock.now = 3.0
    book._clear(5, "path")
    assert book.health[5].since == 1.0
    clock.now = 4.0
    book._clear(5, "unreachable")
    assert book.health[5].since == 4.0
    book._clear(5, "unreachable")

    assert health_events(seen) == [
        ("HealthChanged", 5, False, ("path bad",)),
        ("HealthChanged", 5, False, ("path bad", "no answer")),
        ("HealthChanged", 5, False, ("no answer",)),
        ("HealthChanged", 5, True, ()),
    ]


def make_session(target: int = 9, *, state: str = OPEN, keepalive_id=(7, 1)) -> Session:
    return Session(
        target,
        state=state,
        route=(1,),
        links=(LinkQuality(-100, 0), LinkQuality(-100, 0)),
        keepalive_id=keepalive_id,
        keepalive_deadline=12.0,
        sf=8,
    )


def make_test(rounds: int = 2) -> LinkTest:
    return LinkTest(9, rounds, 0.0, 0.5, 4.0)


def test_one_keepalive_is_pending_and_only_the_named_result_is_recorded():
    clock, seen = Clock(5.0), []
    book = LinkTestBook(clock, seen.append)
    test = book.link_tests[9] = make_test()
    session = make_session()

    assert book._test_round(session, 5.0, 0x42, -7.5, 1.25, 0.1)
    assert test.pending == (7, 1)
    session.keepalive_id = (8, 1)
    assert not book._test_round(session, 5.0, 0x42, -7.5, 1.25, 0.1)
    assert test.pending == (7, 1)
    assert book._test_result(session, 5.5, True) is None
    assert test.results == []

    session.keepalive_id = (7, 1)
    assert book._test_result(session, 5.5, True) is None
    assert test.results == [(5.0, 0.5, 8)]
    assert test.pending is None


@pytest.mark.parametrize(
    ("sessions", "now", "stopped"),
    [
        ({9: make_session()}, 90.1, "90 s 内没有完成一轮（路径打不开或一直断）"),
        ({9: make_session(state=CLOSED)}, 1.0, "会话已关闭"),
    ],
)
def test_patience_or_a_closed_session_finishes_exactly_once_with_its_reason(sessions, now, stopped):
    clock, seen = Clock(now), []
    book = LinkTestBook(clock, seen.append, test_patience=90.0)
    test = book.link_tests[9] = make_test(rounds=20)

    assert list(book._test_tick(sessions, now)) == [(test, False)]
    assert list(book._test_tick(sessions, now)) == []
    assert test.finished == now and test.stopped == stopped
    assert len([event for event in seen if isinstance(event, LinkTestFinished)]) == 1


def test_a_full_run_keeps_the_last_round_and_finishes_a_registered_acceptance():
    clock, seen = Clock(5.0), []
    book = LinkTestBook(clock, seen.append)
    test = book.link_tests[9] = make_test(rounds=2)
    book._accepting[9] = test
    session = make_session()
    session.links = (LinkQuality.measured(-100, -2.0), LinkQuality.measured(-100, 0.5))

    assert book._test_round(session, 5.0, 0x42, -7.5, 1.25, 0.1)
    assert test.path == (0x42, 1, 9)
    assert test.margins == (5.5, 8.0)
    assert test.sf == 8 and test.expected == 1.25
    assert test.worst == pytest.approx(6.9)
    assert test.paths == 1

    assert book._test_result(session, 5.5, True) is None
    assert test.results == [(5.0, 0.5, 8)] and test.next_at == 6.0
    session.keepalive_id = (8, 1)
    session.route = (1, 2)
    session.links = (
        LinkQuality.measured(-100, -2.0),
        LinkQuality.measured(-100, -1.0),
        LinkQuality.measured(-100, 1.0),
    )
    assert book._test_round(session, 5.5, 0x42, -7.5, 1.25, 0.1)

    assert book._test_result(session, 6.0, False) == (test, True)
    assert test.path == (0x42, 1, 2, 9) and test.margins == (5.5, 6.5, 8.5)
    assert test.sf == 8 and test.expected == 1.25
    assert test.worst == pytest.approx(6.4) and test.paths == 2
    assert test.results == [(5.0, 0.5, 8), (5.5, None, 8)] and test.answered == 1
    assert test.next_at == 6.5 and test.finished == 6.0 and test.stopped == ""
    assert 9 not in book._accepting
    assert [(event.node_id, event.test) for event in seen if isinstance(event, LinkTestFinished)] == [(9, test)]


def test_test_due_needs_an_unfinished_test_without_a_pending_round_at_or_after_next_at():
    clock, seen = Clock(1.0), []
    book = LinkTestBook(clock, seen.append)
    session = make_session()

    assert not book._test_due(session, 1.0)
    test = book.link_tests[9] = make_test()
    assert book._test_due(session, 1.0)
    test.pending = (7, 1)
    assert not book._test_due(session, 1.0)
    test.pending = None
    test.next_at = 2.0
    assert not book._test_due(session, 1.999)
    assert book._test_due(session, 2.0)
    test.finished = 3.0
    assert not book._test_due(session, 3.0)


def test_patience_does_not_expire_at_the_boundary():
    clock, seen = Clock(90.0), []
    book = LinkTestBook(clock, seen.append, test_patience=90.0)
    test = book.link_tests[9] = make_test(rounds=20)

    sessions = {9: make_session()}
    assert list(book._test_tick(sessions, 90.0)) == []
    assert test.finished is None
    clock.now = 90.1
    assert list(book._test_tick(sessions, clock.now)) == [(test, False)]
    assert len(seen) == 1
