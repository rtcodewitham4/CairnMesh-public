"""Pure LinkQA acceptance rules (Q1) and snapshot boundary (Q2)."""

from __future__ import annotations

import pytest

from cairnmesh.frames import LinkQuality
from cairnmesh.linkqa import (
    ACCEPT_DETOUR_DB,
    LINK_DOWN,
    LINK_GOOD,
    LINK_LOSSY,
    LINK_WEAK_SIGNAL,
    Acceptance,
    LinkTest,
    PathView,
    assess,
)
from cairnmesh.session_book import OPEN

TARGET = 50
CONTROLLER = 0


def _path(hops: int) -> tuple[int, ...]:
    return (CONTROLLER, *range(1, hops), TARGET)


def _detour_links(
    path: tuple[int, ...], count: int, detour_db: float = ACCEPT_DETOUR_DB + 1.0
) -> dict[tuple[int, int], float]:
    links = {}
    for index, (a, b) in enumerate(zip(path, path[1:])):
        for k in range(count):
            detour = 100 + index * 10 + k
            links[(min(a, detour), max(a, detour))] = detour_db
            links[(min(b, detour), max(b, detour))] = detour_db
    return links


def _assess(
    hops: int,
    margin: float,
    *,
    detours: int = 0,
    detour_db: float = ACCEPT_DETOUR_DB + 1.0,
    test: LinkTest | None = None,
    has_neighbour_info: bool = True,
    state: str = OPEN,
) -> Acceptance:
    path = _path(hops)
    route = path[1:-1]
    links = tuple(LinkQuality.measured(-100.0, margin) for _ in range(hops))
    return assess(
        PathView(TARGET, state, route, links),
        now=123.5,
        controller_id=CONTROLLER,
        floor=0.0,
        link_map=_detour_links(path, detours, detour_db),
        has_neighbour_info=has_neighbour_info,
        test=test,
    )


def _test(verdict: str) -> LinkTest:
    results = {
        LINK_GOOD: [(0.0, 0.2, 7)],
        LINK_DOWN: [],
        LINK_WEAK_SIGNAL: [(0.0, None, 7), (1.0, 0.2, 7)],
        LINK_LOSSY: [(0.0, None, 7), (1.0, 0.2, 7)],
    }[verdict]
    margins = {
        LINK_GOOD: (10.0,),
        LINK_DOWN: (10.0,),
        LINK_WEAK_SIGNAL: (0.0,),
        LINK_LOSSY: (10.0,),
    }[verdict]
    test = LinkTest(TARGET, rounds=2, started=0.0, gap=0.0, margin_needed=4.0)
    test.path = _path(1)
    test.margins = margins
    test.results = results
    assert test.verdict == verdict
    return test


@pytest.mark.parametrize(
    ("hops", "margin", "detours", "expected"),
    [
        pytest.param(5, 4.0, 0, True, id="five-hops-exact-4db"),
        pytest.param(5, 3.75, 0, False, id="five-hops-just-under-4db"),
        pytest.param(5, 0.0, 1, True, id="five-hops-one-detour"),
        pytest.param(6, 6.0, 0, True, id="six-hops-exact-6db"),
        pytest.param(6, 5.75, 0, False, id="six-hops-just-under-6db"),
        pytest.param(6, 0.0, 1, False, id="six-hops-one-detour-is-not-enough"),
        pytest.param(6, 0.0, 2, True, id="six-hops-two-detours"),
        pytest.param(6, 4.0, 1, False, id="six-hops-short-rule-does-not-apply"),
    ],
)
def test_acceptance_rule_table(hops: int, margin: float, detours: int, expected: bool) -> None:
    result = _assess(hops, margin, detours=detours)

    assert result.at == 123.5
    assert result.path == _path(hops)
    assert result.passed is expected
    assert result.passed is (not result.reasons)
    assert all(hop.margin_db == margin for hop in result.hops)
    assert all(hop.detours == detours for hop in result.hops)
    assert all(hop.ok is expected for hop in result.hops)


@pytest.mark.parametrize(
    ("detour_db", "expected", "expected_detours"),
    [
        pytest.param(3.75, False, 0, id="detour-just-under-4db"),
        pytest.param(4.0, True, 1, id="detour-exactly-4db"),
        pytest.param(4.25, True, 1, id="detour-just-over-4db"),
    ],
)
def test_a_detour_link_needs_at_least_accept_detour_db(
    detour_db: float, expected: bool, expected_detours: int
) -> None:
    result = _assess(5, 0.0, detours=1, detour_db=detour_db)

    assert result.passed is expected
    assert [hop.detours for hop in result.hops] == [expected_detours] * 5


@pytest.mark.parametrize(
    ("verdict", "expected", "reason_fragment", "warning"),
    [
        pytest.param(LINK_DOWN, False, "通信质量测试 0/0（down）", None, id="down"),
        pytest.param(LINK_WEAK_SIGNAL, False, "通信质量测试 1/2（weak_signal）", None, id="weak-signal"),
        pytest.param(LINK_LOSSY, True, None, "现场可能有干扰", id="lossy-warning"),
        pytest.param(LINK_GOOD, True, None, None, id="good"),
    ],
)
def test_link_test_verdict_table(
    verdict: str,
    expected: bool,
    reason_fragment: str | None,
    warning: str | None,
) -> None:
    result = _assess(1, 10.0, test=_test(verdict))

    assert result.passed is expected
    assert result.passed is (not result.reasons)
    if reason_fragment is None:
        assert not result.reasons
    else:
        assert reason_fragment in result.reasons
    if warning is None:
        assert not result.warnings
    else:
        assert warning in result.warnings[0]


def test_missing_neighbour_information_is_a_remedy_only_when_failing() -> None:
    failed = _assess(1, 0.0, has_neighbour_info=False)
    passed = _assess(1, 10.0, has_neighbour_info=False)
    prompt = "节点簿里没有邻居信息：先 scan 再验收，绕行才算得全"

    assert prompt in failed.remedies
    assert prompt not in passed.remedies


def test_assess_reads_snapshots_without_mutating_them_or_requiring_a_clock() -> None:
    path = _path(2)
    route = path[1:-1]
    links = (LinkQuality.measured(-100.0, 3.0), LinkQuality.measured(-100.0, 2.0))
    link_map = {
        (CONTROLLER, 1): 0.0,
        (1, TARGET): 1.0,
        (CONTROLLER, 99): 5.0,
        (99, 1): 5.0,
    }
    before = dict(link_map)

    result = assess(
        PathView(TARGET, OPEN, route, links),
        now=0.0,
        controller_id=CONTROLLER,
        floor=0.0,
        link_map=link_map,
        has_neighbour_info=True,
    )

    assert not result.passed
    assert [hop.margin_db for hop in result.hops] == [3.0, 2.0]
    assert link_map == before
    assert route == (1,)
    assert links == (LinkQuality.measured(-100.0, 3.0), LinkQuality.measured(-100.0, 2.0))


def test_a_closed_snapshot_is_rejected_before_using_path_links() -> None:
    result = _assess(1, 10.0, state="probing")

    assert not result.passed
    assert result.reasons == ("路径没打开",)
    assert result.hops == ()


@pytest.mark.parametrize(
    "links",
    [
        pytest.param((LinkQuality.measured(-100.0, 10.0),), id="too-few-links"),
        pytest.param((LinkQuality.measured(-100.0, 10.0),) * 4, id="too-many-links"),
    ],
)
def test_an_open_snapshot_with_mismatched_links_is_rejected_before_assessing(
    links: tuple[LinkQuality, ...],
) -> None:
    result = assess(
        PathView(TARGET, OPEN, (1, 2), links),
        now=123.5,
        controller_id=CONTROLLER,
        floor=0.0,
        link_map={},
        has_neighbour_info=True,
    )

    assert not result.passed
    assert result.reasons == ("路径没打开",)
    assert result.hops == ()


def test_the_probe_that_opened_the_path_overrides_a_stale_link_map_value() -> None:
    path = (CONTROLLER, 1, 2, TARGET)
    route = path[1:-1]
    links = tuple(LinkQuality.measured(-100.0, margin) for margin in (0.0, 5.0, 5.0))
    link_map = {
        (CONTROLLER, 1): 0.0,
        (1, 2): 0.0,  # stale: the probe that opened the path measured 5.0
        (2, TARGET): 5.0,
        (CONTROLLER, 2): 5.0,  # node 2 is a detour only with the fresh (1, 2) margin
    }

    result = assess(
        PathView(TARGET, OPEN, route, links),
        now=123.5,
        controller_id=CONTROLLER,
        floor=0.0,
        link_map=link_map,
        has_neighbour_info=True,
    )

    assert result.passed is True
    assert [hop.detours for hop in result.hops] == [1, 0, 0]
    assert result.hops[0].ok is True


def test_a_stale_good_link_map_value_cannot_replace_the_fresh_probe() -> None:
    path = (CONTROLLER, 1, 2, TARGET)
    route = path[1:-1]
    links = tuple(LinkQuality.measured(-100.0, margin) for margin in (0.0, 0.0, 5.0))
    link_map = {
        (CONTROLLER, 1): 0.0,
        (1, 2): 5.0,  # stale: the probe that opened the path measured 0.0
        (2, TARGET): 5.0,
        (CONTROLLER, 2): 5.0,  # node 2 would be a detour only from the stale value
        (1, 3): 5.0,
        (2, 3): 5.0,  # keeps hop (1, 2) passing through its own detour
    }

    result = assess(
        PathView(TARGET, OPEN, route, links),
        now=123.5,
        controller_id=CONTROLLER,
        floor=0.0,
        link_map=link_map,
        has_neighbour_info=True,
    )

    assert result.passed is False
    assert [hop.detours for hop in result.hops] == [0, 1, 0]
    assert result.hops[0].ok is False
