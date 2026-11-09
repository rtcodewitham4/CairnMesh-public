"""Direct unit tests for PathView's small presentation helpers."""

from __future__ import annotations

import pytest

from cairnmesh.pathview import OK, STUCK, UNREACHED, HopView, PathView, _fmt

G = 0x42


def view(*hops: HopView, target: int = 3) -> PathView:
    return PathView(target, "open", list(hops), source="test", gateway_id=G)


@pytest.mark.parametrize(
    ("value", "width", "digits", "expected"),
    [
        (None, 6, 0, "     ?"),
        (42, 5, 0, "   42"),
        (-12.5, 8, 2, "  -12.50"),
        (12.5, 2, 2, "12.50"),
    ],
)
def test_fmt_right_aligns_its_text_without_truncating_it(value, width, digits, expected):
    assert _fmt(value, width, digits) == expected


def test_name_shows_the_gateway_as_g_and_other_nodes_as_uppercase_hex():
    path = view()
    assert path.name(G) == "G"
    assert path.name(0x0A) == "0A"
    assert path.name(0xAB) == "AB"


@pytest.mark.parametrize(
    ("route", "expected"),
    [
        ((), "G→03"),
        ((0x0A, 0xAB), "G→0A→AB→03"),
    ],
)
def test_path_text_includes_the_gateway_target_and_every_route_node(route, expected):
    assert view().path_text(route) == expected


def test_stuck_at_returns_the_first_stuck_hop():
    first = HopView(1, G, 1, state=OK)
    stuck = HopView(2, 1, 3, state=STUCK)
    later = HopView(3, 3, 3, state=UNREACHED)
    assert view(first, stuck, later).stuck_at is stuck


def test_stuck_at_is_none_when_no_hop_is_stuck():
    path = view(HopView(1, G, 1, state=OK), HopView(2, 1, 3, state=UNREACHED))
    assert path.stuck_at is None
