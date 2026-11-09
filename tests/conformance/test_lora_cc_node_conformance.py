"""The node behaviour vectors (``protocol_spec.md`` 16.3) stay in step with ``node.py`` and the layers it assembles."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cairnmesh.node_conformance import generate, replay
from cairnmesh.relay_conformance import describe_sealed

PATH = Path(__file__).parents[1] / "vectors" / "lora_node_behaviour.json"
FILE = json.loads(PATH.read_text(encoding="utf-8"))


def test_file_is_what_the_generator_writes():
    # On purpose changed behaviour: python -m cairnmesh.node_conformance > tests/vectors/lora_node_behaviour.json
    assert FILE == json.loads(json.dumps(generate())), "regenerate the node behaviour vectors and note it in spec 16.3"


@pytest.mark.parametrize("case", FILE["cases"], ids=lambda case: case["name"])
def test_the_file_alone_replays_each_case(case):
    # Only what is in the file goes in, as on the firmware's test bench.
    assert json.loads(json.dumps(replay(case))) == case["expect"]


@pytest.mark.parametrize("case", FILE["cases"], ids=lambda case: case["name"])
def test_listed_fields_match_the_bytes(case):
    for entry in case["steps"] + case["expect"]:
        data = entry.get("rx") or entry.get("tx")
        if data is None:
            continue
        assert json.loads(json.dumps(describe_sealed(bytes.fromhex(data)))) == entry["frame"]


def test_every_layer_is_covered():
    sections = {part.strip() for case in FILE["cases"] for part in case["spec"].split(",")}
    assert {"8", "12.3", "14.2", "19"} <= sections
