"""Import boundaries of ``cairnmesh``, checked statically (``ast``, nothing is imported).

The protocol core must not reach into the simulation / tooling modules, and the standalone
package must not depend on CLI/TUI libraries or other top-level project packages.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[2] / "cairnmesh"

CORE = {
    "frames", "secure", "transport", "serial_link", "events", "controller", "device", "gateway", "mesh",
    "routed", "discovery", "liveness", "wake", "durable", "airtime", "energy", "pathview", "node",
}
# modules the core must not import (any *_bench, too; the experiment scripts live in tests/)
TOOLING = {
    "sim", "channel", "mesh_sim", "radio_model", "simpool", "demo", "session_demo", "mesh_demo",
    "liveness_demo", "gate", "vectors", "conformance", "relay_conformance",
}
FORBIDDEN_ANYWHERE = {"rich", "prompt_toolkit"}
THIRD_PARTY_ALLOWED = {"cryptography", "serial"}


def modules() -> list[Path]:
    return sorted(path for path in PACKAGE.rglob("*.py") if "__pycache__" not in path.parts)


def module_name(path: Path) -> str:
    parts = path.relative_to(PACKAGE).with_suffix("").parts
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def imports(path: Path):
    """Yield ``(line, absolute dotted name)`` for every import in the file, nested ones included."""

    package = ["cairnmesh", *module_name(path).split(".")[:-1]] if module_name(path) else ["cairnmesh"]
    if path.name == "__init__.py" and module_name(path):
        package = ["cairnmesh", *module_name(path).split(".")]
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), str(path))):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield node.lineno, alias.name
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package[: len(package) - (node.level - 1)]
                name = ".".join([*base, *([node.module] if node.module else [])])
            else:
                name = node.module or ""
            yield node.lineno, name
            for alias in node.names:  # ``from . import sim`` / ``from cairnmesh import sim``
                yield node.lineno, f"{name}.{alias.name}"


def is_under(name: str, prefix: str) -> bool:
    return name == prefix or name.startswith(prefix + ".")


def cairnmesh_module(name: str) -> str | None:
    """The module path below ``cairnmesh`` an import points at (``''`` for the package itself)."""

    if name == "cairnmesh":
        return ""
    if name.startswith("cairnmesh."):
        return name[len("cairnmesh."):]
    return None


def is_tooling(inner: str) -> bool:
    first = inner.split(".")[0]
    return first in TOOLING or first.endswith("_bench")


def report(problems: list[str], rule: str) -> None:
    assert not problems, rule + ":\n  " + "\n  ".join(problems)


def test_the_core_module_list_matches_the_files():
    present = {module_name(path) for path in modules()}
    assert not (CORE | TOOLING) - present, f"listed module missing from cairnmesh: {sorted((CORE | TOOLING) - present)}"


def test_the_core_does_not_import_simulation_or_tooling():
    problems = []
    for path in modules():
        if module_name(path) not in CORE:
            continue
        for line, name in imports(path):
            inner = cairnmesh_module(name)
            if inner is not None:
                if inner and is_tooling(inner):
                    problems.append(f"{module_name(path)}.py:{line} imports {name} (simulation/tooling)")
    report(problems, "protocol core must stay free of simulation/tooling modules")


def test_no_module_imports_the_tui_or_ui_libraries():
    problems = []
    for path in modules():
        for line, name in imports(path):
            if any(is_under(name, banned) for banned in FORBIDDEN_ANYWHERE):
                problems.append(f"{path.relative_to(PACKAGE)}:{line} imports {name}")
    report(problems, "cairnmesh must not depend on the TUI, the CLI or UI libraries")


def test_third_party_imports_are_limited_to_cryptography_and_pyserial():
    stdlib = set(sys.stdlib_module_names)
    problems = []
    for path in modules():
        for line, name in imports(path):
            top = name.split(".")[0]
            if not name or top == "cairnmesh" or top in stdlib or top in THIRD_PARTY_ALLOWED:
                continue
            problems.append(f"{path.relative_to(PACKAGE)}:{line} imports {name}")
    report(problems, "only stdlib, cryptography and pyserial are allowed in cairnmesh")
