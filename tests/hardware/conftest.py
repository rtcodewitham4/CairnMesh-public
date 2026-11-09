"""Fixtures of the real-bench tests: the board, and the results file the numbers go to."""

from __future__ import annotations

import json
from datetime import date

import pytest

from board import FIRMWARE, Board

RESULTS = FIRMWARE / "results" / f"hw-test-{date.today().isoformat()}.json"


@pytest.fixture(scope="module")
def board():
    reason = Board.missing()
    if reason:
        pytest.skip(reason)
    b = Board()
    yield b
    b.flash("gateway")  # leave the board as a gateway


@pytest.fixture(scope="module")
def record():
    found: dict = {}
    yield found
    RESULTS.parent.mkdir(exist_ok=True)
    old = json.loads(RESULTS.read_text()) if RESULTS.exists() else {}
    old.update(found)
    RESULTS.write_text(json.dumps(old, indent=2, ensure_ascii=False))
