"""The test topology text, e.g. ``66-1,1-2,2-3`` (README.md here is its definition).

Same text, same result as ``normalise_links`` / ``neighbours`` in ``firmware/tools/rxfilter_ctl.py``
(the node side): ``tests/vectors/testbed_topo.json`` holds the cases and both are tested against it.
Python only; the Go parser (firmware/rxfilter) is NOT checked against that file (see README.md).
"""

from __future__ import annotations

import re
from typing import List, Sequence, Tuple

#: What the node's table holds (firmware/rxfilter ``MaxLinks``); more would fit on this side only.
MAX_LINKS = 24


class TopoError(ValueError):
    """The topology text is not one the node would take."""


def parse_links(spec: str) -> List[Tuple[int, int]]:
    """'66-1, 2-1' -> [(1, 66), (1, 2)]: first mention first, each pair low-high, no repeats."""

    out: List[Tuple[int, int]] = []
    for item in re.split(r"[,\s]+", spec.strip()):
        if not item:
            continue
        if ">" in item or "<" in item:
            raise TopoError(f"{item!r}: a link holds both ways; one-way is the explicit asym switch")
        m = re.fullmatch(r"(\d+)-(\d+)", item)
        if not m:
            raise TopoError(f"bad link {item!r} (want A-B)")
        a, b = int(m[1]), int(m[2])
        if not (1 <= a <= 254 and 1 <= b <= 254) or a == b:
            raise TopoError(f"bad link {item!r} (IDs 1..254, A != B)")
        pair = (min(a, b), max(a, b))
        if pair not in out:
            out.append(pair)
    if not out:
        raise TopoError("no links")
    if len(out) > MAX_LINKS:
        raise TopoError(f"{len(out)} links; the node holds {MAX_LINKS}")
    return out


def neighbours(links: Sequence[Tuple[int, int]], node: int) -> List[int]:
    """Who ``node`` hears under a symmetric link list, ascending."""

    return sorted({b if a == node else a for a, b in links if node in (a, b)})


def nodes(links: Sequence[Tuple[int, int]]) -> List[int]:
    """Every node that appears in the links, ascending."""

    return sorted({n for pair in links for n in pair})


def full_mesh(ids: Sequence[int]) -> str:
    """Every pair of ``ids`` linked: the "everyone hears everyone" topology (nothing is dropped among them)."""

    ordered = sorted(set(ids))
    if len(ordered) < 2:
        raise TopoError("a full mesh needs two nodes")
    return ",".join(f"{a}-{b}" for i, a in enumerate(ordered) for b in ordered[i + 1:])


def canonical(links: Sequence[Tuple[int, int]]) -> str:
    """The normalised text (what the node prints back and the TESTFILTER banner shows)."""

    return ",".join(f"{a}-{b}" for a, b in links)
