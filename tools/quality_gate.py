#!/usr/bin/env python3
"""Ratchet checks for file size and class constructor shape.

The baseline contains the current values above these hard limits:

* ``cairnmesh/*.py``: 1500 lines
* ``__init__``: 15 parameters

An existing offender may stay at its baseline value or improve, but it may
not grow. A new offender has no baseline entry and fails immediately.
"""

from __future__ import annotations

import argparse
import ast
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "tools" / "quality_baseline.json"

MAX_FILE_LINES = 1500
MAX_INIT_PARAMS = 15


@dataclass(frozen=True)
class Finding:
    kind: str
    location: str
    current: int
    limit: int
    baseline: int | None

    def format(self) -> str:
        if self.baseline is None:
            return f"{self.kind}: {self.location} is {self.current}; new limit is {self.limit}"
        return (
            f"{self.kind}: {self.location} grew from baseline {self.baseline} "
            f"to {self.current} (hard limit {self.limit})"
        )


def _read_baseline(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"baseline file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"baseline file is not valid JSON: {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("baseline root must be a JSON object")
    files = data.get("files")
    classes = data.get("classes")
    if not isinstance(files, dict) or not isinstance(classes, dict):
        raise ValueError("baseline must contain object values for 'files' and 'classes'")
    return data


def _metric(
    failures: list[Finding],
    kind: str,
    location: str,
    current: int,
    limit: int,
    baseline: Any,
) -> None:
    if baseline is not None and (not isinstance(baseline, int) or isinstance(baseline, bool)):
        failures.append(Finding(kind, location, current, limit, None))
        return
    if baseline is None:
        if current > limit:
            failures.append(Finding(kind, location, current, limit, None))
        return
    if current > baseline:
        failures.append(Finding(kind, location, current, limit, baseline))


def _class_init_params(tree: ast.Module) -> list[tuple[str, int | None]]:
    """Return ``(class name, __init__ parameter count)``."""

    out: list[tuple[str, int | None]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        init_params = None
        for child in node.body:
            if (
                not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                or child.name != "__init__"
            ):
                continue
            args = child.args
            positional = [*args.posonlyargs, *args.args]
            if positional and positional[0].arg in {"self", "cls"}:
                positional = positional[1:]
            init_params = len(positional) + len(args.kwonlyargs)
            init_params += int(args.vararg is not None) + int(args.kwarg is not None)
            break
        out.append((node.name, init_params))
    return out


def check(root: Path = ROOT, baseline_path: Path | None = None) -> list[Finding]:
    """Return all ratchet violations below ``root``."""

    root = root.resolve()
    baseline_path = baseline_path or root / "tools" / "quality_baseline.json"
    baseline = _read_baseline(baseline_path)
    files = baseline["files"]
    classes = baseline["classes"]
    failures: list[Finding] = []

    for path in sorted((root / "cairnmesh").glob("*.py")):
        relative = path.relative_to(root).as_posix()
        line_count = len(path.read_text(encoding="utf-8").splitlines())
        _metric(
            failures,
            "file lines",
            relative,
            line_count,
            MAX_FILE_LINES,
            files.get(relative),
        )

        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError as exc:
            failures.append(
                Finding(
                    "syntax",
                    f"{relative}:{exc.lineno or 0}",
                    0,
                    0,
                    None,
                )
            )
            continue

        for class_name, init_params in _class_init_params(tree):
            key = f"{relative}::{class_name}"
            values = classes.get(key, {})
            if not isinstance(values, dict):
                current = 0 if init_params is None else init_params
                failures.append(Finding("class baseline", key, current, MAX_INIT_PARAMS, None))
                continue
            if init_params is not None:
                _metric(
                    failures,
                    "__init__ parameters",
                    key,
                    init_params,
                    MAX_INIT_PARAMS,
                    values.get("init_params"),
                )

    return failures


def mutation_audit_missing(root: Path = ROOT) -> list[str]:
    """List package modules omitted from the mutation audit's fixed MODULES."""

    root = root.resolve()
    path = root / "tests" / "audit" / "mutate.py"
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError):
        return []

    configured: list[str] | None = None
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(target, ast.Name) and target.id == "MODULES" for target in node.targets):
            continue
        try:
            value = ast.literal_eval(node.value)
        except (ValueError, TypeError):
            return []
        if isinstance(value, list) and all(isinstance(item, str) for item in value):
            configured = value
        break
    if configured is None:
        return []

    covered = set(configured)
    modules = sorted(path.stem for path in (root / "cairnmesh").glob("*.py"))
    return [module for module in modules if module not in covered]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT, help="repository root")
    parser.add_argument("--baseline", type=Path, help="quality baseline JSON")
    args = parser.parse_args(argv)

    try:
        failures = check(args.root, args.baseline)
    except ValueError as exc:
        print(f"quality gate: FAIL: {exc}")
        return 2

    missing = mutation_audit_missing(args.root)
    if missing:
        print(
            "mutation audit coverage (report only): "
            f"{len(missing)} module(s) missing from MODULES: {', '.join(missing)}"
        )
    else:
        print("mutation audit coverage: all cairnmesh/*.py modules are listed")

    if failures:
        print(f"quality gate: FAIL ({len(failures)} violation(s))")
        for finding in failures:
            print(f"  {finding.format()}")
        return 1

    print("quality gate: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
