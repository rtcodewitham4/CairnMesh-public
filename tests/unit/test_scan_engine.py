from __future__ import annotations

from dataclasses import dataclass

import pytest

from cairnmesh import gateway
from cairnmesh.scan_engine import (
    JoinStorm,
    Scan,
    interval_due,
    missing_nodes,
    rescan_due,
    round_plan,
    start_due,
)


def _scan(finished_at: float | None = 100.0) -> Scan:
    return Scan(0.0, 10.0, 1.0, 2.0, frozenset(), finished_at=finished_at)


def test_scan_state_is_reexported_from_its_new_module() -> None:
    assert gateway.Scan is Scan
    assert gateway.JoinStorm is JoinStorm


@pytest.mark.parametrize(
    ("scan", "enabled", "has_controller", "expected"),
    [
        (None, True, True, True),
        (None, False, True, False),
        (None, True, False, False),
        (_scan(), True, True, False),
    ],
)
def test_start_scan_predicate(scan, enabled, has_controller, expected) -> None:
    assert start_due(scan, enabled, has_controller) is expected


@pytest.mark.parametrize(
    ("scan", "node_count", "now", "has_sessions", "expected"),
    [
        (_scan(), 2, 130.0, False, True),
        (_scan(), 2, 129.999, False, False),
        (_scan(), 3, 130.0, False, False),
        (_scan(), 2, 130.0, True, False),
        (_scan(finished_at=None), 2, 130.0, False, False),
    ],
)
def test_rescan_predicate(scan, node_count, now, has_sessions, expected) -> None:
    assert rescan_due(
        scan,
        node_count,
        now,
        has_sessions,
        expected_nodes=3,
        rescan_after=30.0,
        rescans_done=0,
        rescans=2,
    ) is expected


def test_rescan_predicate_respects_limits_and_disabled_setting() -> None:
    scan = _scan()
    assert not rescan_due(
        scan, 0, 1000.0, False, expected_nodes=3, rescan_after=30.0, rescans_done=2, rescans=2
    )
    assert not rescan_due(
        scan, 0, 1000.0, False, expected_nodes=3, rescan_after=None, rescans_done=0, rescans=2
    )


@pytest.mark.parametrize(
    ("scan", "now", "has_sessions", "expected"),
    [
        (_scan(), 460.0, False, True),
        (_scan(), 459.999, False, False),
        (_scan(), 460.0, True, False),
        (_scan(finished_at=None), 460.0, False, False),
    ],
)
def test_periodic_scan_predicate(scan, now, has_sessions, expected) -> None:
    assert interval_due(scan, now, has_sessions, 360.0) is expected


def test_periodic_scan_can_be_disabled() -> None:
    assert not interval_due(_scan(), 1000.0, False, None)


def test_join_storm_threshold_window_running_clear_and_none() -> None:
    storm = JoinStorm(3, 15.0)

    def due(now: float, scanning: bool) -> bool:
        triggered = storm.note(now)
        if triggered and not scanning:
            storm.clear()
            return True
        return False

    assert not due(10.0, False)
    assert not due(20.0, False)
    assert not due(25.0, True)
    assert len(storm.joins) == 3
    assert due(26.0, False)
    assert not storm.joins
    assert not due(40.0, False)
    assert not due(41.0, False)
    assert not due(56.0, False)
    assert not due(56.1, False)
    assert not JoinStorm(None, 15.0).note(1.0)


@dataclass
class _NumericModel:
    spread: float
    depths: list[int] | None = None
    scans: list[tuple[int, float, float, float]] | None = None

    def scan_spread(self, hops, factor: float = 2.0) -> float:
        self.depths = list(hops)
        return self.spread

    def scan(self, max_hops: int, window: float, backoff: float, spread: float) -> float:
        if self.scans is not None:
            self.scans.append((max_hops, window, backoff, spread))
        return max_hops + window + backoff + spread


def test_round_plan_uses_depth_four_for_an_empty_book() -> None:
    model = _NumericModel(0.8)
    plan = round_plan([], 0, model, 1.5, 0.25, 3.0, 0.1)

    assert model.depths == [4]
    assert (plan.spread, plan.units) == (1.0, 4)
    assert plan.expected == 17 + 1.5 + 0.25 + 1.0
    assert plan.spreading == 17 + 1.5 + 0.25 + 0.0 + 0.1


def test_round_plan_estimates_a_known_depth_four_node() -> None:
    model = _NumericModel(0.8)
    plan = round_plan([4], 0, model, 1.5, 0.25, 3.0, 0.1)

    assert model.depths == [4]
    assert plan.expected == 4 + 1.5 + 0.25 + 1.0
    assert plan.spreading == 4 + 1.5 + 0.25 + 0.0 + 0.1


@pytest.mark.parametrize(("modelled_spread", "units", "spread"), [(0.0, 1, 0.25), (1000.0, 255, 63.75)])
def test_round_plan_clamps_units(modelled_spread, units, spread) -> None:
    plan = round_plan([4], 0, _NumericModel(modelled_spread), 1.0, 0.2, 3.0, 0.1)

    assert (plan.units, plan.spread) == (units, spread)


def test_round_plan_unknown_nodes_are_four_hops_and_cap_the_scan() -> None:
    model = _NumericModel(0.5, scans=[])
    plan = round_plan([1, 4], 2, model, 1.0, 0.2, 3.0, 0.1)

    assert model.depths == [1, 4, 4, 4]
    assert model.scans == [(4, 1.0, 0.2, plan.spread), (4, 1.0, 0.2, 0.0)]


def test_scan_note_answer_keeps_first_time_and_accumulates_mentions() -> None:
    scan = _scan()
    scan.note_answer(2, 11.0, [3, 4])
    scan.note_answer(2, 12.0, [5])

    assert scan.answered == {2: 11.0}
    assert scan.mentioned == {3, 4, 5}


def test_missing_nodes_excludes_answers_and_away_nodes() -> None:
    scan = Scan(0.0, 0.0, 0.0, 0.0, frozenset({1, 2}))
    scan.mentioned = {3, 4}
    scan.answered[2] = 5.0

    assert missing_nodes(scan, frozenset({3})) == [1, 4]
