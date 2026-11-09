"""The relay behaviour vectors (``protocol_spec.md`` 16.2) stay in step with ``mesh.py``."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cairnmesh.relay_conformance import describe_sealed, generate, replay

PATH = Path(__file__).parents[1] / "vectors" / "lora_relay_behaviour.json"
FILE = json.loads(PATH.read_text(encoding="utf-8"))


def test_file_is_what_the_generator_writes():
    # On purpose changed behaviour: python -m cairnmesh.relay_conformance > tests/vectors/lora_relay_behaviour.json
    assert FILE == json.loads(json.dumps(generate())), "regenerate the relay behaviour vectors and note it in spec 16.2"


@pytest.mark.parametrize("case", FILE["cases"], ids=lambda case: case["name"])
def test_the_file_alone_replays_each_case(case):
    # Only what is in the file goes in, as on the firmware's test bench.
    assert json.loads(json.dumps(replay(case))) == case["expect"]


@pytest.mark.parametrize("case", FILE["cases"], ids=lambda case: case["name"])
def test_listed_fields_match_the_bytes(case):
    for entry in case["steps"] + case["expect"]:
        data = entry.get("rx") or entry.get("tx")
        if data is None or "invalid" in entry:
            continue
        assert json.loads(json.dumps(describe_sealed(bytes.fromhex(data)))) == entry["frame"]


def test_every_rule_group_is_covered():
    sections = {part.strip() for case in FILE["cases"] for part in case["spec"].split(",")}
    assert {"7", "11.2", "11.3", "12.2", "14.3", "15.2"} <= sections
