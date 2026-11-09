"""Tests for the E4g JSON-lines comparator."""

from __future__ import annotations

import json
from pathlib import Path

from tests.tools.e4g_compare import main


def write(path: Path, entries: list[dict]) -> Path:
    """Write test records as E4g JSON lines."""

    path.write_text(
        "".join(f"{json.dumps(entry, sort_keys=True)}\n" for entry in entries),
        encoding="utf-8",
    )
    return path


def test_two_empty_outputs_are_rejected(tmp_path, capsys):
    """Two empty files cannot prove Gateway behaviour is equal."""

    first, second = tmp_path / "first.jsonl", tmp_path / "second.jsonl"
    first.write_text("", encoding="utf-8")
    second.write_text("", encoding="utf-8")

    assert main([str(first), str(second)]) != 0
    assert "no test records in first and second" in capsys.readouterr().err


def test_one_empty_output_is_rejected(tmp_path, capsys):
    """One empty file must fail even when the other has records."""

    first = write(tmp_path / "first.jsonl", [{"test": "one", "gateways": [{}]}])
    second = tmp_path / "second.jsonl"
    second.write_text("", encoding="utf-8")

    assert main([str(first), str(second)]) != 0
    assert "no test records in second" in capsys.readouterr().err


def test_zero_gateway_records_are_rejected(tmp_path, capsys):
    """Non-empty test lines without any Gateway cannot be a valid comparison."""

    first = write(tmp_path / "first.jsonl", [{"test": "one", "gateways": []}])
    second = write(tmp_path / "second.jsonl", [{"test": "one", "gateways": []}])

    assert main([str(first), str(second)]) != 0
    assert "no Gateway records in first and second" in capsys.readouterr().err


def test_identical_outputs_pass_and_print_counts(tmp_path, capsys):
    """Equal non-empty outputs return success with both sides' counts."""

    entries = [{"test": "one", "gateways": [{"history": []}]}]
    first = write(tmp_path / "first.jsonl", entries)
    second = write(tmp_path / "second.jsonl", entries)

    assert main([str(first), str(second)]) == 0
    out = capsys.readouterr().out
    assert "first: 1 tests, 1 Gateways; second: 1 tests, 1 Gateways" in out


def test_different_outputs_fail_and_name_the_test(tmp_path, capsys):
    """A changed Gateway record fails and identifies its pytest node id."""

    first = write(tmp_path / "first.jsonl", [{"test": "pkg::changed", "gateways": [{"history": []}]}])
    second = write(tmp_path / "second.jsonl", [{"test": "pkg::changed", "gateways": [{"history": [[0.0, "open", 1]]}]}])

    assert main([str(first), str(second)]) != 0
    out = capsys.readouterr().out
    assert "changed: pkg::changed" in out
