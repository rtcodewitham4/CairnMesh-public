"""The test topology text: ``tests/testbed/topo.py`` and the node-side script agree on one shared vector file.

``tests/vectors/testbed_topo.json`` is the contract. The bench script
(``firmware/tools/rxfilter_ctl.py``, which sends the same text to the E77 nodes) is checked against
it here without importing anything from ``tests/testbed``, so the script stays free of test code.
The Go parser (``firmware/rxfilter``) is not checked against this file.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "firmware" / "tools"))

import rxfilter_ctl as ctl  # noqa: E402

from tests.testbed import topo  # noqa: E402

CASES = json.loads((ROOT / "tests" / "vectors" / "testbed_topo.json").read_text(encoding="utf-8"))["cases"]
GOOD = [c for c in CASES if "links" in c]
BAD = [c for c in CASES if "error" in c]


def ids(cases):
    return [repr(c["text"])[:30] for c in cases]


@pytest.mark.parametrize("case", GOOD, ids=ids(GOOD))
def test_testbed_parser_matches_the_vector(case):
    links = topo.parse_links(case["text"])
    assert [list(p) for p in links] == case["links"]
    for node, heard in case["hear"].items():
        assert topo.neighbours(links, int(node)) == heard


@pytest.mark.parametrize("case", BAD, ids=ids(BAD))
def test_testbed_parser_rejects_what_the_vector_rejects(case):
    with pytest.raises(topo.TopoError, match=case["error"]):
        topo.parse_links(case["text"])


@pytest.mark.parametrize("case", GOOD, ids=ids(GOOD))
def test_rxfilter_ctl_matches_the_vector(case):
    links = ctl.normalise_links(case["text"])
    assert [list(p) for p in links] == case["links"]
    for node, heard in case["hear"].items():
        assert ctl.neighbours(links, int(node)) == heard


@pytest.mark.parametrize("case", [c for c in BAD if c.get("ctl", True)], ids=ids([c for c in BAD if c.get("ctl", True)]))
def test_rxfilter_ctl_rejects_what_the_vector_rejects(case):
    with pytest.raises(ctl.RxfError, match=case["error"]):
        ctl.normalise_links(case["text"])


def test_canonical_text_round_trips():
    links = topo.parse_links("66-1, 2-1  3-2")
    assert topo.canonical(links) == "1-66,1-2,2-3"
    assert topo.parse_links(topo.canonical(links)) == links
    assert topo.nodes(links) == [1, 2, 3, 66]


def test_full_mesh_helper_is_every_pair():
    text = topo.full_mesh([3, 1, 2])
    assert topo.parse_links(text) == [(1, 2), (1, 3), (2, 3)]
    with pytest.raises(topo.TopoError):
        topo.full_mesh([1])
