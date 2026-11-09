"""Compare two Gateway E4g JSON-lines outputs by test name.

    python tests/tools/e4g_compare.py main.jsonl branch.jsonl

Exit status is 0 only when both files contain the same tests and every
Gateway record is equal.  Empty inputs and inputs with no Gateway records
are errors.  Missing, extra and changed tests are printed.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def load(path: Path) -> dict[str, Any]:
    """Load one E4g output and reject duplicate or malformed test records."""

    records: dict[str, Any] = {}
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
            test = entry["test"]
            gateways = entry["gateways"]
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise ValueError(f"{path}:{number}: malformed E4g line: {exc}") from exc
        if test in records:
            raise ValueError(f"{path}:{number}: duplicate test {test}")
        records[test] = gateways
    return records


def compare(left: dict[str, Any], right: dict[str, Any]) -> list[str]:
    """Return stable, human-readable differences between two outputs."""

    differences = []
    for test in sorted(left.keys() - right.keys()):
        differences.append(f"only in first: {test}")
    for test in sorted(right.keys() - left.keys()):
        differences.append(f"only in second: {test}")
    for test in sorted(left.keys() & right.keys()):
        if left[test] != right[test]:
            differences.append(f"changed: {test}")
    return differences


def counts(records: dict[str, Any]) -> tuple[int, int]:
    """Return the number of tests and Gateway records in one output."""

    return len(records), sum(len(gateways) for gateways in records.values())


def main(argv: list[str] | None = None) -> int:
    """Run the comparison and return a shell exit status."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("first", type=Path)
    parser.add_argument("second", type=Path)
    parser.add_argument("--limit", type=int, default=50, help="maximum differences to print")
    args = parser.parse_args(argv)

    try:
        first = load(args.first)
        second = load(args.second)
    except (OSError, ValueError) as exc:
        print(f"e4g compare: {exc}", file=sys.stderr)
        return 2
    first_counts, second_counts = counts(first), counts(second)
    summary = (
        f"first: {first_counts[0]} tests, {first_counts[1]} Gateways; "
        f"second: {second_counts[0]} tests, {second_counts[1]} Gateways"
    )
    empty = [
        label
        for label, records in (("first", first), ("second", second))
        if not records
    ]
    if empty:
        print(f"e4g compare: no test records in {' and '.join(empty)} ({summary})", file=sys.stderr)
        return 2
    no_gateways = [
        label
        for label, records in (("first", first_counts), ("second", second_counts))
        if records[1] == 0
    ]
    if no_gateways:
        print(f"e4g compare: no Gateway records in {' and '.join(no_gateways)} ({summary})", file=sys.stderr)
        return 2
    differences = compare(first, second)
    if not differences:
        print(f"e4g compare: identical ({summary})")
        return 0
    print(f"e4g compare: different ({summary})")
    for difference in differences[: args.limit]:
        print(difference)
    if len(differences) > args.limit:
        print(f"... {len(differences) - args.limit} more")
    print(f"e4g compare: {len(differences)} difference(s)")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
