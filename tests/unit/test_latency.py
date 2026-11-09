"""Latency helpers: the shared link-margin function."""

from __future__ import annotations

import math

import pytest

from cairnmesh.latency import margin_db


@pytest.mark.parametrize(
    ("snr_db", "floor", "expected"),
    [
        (17.5, -7.5, 25.0),
        (10.0, -7.5, 17.5),
        (0.0, 0.0, 0.0),
        (-7.5, -7.5, 0.0),
        (-10.5, -7.5, -3.0),
        (0.1, 0.2, 0.1 - 0.2),
        (math.nextafter(1.0, 2.0), 1.0, math.nextafter(1.0, 2.0) - 1.0),
        (math.inf, -7.5, math.inf),
        (-math.inf, -7.5, -math.inf),
    ],
)
def test_margin_db_is_snr_minus_floor(snr_db, floor, expected):
    assert margin_db(snr_db, floor) == expected


def test_margin_db_keeps_operand_order():
    assert margin_db(-10.5, -7.5) < 0
    assert margin_db(-7.5, -10.5) > 0
