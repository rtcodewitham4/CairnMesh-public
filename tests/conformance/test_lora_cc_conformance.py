"""The device behaviour vectors (``protocol_spec.md`` 16.1) stay in step with ``device.py``."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cairnmesh import frames as F
from cairnmesh.conformance import describe, generate, replay

PATH = Path(__file__).parents[1] / "vectors" / "lora_device_behaviour.json"
FILE = json.loads(PATH.read_text(encoding="utf-8"))


def test_file_is_what_the_generator_writes():
    # On purpose changed behaviour: python -m cairnmesh.conformance > tests/vectors/lora_device_behaviour.json
    assert FILE == json.loads(json.dumps(generate())), "regenerate the behaviour vectors and note it in spec 16.1"


@pytest.mark.parametrize("case", FILE["cases"], ids=lambda case: case["name"])
def test_the_file_alone_replays_each_case(case):
    # Only what is in the file goes in, as on the firmware's test bench.
    assert json.loads(json.dumps(replay(case))) == case["expect"]


@pytest.mark.parametrize("case", FILE["cases"], ids=lambda case: case["name"])
def test_listed_fields_match_the_bytes(case):
    for entry in case["steps"] + case["expect"]:
        data = entry.get("rx") or entry.get("tx")
        if data is None or "frame" not in entry:
            continue
        assert json.loads(json.dumps(describe(F.decode_frame(bytes.fromhex(data))))) == entry["frame"]


# ------------------------------------------------- result known later (4.1)


def _pending_device():
    from cairnmesh.conformance import Midpoint, _Wire
    from cairnmesh.device import PENDING, Device

    clock = {"now": 0.0}
    sent: list = []
    runs: list = []

    class App(Device):
        def perform_action(self, frame):
            runs.append(frame.message_id)
            return PENDING

    device = App(_Wire(lambda data: sent.append(F.decode_frame(data))), 1, awake=True, heartbeat_interval=1e6,
                 action_time=5.0, rng=Midpoint(), clock=lambda: clock["now"])
    return device, clock, sent, runs


def test_complete_takes_only_ok_or_error():
    device, _, _, _ = _pending_device()
    with pytest.raises(ValueError):
        device.complete(F.DEFAULT_CONTROLLER_ID, 10, F.ReplyResult.ACCEPTED)


def test_retry_of_a_pending_action_does_not_run_it_again():
    device, clock, sent, runs = _pending_device()
    request = F.RequestFrame(F.DEFAULT_CONTROLLER_ID, 1, 10, 8, 0, epoch=9)
    device.on_frame(request.encode())
    clock["now"] = 1.0
    device.on_frame(request.encode())  # a retry while it runs
    assert runs == [request.message_id]
    assert [f.result for f in sent] == [F.ReplyResult.ACCEPTED, F.ReplyResult.ACCEPTED]
    assert device.complete(F.DEFAULT_CONTROLLER_ID, request.message_id, F.ReplyResult.OK)
    device.tick(1.0)
    assert isinstance(sent[-1], F.ResultFrame) and sent[-1].result == F.ReplyResult.OK
    # Completing twice: the second is late.
    assert not device.complete(F.DEFAULT_CONTROLLER_ID, request.message_id, F.ReplyResult.ERROR)
    assert device.stats["late_completions"] == 1 and device.stats["deadline_missed"] == 0


def test_deadline_counts():
    device, clock, sent, _ = _pending_device()
    device.on_frame(F.RequestFrame(F.DEFAULT_CONTROLLER_ID, 1, 10, 8, 0, epoch=9).encode())
    clock["now"] = 5.0
    device.tick(5.0)
    assert device.stats["deadline_missed"] == 1
    assert sent[-1].result == F.ReplyResult.ERROR
