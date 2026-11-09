"""Mutation testing of the protocol stack (PIT / mutmut method): how much of the code do the unit tests pin down?

Each mutant changes one token (comparison, arithmetic, boolean, constant) or deletes one
statement in a protocol module; the whole unit suite runs against it (``pytest -x``).
A mutant the suite still passes ("survivor") is behaviour no test checks.

    python tests/audit/mutate.py count
    python tests/audit/mutate.py run OUT.jsonl [--sample N] [--modules a,b]
"""

from __future__ import annotations

import ast
import io
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import time
import tokenize
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PKG = ROOT / "cairnmesh"
MODULES = ["device", "secure", "controller", "confirm_policy", "pending", "outcomes", "frames", "durable", "routed", "session", "mesh", "gateway", "scan_engine", "nodebook", "latency", "linkqa", "session_book", "state_store", "late_ledger", "id_ledger", "node"]

SWAP = {
    "<": "<=", "<=": "<", ">": ">=", ">=": ">", "==": "!=", "!=": "==",
    "+": "-", "-": "+", "//": "*", "<<": ">>", ">>": "<<", "&": "|",
    "+=": "-=", "-=": "+=", "and": "or", "or": "and", "True": "False", "False": "True",
    "is": "is not",
}
SKIP_LINE = ("_log(", "print(", "raise ", "assert ", "log(", "logger", "warnings.warn", "f\"", "__all__")
TIMEOUT = float(os.environ.get("MUT_TIMEOUT", "120"))


def _skip(line: str) -> bool:
    text = line.strip()
    return not text or text.startswith(("#", "@", "def ", "class ", "import ", "from ")) or any(s in text for s in SKIP_LINE)


def mutants(module: str) -> list[dict]:
    path = PKG / f"{module}.py"
    source = path.read_text(encoding="utf-8")
    lines = source.splitlines(keepends=True)
    out = []
    tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    for i, tok in enumerate(tokens):
        (row, col), (erow, ecol) = tok.start, tok.end
        if row != erow or row > len(lines) or _skip(lines[row - 1]):
            continue
        prev = tokens[i - 1].string if i else ""
        nxt = tokens[i + 1] if i + 1 < len(tokens) else None
        if tok.type == tokenize.OP and tok.string in SWAP:
            if tok.string == "-" and prev in ("(", ",", "=", "[", ":", "return"):
                continue  # unary minus: the number itself is mutated
            out.append(_mk(module, lines, row, col, ecol, tok.string, SWAP[tok.string]))
        elif tok.type == tokenize.NAME and tok.string == "is":
            if nxt is not None and nxt.string == "not":
                out.append(_mk(module, lines, row, col, nxt.end[1], "is not", "is"))
            else:
                out.append(_mk(module, lines, row, col, ecol, "is", "is not"))
        elif tok.type == tokenize.NAME and tok.string in SWAP:
            out.append(_mk(module, lines, row, col, ecol, tok.string, SWAP[tok.string]))
        elif tok.type == tokenize.NAME and tok.string == "not" and prev != "is" and nxt is not None and nxt.start[0] == row:
            out.append(_mk(module, lines, row, col, nxt.start[1], "not ", ""))
        elif tok.type == tokenize.NUMBER and tok.string.isdigit():
            out.append(_mk(module, lines, row, col, ecol, tok.string, str(int(tok.string) + 1)))
    # statement deletion inside functions
    tree = ast.parse(source)
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(fn):
            if isinstance(node, (ast.Expr, ast.Assign, ast.AugAssign)) and node.lineno == node.end_lineno:
                if isinstance(node, ast.Expr) and not isinstance(node.value, ast.Call):
                    continue
                line = lines[node.lineno - 1]
                if _skip(line):
                    continue
                out.append(_mk(module, lines, node.lineno, node.col_offset, node.end_col_offset,
                               line[node.col_offset:node.end_col_offset], "pass"))
    good, seen = [], set()
    for m in out:
        key = (m["line"], m["col"], m["new"])
        if key in seen:
            continue
        seen.add(key)
        try:
            compile(apply(source, m), str(path), "exec")
        except SyntaxError:
            continue
        good.append(m)
    for k, m in enumerate(good):
        m["id"] = f"{module}:{k}"
    return good


def _mk(module, lines, row, col, ecol, old, new) -> dict:
    return {"module": module, "line": row, "col": col, "ecol": ecol, "old": old, "new": new,
            "text": lines[row - 1].rstrip()[:160]}


def apply(source: str, m: dict) -> str:
    lines = source.splitlines(keepends=True)
    line = lines[m["line"] - 1]
    lines[m["line"] - 1] = line[: m["col"]] + m["new"] + line[m["ecol"]:]
    return "".join(lines)


# ------------------------------------------------------------------ running

_TREE = None
_ORIG: dict[str, str] = {}


def _fresh_worker():
    global _TREE
    _TREE = None
    _ORIG.clear()
    _init_worker()


def _init_worker():
    global _TREE
    if _TREE is not None:
        return
    _TREE = Path(tempfile.mkdtemp(prefix="mutw-"))
    for part in ("cairnmesh", "tests", "pyproject.toml"):
        src = ROOT / part
        if src.is_dir():
            shutil.copytree(src, _TREE / part, ignore=shutil.ignore_patterns("__pycache__"))
        else:
            shutil.copy(src, _TREE / part)


def _limit():
    import resource

    cap = int(os.environ.get("MUT_MEM", str(2 << 30)))
    resource.setrlimit(resource.RLIMIT_AS, (cap, cap))  # one runaway mutant must not take the machine down


def _pytest() -> tuple[int, float, str]:
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONHASHSEED="0", CAIRNMESH_WORKERS="1")
    start = time.monotonic()
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "-x", "-q", "-p", "no:cacheprovider", "-m", "not slow and not long", "tests/unit", "tests/conformance"],
            cwd=_TREE, env=env, capture_output=True, text=True, timeout=TIMEOUT, preexec_fn=_limit,
        )
    except subprocess.TimeoutExpired:
        return -9, time.monotonic() - start, "TIMEOUT"
    first = next((l for l in proc.stdout.splitlines() if l.startswith(("FAILED", "ERROR"))), "")
    return proc.returncode, time.monotonic() - start, first[:200]


def run_one(m: dict) -> dict:
    _init_worker()
    path = _TREE / "cairnmesh" / f"{m['module']}.py"
    orig = _ORIG.setdefault(m["module"], path.read_text(encoding="utf-8"))
    path.write_text(apply(orig, m), encoding="utf-8")
    try:
        rc, wall, first = _pytest()
    finally:
        path.write_text(orig, encoding="utf-8")
    status = "survived" if rc == 0 else "timeout" if rc == -9 else "killed" if rc in (1, 2) else f"error{rc}"
    return {**m, "status": status, "wall": round(wall, 1), "first": first}


def baseline() -> dict:
    _init_worker()
    rc, wall, first = _pytest()
    return {"rc": rc, "wall": wall, "first": first}


def all_mutants(modules=MODULES) -> list[dict]:
    return [m for mod in modules for m in mutants(mod)]


def summarize(results: list[dict]) -> dict:
    counts: dict[str, dict[str, int]] = {}
    for r in results:
        c = counts.setdefault(r["module"], {})
        c[r["status"]] = c.get(r["status"], 0) + 1
    out = {}
    for mod, c in counts.items():
        total = sum(c.values())
        out[mod] = {"n": total, "score": round((total - c.get("survived", 0)) / total, 4), **c}
    return out


def main(argv):
    if argv[0] == "count":
        total = 0
        for mod in MODULES:
            n = len(mutants(mod))
            total += n
            print(mod, n)
        print("total", total)
        return
    out = Path(argv[1])
    sample = int(argv[argv.index("--sample") + 1]) if "--sample" in argv else None
    mods = argv[argv.index("--modules") + 1].split(",") if "--modules" in argv else MODULES
    ms = all_mutants(mods)
    if sample:
        ms = random.Random(1).sample(ms, min(sample, len(ms)))
    from multiprocessing import get_context

    base = baseline()
    print(f"[mut] baseline rc={base['rc']} wall={base['wall']:.1f}s {base['first']}", flush=True)
    workers = int(os.environ.get("CAIRNMESH_WORKERS", os.cpu_count() or 1))
    results = []
    with get_context("fork").Pool(workers, initializer=_fresh_worker) as pool, out.open("w") as fh:
        start = time.monotonic()
        for k, r in enumerate(pool.imap_unordered(run_one, ms), 1):
            fh.write(json.dumps(r) + "\n")
            fh.flush()
            results.append(r)
            if k % 250 == 0:
                print(f"[mut] {k}/{len(ms)} at {time.monotonic() - start:.0f}s", flush=True)
    (out.parent / "mutation_summary.json").write_text(json.dumps(summarize(results), indent=1))
    for mod, s in summarize(results).items():
        print(f"[mut] {mod:11s} {s}", flush=True)


if __name__ == "__main__":
    main(sys.argv[1:])
