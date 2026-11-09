"""SessionBook on its own: the invariants of the book, with the gateway's callbacks faked.

``Gateway`` end to end is covered by ``test_lora_cc_sessions`` and the
fingerprint; here only what the book promises by itself.
"""

from __future__ import annotations

import pytest

from cairnmesh.frames import LinkQuality
from cairnmesh.gateway import Estimate
from cairnmesh.session_book import (
    CLOSED,
    FAILED,
    OPEN,
    PROBING,
    Delivery,
    Session,
    SessionBook,
    SessionConfig,
    TraceResult,
)


class Fakes:
    """The callbacks ``Gateway`` hands the book, recording what the book asked of them."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.stale_keys: set[tuple[int, int]] = set()
        self.book: SessionBook

    def hang_up(self, session: Session) -> None:
        # The book has already let go of the session when the radio side runs.
        self.calls.append(("hang_up", session.target, session.target in self.book.sessions, session.state))

    def probe(self, session: Session, now: float) -> None:
        self.calls.append(("probe", session.target, now))

    def stale(self, key: tuple[int, int]) -> bool:
        return key in self.stale_keys

    def log(self, event: str, node: int) -> None:
        self.calls.append(("log", event, node))


def make(idle_timeout=60.0, probe_retries=2, reprobe_after=10.0, skip_reprobe=2) -> tuple[SessionBook, Fakes]:
    fakes = Fakes()
    book = SessionBook(
        SessionConfig(
            idle_timeout=idle_timeout,
            probe_retries=probe_retries,
            reprobe_after=reprobe_after,
            skip_reprobe=skip_reprobe,
        ),
        hang_up=fakes.hang_up,
        probe=fakes.probe,
        stale=fakes.stale,
        log=fakes.log,
    )
    fakes.book = book
    return book, fakes


def delivery(target: int, sequence: int) -> Delivery:
    return Delivery(target, sequence, 0, (), 0.0, 1.0, 2.0, 0.5)


# ------------------------------------------------------------------ open / close


def test_open_registers_a_probing_session_and_probes_once():
    book, fakes = make()
    session = book.open(5, 12.0)
    assert book.sessions[5] is session
    assert session.state == PROBING and session.opened_at == session.last_used == 12.0
    assert fakes.calls == [("probe", 5, 12.0)]


def test_open_again_keeps_the_session_and_only_refreshes_last_used():
    book, fakes = make()
    first = book.open(5, 1.0)
    assert book.open(5, 7.0) is first
    assert first.last_used == 7.0 and first.opened_at == 1.0
    assert fakes.calls == [("probe", 5, 1.0)]  # no second probe


def test_one_session_at_a_time_opening_another_closes_the_first():
    book, fakes = make()
    first = book.open(5, 1.0)
    second = book.open(6, 2.0)
    assert list(book.sessions) == [6] and book.sessions[6] is second
    assert first.state == CLOSED
    assert fakes.calls == [
        ("probe", 5, 1.0),
        ("hang_up", 5, False, PROBING),  # out of the book before the radio side runs, state not yet CLOSED
        ("log", "closed", 5),
        ("probe", 6, 2.0),
    ]


def test_close_drops_the_session_and_its_message_in_flight():
    book, fakes = make()
    session = book.open(5, 0.0)
    book.dispatch(5, (5, 1), b"frame")
    fakes.calls.clear()
    book.close(5)
    assert 5 not in book.sessions and 5 not in book.inflight
    assert session.state == CLOSED
    assert fakes.calls == [("hang_up", 5, False, PROBING), ("log", "closed", 5)]


def test_close_of_an_unknown_target_does_nothing():
    book, fakes = make()
    book.close(9)
    assert fakes.calls == []


def test_reopening_a_closed_target_makes_a_fresh_session():
    book, _ = make()
    old = book.open(5, 0.0)
    book.close(5)
    new = book.open(5, 3.0)
    assert new is not old and new.state == PROBING and old.state == CLOSED


def test_the_views_are_read_only():
    book, _ = make()
    book.open(5, 0.0)
    with pytest.raises(TypeError):
        book.sessions[7] = Session(7)  # type: ignore[index]
    with pytest.raises(TypeError):
        book.inflight[5] = ((5, 1), b"x")  # type: ignore[index]
    with pytest.raises(TypeError):
        book.deliveries[(5, 1)] = delivery(5, 1)  # type: ignore[index]


# ------------------------------------------------------------ timers and rounds


def test_idle_is_strictly_longer_than_the_timeout():
    book, _ = make(idle_timeout=60.0)
    session = book.open(5, 100.0)
    assert not book.idle(session, 160.0)
    assert book.idle(session, 160.1)


def test_no_idle_timeout_never_expires():
    book, _ = make(idle_timeout=None)
    session = book.open(5, 0.0)
    assert not book.idle(session, 1e9)


def test_probing_keeps_the_last_retries_plus_one_ids_and_counts_attempts():
    book, _ = make(probe_retries=2)
    session = Session(5)
    for n in range(1, 6):
        book.probing(session, (n, 0), 10.0 + n)
    assert session.probe_ids == [(3, 0), (4, 0), (5, 0)]
    assert session.probe_id == (5, 0) and session.probe_attempts == 5 and session.state == PROBING
    assert session.probe_deadline == 15.0 and session.counts["probes"] == 5


def test_may_probe_again_stops_after_one_plus_retries_probes():
    book, _ = make(probe_retries=2)
    session = Session(5)
    outcomes = []
    for n in range(4):
        book.probing(session, (n, 0), 20.0)
        outcomes.append(book.may_probe_again(session))
    assert outcomes == [True, True, False, False]  # after 1 + probe_retries probes the round has failed


def test_a_detour_probe_is_one_probe_and_one_claimed_detour():
    book, _ = make()
    session = Session(5, suspect=(2, 3))
    assert session.claim_detour()
    book.probing(session, (5, 1), 30.0, detour=True)
    assert session.probe_id == (5, 1) and session.probe_deadline == 30.0
    assert session.counts["probes"] == 1 and session.counts["detours"] == 1
    assert session.detour_tried and not session.claim_detour()


def test_claim_detour_without_a_suspect_is_refused_without_using_the_attempt():
    session = Session(5)
    assert session.suspect is None
    assert not session.claim_detour()
    assert not session.detour_tried  # no suspect means no attempt was spent
    assert not session.claim_detour()


def test_fail_schedules_the_next_probe_reprobe_after_later():
    book, _ = make(reprobe_after=10.0)
    session = Session(5)
    book.fail(session, 100.0)
    assert session.state == FAILED and session.retry_at == 110.0


def test_begin_path_break_records_the_break_and_suspect_hop():
    book, _ = make()
    session = Session(5)
    book.begin_path_break(session, (2, 3))
    assert session.counts["broken"] == 1
    assert session.suspect == (2, 3) and not session.detour_tried


def test_route_error_counts_both_the_error_and_its_hop():
    book, _ = make()
    session = Session(5)
    book.note_route_error(session, (2, 3))
    book.note_route_error(session, (2, 3))
    assert session.counts["route_errors"] == 2 and session.hop_errors[(2, 3)] == 2


def test_first_hop_error_counts_the_hop_without_a_route_error():
    book, _ = make()
    session = Session(5)
    book.note_first_hop_error(session, (1, 2))
    assert session.counts["route_errors"] == 0 and session.hop_errors[(1, 2)] == 1


def test_probe_round_resets_attempts_before_a_retry_and_ids_after_a_break():
    book, _ = make()
    session = Session(5, probe_attempts=4, probe_ids=[(1, 0), (2, 0)], keepalive_id=(3, 0))
    book.reset_probe_attempts(session)
    assert session.probe_attempts == 0 and session.probe_ids == [(1, 0), (2, 0)]
    book.reset_probe_round(session)
    assert session.probe_attempts == 0 and session.probe_ids == []
    assert session.keepalive_id is None


def test_defer_probe_keeps_a_later_date_and_wake_overrides_it():
    book, _ = make()
    session = Session(5, probe_id=(1, 0), probe_not_before=10.0)
    book.defer_probe(session, 8.0)
    assert session.state == PROBING and session.probe_id is None and session.probe_not_before == 10.0
    book.defer_probe(session, 12.0)
    assert session.probe_not_before == 12.0
    book.probe_after_wake(session, 20.0)
    assert session.state == PROBING and session.probe_not_before == 20.0


def test_open_from_probe_is_one_atomic_transition_and_note_latency_updates_the_path_model():
    book, _ = make()
    links = (LinkQuality(-90, 20), LinkQuality(-80, 40))
    session = Session(5, probe_id=(1, 0), probe_ids=[(1, 0)], keepalive_id=(2, 0), first_hop_skips=3)
    book.open_from_probe(session, (2, 3), links, 12.0, 42.0)
    assert session.state == OPEN and session.route == (2, 3) and session.links == links
    assert session.established_at == 12.0 and session.next_keepalive == 42.0
    assert session.counts["established"] == 1
    assert session.probe_ids == [] and session.probe_id is None and session.keepalive_id is None
    assert session.first_hop_skips == 0 and session.suspect is None and not session.detour_tried
    latency = Estimate(1.0, 2.0)
    book.note_latency(session, latency, False)
    assert session.latency is latency and session.within_target is False


def test_fallback_starts_probing_and_only_the_named_answer_clears_it():
    book, _ = make()
    session = Session(5, state=OPEN)
    book.begin_fallback(session, (5, 1), 30.0)
    assert session.state == PROBING and session.probe_id is None
    assert session.fallback == (5, 1) and session.probe_not_before == 30.0
    assert session.counts["fallbacks"] == 1
    assert not book.clear_fallback(session, (5, 2)) and session.fallback == (5, 1)
    assert book.clear_fallback(session, (5, 1)) and session.fallback is None


def test_skipped_out_needs_enough_skips_and_an_idle_path():
    book, _ = make(skip_reprobe=2)
    session = book.open(5, 0.0)
    session.first_hop_skips = 1
    assert not book.skipped_out(session)
    session.first_hop_skips = 2
    assert book.skipped_out(session)
    book.dispatch(5, (5, 1), b"x")  # a command in flight: the path is not idle
    assert not book.skipped_out(session)
    book.take_inflight(5)
    session.waiting = ((5, 2), b"y")
    assert not book.skipped_out(session)


def test_skip_reprobe_none_never_skips_out():
    book, _ = make(skip_reprobe=None)
    session = Session(5, first_hop_skips=99)
    assert not book.skipped_out(session)


# --------------------------------------------------------- messages over a session


def test_release_inflight_only_drops_the_message_it_names():
    book, _ = make()
    book.dispatch(5, (5, 1), b"x")
    book.release_inflight((5, 2))  # an answer to another message
    assert book.inflight[5][0] == (5, 1)
    book.release_inflight((5, 1))
    assert 5 not in book.inflight


def test_take_inflight_returns_and_removes():
    book, _ = make()
    book.dispatch(5, (5, 1), b"x")
    assert book.take_inflight(5) == ((5, 1), b"x")
    assert book.take_inflight(5) is None


def test_hold_and_take_waiting_preserve_the_command_once():
    book, _ = make()
    session = book.open(5, 0.0)
    book.hold_command(session, (5, 2), b"y")
    assert book.take_waiting(session) == ((5, 2), b"y")
    assert session.waiting is None
    assert book.take_waiting(session) is None


def test_record_and_finish_deliveries():
    book, _ = make()
    made = book.record((5, 1), delivery(5, 1))
    assert book.deliveries[(5, 1)] is made
    assert book.finish((5, 1)) is made
    assert book.finish((5, 1)) is None


def test_record_attaches_a_delivery_to_its_open_session():
    book, _ = make()
    session = book.open(5, 0.0)
    made = book.record((5, 1), delivery(5, 1))
    assert list(session.deliveries) == [made]


def test_a_requested_trace_becomes_one_active_exchange_and_finishes_with_its_result():
    book, _ = make()
    session = Session(5, route=(2, 3), last_trace_at=7.0)
    first = delivery(5, 1)
    book.request_trace(session, first, bypass_interval=True)
    assert session.trace_wanted is first
    assert session.last_trace_at == session.last_finished_at == float("-inf")
    assert book.begin_trace(session, (5, 9), 42.0, 20.0, 30.0) is first
    assert session.trace_wanted is None and session.trace_id == (5, 9) and session.trace_of is first
    assert session.trace_deadline == 42.0 and session.last_trace_at == 20.0
    assert session.next_keepalive == 50.0
    result = TraceResult(session.route, {}, 21.0)
    book.finish_trace(session, result, 30.0)
    assert first.trace is result
    assert session.trace_id is None and session.trace_of is None
    assert session.next_keepalive == 51.0


def test_a_trace_request_is_cancelled_only_when_its_command_took_another_path():
    book, _ = make()
    session = Session(5, route=(2, 3), last_trace_at=7.0)
    assert not book.cancel_trace_request_if_path_changed(session)  # nothing requested
    wanted = delivery(5, 1)
    wanted.route = (2, 3)
    book.request_trace(session, wanted)
    assert session.last_trace_at == 7.0  # an automatic request still honours the rate limit
    assert not book.cancel_trace_request_if_path_changed(session)
    assert session.trace_wanted is wanted
    session.route = (2, 4)
    assert book.cancel_trace_request_if_path_changed(session)
    assert session.trace_wanted is None


def test_trace_timeout_and_path_break_end_the_exchange_with_their_own_diagnostic():
    book, _ = make()
    session = Session(5, route=(2, 3))
    expired, interrupted = delivery(5, 1), delivery(5, 2)
    book.request_trace(session, expired)
    book.begin_trace(session, (5, 9), 30.0, 20.0, 10.0)
    book.expire_trace(session, 30.0)
    assert session.trace_id is None and session.trace_of is None and expired.trace is None
    assert expired.events[-1] == (30.0, "追踪无回应", None)
    book.request_trace(session, interrupted)
    book.begin_trace(session, (5, 10), 40.0, 31.0, 10.0)
    book.interrupt_trace(session, 32.0)
    assert session.trace_id is None and session.trace_of is None and interrupted.trace is None
    assert interrupted.events[-1] == (32.0, "追踪中断（路径断了）", None)


def test_finish_command_records_when_it_finished_and_optionally_requests_a_trace():
    book, _ = make()
    session = Session(5)
    finished = delivery(5, 1)
    book.finish_command(session, finished, 20.0, auto_trace=False)
    assert session.last_finished_at == 20.0 and session.trace_wanted is None
    book.finish_command(session, finished, 21.0, auto_trace=True)
    assert session.last_finished_at == 21.0 and session.trace_wanted is finished


def test_forget_waiting_only_clears_the_command_it_names():
    book, _ = make()
    session = book.open(5, 0.0)
    session.waiting = ((5, 1), b"x")
    book.forget_waiting((5, 2))
    assert session.waiting is not None
    book.forget_waiting((5, 1))
    assert session.waiting is None
    book.forget_waiting((8, 1))  # no session there: nothing to do


def test_settle_drops_only_what_the_controller_no_longer_awaits_of_that_node():
    book, fakes = make()
    book.record((5, 1), delivery(5, 1))  # stale
    book.record((5, 2), delivery(5, 2))  # still awaited
    book.record((6, 1), delivery(6, 1))  # stale, but another node
    book.dispatch(5, (5, 1), b"x")  # stale
    fakes.stale_keys = {(5, 1), (6, 1)}
    book.settle(5)
    assert set(book.deliveries) == {(5, 2), (6, 1)}
    assert 5 not in book.inflight


def test_settle_keeps_a_message_in_flight_the_controller_still_awaits():
    book, _ = make()
    book.dispatch(5, (5, 2), b"x")
    book.settle(5)
    assert book.inflight[5][0] == (5, 2)


# ------------------------------------------------------------------ radio views


def test_request_radio_change_requires_a_real_difference_and_a_free_radio():
    book, _ = make()
    session = Session(5, radio=0, sf=7, next_keepalive=100.0)
    assert not book.request_radio_change(session, None, 7, 20.0)
    assert session.radio_wanted is None and session.sf_wanted is None
    assert session.next_keepalive == 100.0
    assert book.request_radio_change(session, 1, 9, 20.0)
    assert (session.radio_wanted, session.sf_wanted) == (1, 9)
    assert session.next_keepalive == 20.0


def test_request_radio_change_does_not_want_the_radio_the_path_already_uses():
    # The one caller only comes here with session.radio == 0, so this equality
    # is unreachable there; the check still guards that premise on its own.
    book, _ = make()
    session = Session(5, radio=1, sf=7)
    assert not book.request_radio_change(session, 1, 7, 20.0)
    assert session.radio_wanted is None and session.sf_wanted is None


def test_return_to_common_abandons_another_destination_and_only_asks_real_returns():
    book, _ = make()
    waiting_on_common = Session(5, radio=0, sf=9, radio_wanted=1, next_keepalive=100.0)
    assert book.return_to_common(waiting_on_common, 7, 20.0)
    assert waiting_on_common.radio_wanted is None  # do not arm an unnecessary switch to radio 0
    assert waiting_on_common.sf_wanted == 7
    assert waiting_on_common.next_keepalive == 20.0
    away = Session(6, radio=1, sf=9, radio_wanted=2, sf_wanted=12, next_keepalive=100.0)
    assert book.return_to_common(away, 7, 20.0)
    assert (away.radio_wanted, away.sf_wanted) == (0, 7)
    assert away.next_keepalive == 20.0


def test_return_to_common_asks_no_sf_move_when_the_path_is_already_there():
    book, _ = make()
    away = Session(5, radio=1, sf=7)
    assert book.return_to_common(away, 7, 20.0)  # the radio move alone is a real return
    assert away.radio_wanted == 0 and away.sf_wanted is None
    common = Session(6, radio=0, sf=7)
    assert not book.return_to_common(common, 7, 20.0)  # nothing to ask for: no needless keepalive move
    assert common.radio_wanted is None and common.sf_wanted is None


def test_keepalive_target_records_only_a_move_that_is_still_in_flight():
    book, _ = make()
    moving = Session(5, radio=0, sf=7, radio_wanted=1, sf_wanted=9)
    assert book.keepalive_target(moving) == (1, 9)
    assert moving.asked == (1, 9)
    steady = Session(6, radio=1, sf=9, radio_wanted=1, sf_wanted=9)
    assert book.keepalive_target(steady) == (1, 9)
    assert steady.asked is None  # already there: the in-flight keepalive asks for no move


def test_switch_radio_completes_asked_move_but_keeps_a_newer_destination_pending():
    book, _ = make()
    session = Session(
        5, radio=0, sf=7, radio_wanted=2, sf_wanted=9, asked=(0, 9)
    )
    assert book.switch_radio(session, 0, 9, 50.0)  # answer to the SF-only move
    assert session.asked is None and (session.radio, session.sf) == (0, 9)
    assert session.sf_wanted is None and session.radio_wanted == 2
    assert session.next_keepalive == 50.0  # the newer destination asks again now
    assert session.radio_since == float("-inf")  # common radio is not a new away period
    assert session.counts["sf_switches"] == 1 and session.counts["radio_switches"] == 0
    assert not book.switch_radio(session, 2, 9, 60.0)  # that completed the newer destination too
    assert session.radio_wanted is None and session.radio_since == 60.0
    assert session.next_keepalive == 50.0
    assert session.counts["radio_switches"] == 1


def test_keepalive_exchange_tracks_its_answer_and_follow_up_move():
    book, _ = make()
    asked = Session(5, radio=0, sf=7, radio_wanted=1, sf_wanted=9, asked=(1, 9))
    book.begin_keepalive(asked, (5, 1), 30.0)
    assert asked.keepalive_id == (5, 1) and asked.keepalive_deadline == 30.0
    assert asked.counts["keepalives"] == 1
    assert book.keepalive_answered(asked, 20.0) == (1, 9)
    assert asked.keepalive_id is None

    follow_up = Session(5, radio=0, sf=7, radio_wanted=1, next_keepalive=100.0)
    book.begin_keepalive(follow_up, (5, 2), 40.0)
    assert book.keepalive_answered(follow_up, 25.0) is None
    assert follow_up.next_keepalive == 25.0

    lost = Session(5, asked=(1, 9))
    book.begin_keepalive(lost, (5, 3), 50.0)
    assert book.follow_lost_keepalive(lost) == (1, 9)
    assert lost.keepalive_id is None and lost.counts["switch_follows"] == 1


def test_keepalive_miss_is_counted_as_a_diagnostic():
    book, _ = make()
    session = Session(5)
    book.note_keepalive_missed(session)
    book.note_keepalive_missed(session)
    assert session.counts["keepalive_misses"] == 2


def test_radio_strike_is_counted_as_a_diagnostic():
    book, _ = make()
    session = Session(5)
    book.note_radio_strike(session)
    book.note_radio_strike(session)
    assert session.counts["radio_strikes"] == 2


def test_keepalive_answered_moves_a_new_keepalive_now_for_a_wanted_sf_alone():
    book, _ = make()
    session = Session(5, radio=0, sf=7, sf_wanted=9, next_keepalive=100.0)
    book.begin_keepalive(session, (5, 1), 30.0)
    assert book.keepalive_answered(session, 20.0) is None
    assert session.sf_wanted == 9
    assert session.next_keepalive == 20.0  # the newer SF request asks again now


def test_keepalive_answered_after_a_move_asked_leaves_a_later_next_keepalive():
    book, _ = make()
    session = Session(5, radio=0, sf=7, asked=(1, 9), next_keepalive=100.0)
    book.begin_keepalive(session, (5, 1), 30.0)
    assert book.keepalive_answered(session, 20.0) == (1, 9)
    assert session.keepalive_id is None
    assert session.next_keepalive == 100.0  # no newer request: the answer does not move it


def test_command_dispatch_starts_delivery_clock_and_defers_keepalive():
    book, _ = make()
    session = Session(5, next_keepalive=100.0)
    book.dispatch_command(session, (5, 1), b"frame", 20.0, 30.0)
    assert book.inflight[5] == ((5, 1), b"frame")
    assert session.sent_at == 20.0 and session.next_keepalive == 50.0
    assert session.counts["routed"] == 1


def test_note_first_hop_rssi_ignores_a_missing_measurement():
    session = Session(5, route=(2, 3))
    session.note_first_hop_rssi(2, None, 20.0)
    assert session.first_hop_rssi is None


def test_note_first_hop_rssi_falls_back_to_the_target_when_the_route_is_empty():
    session = Session(5)
    assert session.route == ()
    session.note_first_hop_rssi(5, -70.0, 20.0)
    assert session.first_hop_rssi == (-70.0, 20.0)


def test_path_diagnostics_keep_only_first_hop_measurement_and_one_detour():
    book, _ = make()
    session = Session(5, route=(2, 3))
    session.note_path_broken((2, 3))
    assert session.suspect == (2, 3) and session.claim_detour()
    session.note_first_hop_rssi(4, -80.0, 20.0)
    assert session.first_hop_rssi is None  # not the gateway's first hop
    session.note_first_hop_rssi(2, -81.0, 21.0)
    assert session.first_hop_rssi == (-81.0, 21.0)
    session.note_path_opened()
    assert session.suspect is None and not session.detour_tried


def test_note_path_traffic_schedules_the_next_keepalive_from_the_given_time():
    book, _ = make()
    session = Session(5, next_keepalive=5.0)
    book.note_path_traffic(session, 20.0, 30.0)
    assert session.next_keepalive == 50.0  # now + interval, not 0 + interval


def test_request_radio_change_brings_a_later_next_keepalive_forward_but_keeps_an_earlier_one():
    book, _ = make()
    later = Session(5, radio=0, sf=7, next_keepalive=100.0)
    assert book.request_radio_change(later, 1, 9, 20.0)
    assert later.next_keepalive == 20.0
    earlier = Session(6, radio=0, sf=7, next_keepalive=10.0)
    assert book.request_radio_change(earlier, 1, 9, 20.0)
    assert earlier.next_keepalive == 10.0  # min(10, 20): a real ask never postpones


def test_return_to_common_brings_a_later_next_keepalive_forward_but_keeps_an_earlier_one():
    book, _ = make()
    later = Session(5, radio=1, sf=9, next_keepalive=100.0)
    assert book.return_to_common(later, 7, 20.0)
    assert later.next_keepalive == 20.0
    earlier = Session(6, radio=1, sf=9, next_keepalive=10.0)
    assert book.return_to_common(earlier, 7, 20.0)
    assert earlier.next_keepalive == 10.0  # min(10, 20): a real return never postpones


def test_return_to_common_with_no_real_return_leaves_the_next_keepalive_alone():
    book, _ = make()
    common = Session(5, radio=0, sf=7, next_keepalive=100.0)
    assert not book.return_to_common(common, 7, 20.0)
    assert common.radio_wanted is None and common.sf_wanted is None
    assert common.next_keepalive == 100.0


def test_switch_radio_from_the_session_radio_back_to_common_keeps_radio_since():
    # radio_since marks when the session moved onto its own radio;
    # coming home 1 -> 0 must not start a fresh away period there.
    book, _ = make()
    session = Session(5, radio=1, sf=9, radio_since=20.0)
    assert not book.switch_radio(session, 0, 9, 60.0)
    assert (session.radio, session.sf) == (0, 9)
    assert session.radio_since == 20.0
    assert session.counts["radio_switches"] == 1 and session.counts["sf_switches"] == 0


def test_leave_radio_drops_asked_and_wanted_moves_without_faking_radio_since():
    book, _ = make()
    session = Session(
        5,
        radio=1,
        sf=9,
        radio_wanted=2,
        sf_wanted=12,
        asked=(1, 9),
        radio_since=20.0,
    )
    book.leave_radio(session, 7)
    assert (session.radio, session.sf) == (0, 7)
    assert session.radio_wanted is None and session.sf_wanted is None and session.asked is None
    assert session.radio_since == 20.0  # leaving does not claim a fresh session-radio period


def test_away_nodes_are_the_paths_of_open_sessions_off_the_common_radio():
    book, _ = make()
    session = book.open(5, 0.0)
    session.route = (2, 3)
    session.radio = 1
    assert book.away_nodes() == set()  # still probing
    session.state = OPEN
    assert book.away_nodes() == {2, 3, 5}
    session.radio = 0
    assert book.away_nodes() == set()


def test_yield_radio_closes_an_open_session_that_slowed_down_on_radio_0():
    book, fakes = make()
    session = book.open(5, 0.0)
    session.state, session.radio, session.sf = OPEN, 0, 9
    book.yield_radio(7)
    assert 5 not in book.sessions and session.state == CLOSED
    assert ("log", "closed", 5) in fakes.calls


def test_yield_radio_leaves_sessions_at_the_base_sf_or_on_another_radio():
    book, _ = make()
    session = book.open(5, 0.0)
    session.state, session.radio, session.sf = OPEN, 0, 7
    book.yield_radio(7)
    assert 5 in book.sessions
    session.sf, session.radio = 9, 1
    book.yield_radio(7)
    assert 5 in book.sessions


# ------------------------------------------------------------------ Session itself


def test_note_first_hop_counts_skips_on_a_first_copy_only():
    session = Session(5, route=(2, 3))
    session.note_first_hop(3, 1)  # a later relay named on a first copy: the first stayed silent
    assert session.first_hop_skips == 1 and session.counts["first_hop_skips"] == 1
    session.note_first_hop(3, 2)  # after a repeat the name means nothing
    assert session.first_hop_skips == 1
    session.note_first_hop(2, 1)  # the first relay itself: reset
    assert session.first_hop_skips == 0 and session.counts["first_hop_skips"] == 1


def test_off_common_looks_at_the_session_and_at_what_it_asked_for():
    session = Session(5, sf=7)
    assert not session.off_common(7)
    assert Session(5, sf=9).off_common(7)
    assert Session(5, sf=7, radio=1).off_common(7)
    assert Session(5, sf=7, radio_wanted=1).off_common(7)
    assert Session(5, sf=7, sf_wanted=9).off_common(7)
    assert Session(5, sf=7, asked=(1, 7)).off_common(7)
    assert not Session(5, sf=7, asked=(0, 7)).off_common(7)
