"""The v5 test vectors (``tests/vectors/lora_v5.json``) match the code; the frozen v1 to v4 ones are a part of them."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("cryptography")

from cryptography.hazmat.primitives.ciphers.aead import AESCCM

from cairnmesh import vectors
from cairnmesh.frames import ReplyResult, decode_frame, decode_routed, is_routed
from cairnmesh.secure import AppKeys, ReplayWindow, SecureTransport, open_sealed, reseal

FILE = Path(__file__).parents[1] / "vectors" / "lora_v5.json"
#: v1 (2026-10-20), v2 and v3 (2026-10-21), v4 (2026-10-22) stay frozen, never regenerated.
V1 = json.loads((Path(__file__).parents[1] / "vectors" / "lora_v1.json").read_text(encoding="utf-8"))
V2 = json.loads((Path(__file__).parents[1] / "vectors" / "lora_v2.json").read_text(encoding="utf-8"))
V3 = json.loads((Path(__file__).parents[1] / "vectors" / "lora_v3.json").read_text(encoding="utf-8"))
V4 = json.loads((Path(__file__).parents[1] / "vectors" / "lora_v4.json").read_text(encoding="utf-8"))
DATA = json.loads(FILE.read_text(encoding="utf-8"))
NETWORK_KEY = bytes.fromhex(DATA["network_key"])
AEAD = AESCCM(NETWORK_KEY, tag_length=4)
FRAMES = {entry["name"]: entry for entry in DATA["frames"]}


class Inner:
    def set_receiver(self, callback) -> None:
        self.deliver = callback

    def send(self, data: bytes) -> None:
        pass


def receiver() -> tuple[Inner, SecureTransport, list]:
    keys = AppKeys({int(node, 16): bytes.fromhex(by_version["0"]) for node, by_version in DATA["app_keys"].items()})
    inner = Inner()
    transport = SecureTransport(inner, NETWORK_KEY, initial_fcnt=0, app=keys)
    heard: list = []
    transport.set_receiver(heard.append)
    return inner, transport, heard


def test_the_file_is_what_the_code_produces_now():
    assert vectors.build() == DATA, "wire format changed: regenerate with python -m cairnmesh.vectors"


def test_v2_changes_no_v1_byte():
    # v2 = v1 + RESULT and ACCEPTED (user, 2026-10-21): every v1 vector holds as it was.
    assert V2["version"] == "v2" and V1["version"] == "v1"
    assert V2["frames"][: len(V1["frames"])] == V1["frames"]
    assert all(V2[key] == V1[key] for key in V1 if key not in ("frames", "version"))


def test_v3_changes_only_the_report():
    # v3 (user, 2026-10-21, review C1): a REPORT's SUBJECT and EVENT in clear, authenticated.
    assert V3["version"] == "v3"
    changed = [new["name"] for old, new in zip(V2["frames"], V3["frames"]) if old != new]
    assert changed == ["report"] and len(V3["frames"]) == len(V2["frames"])
    assert all(V3[key] == V2[key] for key in V2 if key not in ("frames", "version"))
    below = bytes.fromhex(FRAMES["report"]["below"])
    plain = bytes.fromhex(FRAMES["report"]["plain"])
    assert len(below) == 12 and below[6:8] == plain[5:7]  # what a neighbour reads


def test_v4_adds_only_the_event():
    # v4 (user, 2026-10-22): the EVENT frame and its acknowledgement; no v3 byte changes.
    assert V4["version"] == "v4"
    assert V4["frames"][: len(V3["frames"])] == V3["frames"]
    assert [entry["name"] for entry in V4["frames"][len(V3["frames"]) :]] == ["event", "event_ack"]
    assert all(V4[key] == V3[key] for key in V3 if key not in ("frames", "version"))
    by_name = {entry["name"]: entry for entry in V4["frames"]}
    below = bytes.fromhex(by_name["event"]["below"])
    assert len(below) == 11 and by_name["event"]["app_layer"]  # the event id is sealed: relays cannot read it
    assert not by_name["event_ack"]["app_layer"]  # network-only, unlike a RESULT's acknowledgement


def test_v5_adds_only_the_interrupted_result():
    # v5 (2026-11-08): RESULT code INTERRUPTED = 0x04 in a REPLY and in a RESULT frame;
    # the byte layout and every v4 vector stay as they were.
    assert DATA["version"] == "v5"
    assert DATA["frames"][: len(V4["frames"])] == V4["frames"]
    assert [entry["name"] for entry in DATA["frames"][len(V4["frames"]) :]] == ["reply_interrupted", "result_interrupted"]
    assert all(DATA[key] == V4[key] for key in V4 if key not in ("frames", "version"))
    for name, kind in (("reply_interrupted", 0x02), ("result_interrupted", 0x12)):
        plain = bytes.fromhex(FRAMES[name]["plain"])
        assert len(plain) == 6 and plain[0] == kind and plain[5] == 0x04  # RESULT byte = INTERRUPTED
        assert FRAMES[name]["app_layer"]  # sealed end to end like the ACCEPTED / final results
        assert decode_frame(plain).result == ReplyResult.INTERRUPTED


@pytest.mark.parametrize("entry", DATA["frames"], ids=lambda entry: entry["name"])
def test_each_frame_decodes_and_opens_to_its_layers(entry):
    plain, below, wire = (bytes.fromhex(entry[key]) for key in ("plain", "below", "wire"))
    frame = decode_routed(plain)[0] if is_routed(plain) else decode_frame(plain)
    assert type(frame).__name__ == entry["fields"]["class"]
    assert open_sealed(AEAD, wire) == (below, entry["fcnt"])
    assert (below != plain) == entry["app_layer"]


@pytest.mark.parametrize("name", [name for name, entry in FRAMES.items() if entry["app_layer"]])
def test_the_application_layer_opens_to_the_plain_frame(name):
    inner, _, heard = receiver()
    answered = {
        "reply_to_request": "request",
        "reply_routed": "request_routed",
        "reply_accepted": "request",
        "reply_interrupted": "request",
        "result_ack": "result",
    }.get(name)
    if answered is not None:
        inner.deliver(bytes.fromhex(FRAMES[answered]["wire"]))  # it carries the layer because its REQUEST did
        heard.clear()
    inner.deliver(bytes.fromhex(FRAMES[name]["wire"]))
    assert heard == [bytes.fromhex(FRAMES[name]["plain"])]


def test_a_relay_passing_a_frame_on_changes_only_tx():
    passes = DATA["relay_passes_on"]
    source = FRAMES[passes["from"]]
    assert reseal(AEAD, bytes.fromhex(source["wire"]), passes["tx"]).hex() == passes["wire"]
    assert open_sealed(AEAD, bytes.fromhex(passes["wire"])) == (bytes.fromhex(source["below"]), source["fcnt"])


def test_the_replay_window_steps():
    window = ReplayWindow()
    for step in DATA["replay_window"]["steps"]:
        fresh = window.check(step["fcnt"])
        if fresh:
            window.accept(step["fcnt"])
        assert fresh == step["accepted"], step


def test_a_command_resealed_under_a_fresh_counter_is_refused():
    inner, transport, heard = receiver()
    inner.deliver(bytes.fromhex(DATA["app_replay"]["wire"]))
    assert heard == [] and transport.stats["app_failed"] == 1
