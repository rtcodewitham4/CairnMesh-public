"""Golden guard for the Controller state file before its state I/O is extracted.

Regenerate ``golden/controller_state_current_v1.json`` only after approval
a state-file format change.  The fixture is produced by the current Controller
through ``test_current_state_bytes_match_golden``::

    CAIRNMESH_REGEN_CONTROLLER_STATE_GOLDEN=1 \
      python -m pytest \
      tests/unit/test_controller_state_golden.py::test_current_state_bytes_match_golden

The legacy fixture is a hand-maintained, committed read vector and must not be
regenerated from the current writer: it models a file without ``important``,
``queries``, ``late`` or ``closed_ids``.
"""

from __future__ import annotations

import json
import os
import random
import time
from pathlib import Path

import pytest

from cairnmesh.controller import Controller
from cairnmesh.frames import ReplyFrame, ReplyResult, RequestFrame


GOLDEN_DIR = Path(__file__).parent / "golden"
CURRENT_GOLDEN = GOLDEN_DIR / "controller_state_current_v1.json"
LEGACY_GOLDEN = GOLDEN_DIR / "controller_state_legacy_v1.json"
REGEN_ENV = "CAIRNMESH_REGEN_CONTROLLER_STATE_GOLDEN"

WALL = 1_700_000_000.0
START = 2_000.0
EPOCH = 0x5A
SEED = 44


class FakeClock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> float:
        return self.now


class RecordingTransport:
    def __init__(self) -> None:
        self.sent: list[bytes] = []
        self.receiver = None

    def set_receiver(self, callback) -> None:
        self.receiver = callback

    def send(self, data: bytes) -> None:
        self.sent.append(data)


def controller_at(
    state_file: Path,
    *,
    clock: FakeClock | None = None,
    epoch: int | None = None,
) -> Controller:
    """A Controller with one fixed state-file timing profile."""

    return Controller(
        RecordingTransport(),
        epoch=epoch,
        timeout=0.25,
        max_retries=0,
        confirm_window=1.0,
        confirm_schedule=(100.0, 10_000.0, 10_000.0),
        record_keep=None,
        confirm_limit=None,
        state_file=str(state_file),
        clock=clock or FakeClock(),
        rng=random.Random(SEED),
    )


def reply(controller: Controller, frame: RequestFrame, result: ReplyResult) -> None:
    controller.on_frame(
        ReplyFrame(
            sender_id=frame.target_id,
            target_id=controller.sender_id,
            sequence=frame.sequence,
            epoch=frame.epoch,
            result=result,
        ).encode()
    )


def write_state_document(path: Path, document: dict) -> None:
    path.write_text(json.dumps(document), encoding="utf-8")


def normalize_state_seq(data: bytes) -> bytes:
    """Replace only the snapshot counter value; every other byte remains significant.

    ``_state_seq`` counts writes made during this run, not a format property. A
    StateStore extraction may change the number of snapshots, so pinning it
    would create false failures without protecting the state-file contract.
    """

    document = json.loads(data)
    encoded = f'"seq": {document["seq"]}'.encode()
    assert data.count(encoded) == 1
    return data.replace(encoded, b'"seq": 0', 1)


def current_state_bytes(tmp_path: Path, monkeypatch) -> bytes:
    """Drive the fixed scenario that covers every current top-level and entry field."""

    monkeypatch.setattr(time, "time", lambda: WALL)
    clock = FakeClock()
    state = tmp_path / "controller.json"
    controller = controller_at(state, clock=clock, epoch=EPOCH)

    # A finished command gives node 1 one request id and one closed id.
    closed = controller.build_request(action=1, value=7, target_id=1)
    controller.submit(closed, important=False)
    reply(controller, closed, ReplyResult.OK)

    # Keep one important and one ordinary command confirming.
    important = controller.build_request(action=3, value=9, target_id=1)
    ordinary = controller.build_request(action=5, value=17, target_id=3)
    late = controller.build_request(action=2, value=8, target_id=2)
    controller.submit(important, important=True)
    controller.submit(ordinary, important=False)
    controller.submit(late, important=True)

    # Give every command one query id, then enter confirming.
    clock.now = START + 0.25
    controller.tick()
    clock.now = START + 1.25
    controller.tick()

    # End node 2 unconfirmed, take a late result, then acknowledge it.
    assert controller.abandon(2, late.sequence) == 1
    reply(controller, late, ReplyResult.OK)
    assert controller.acknowledge_late(2, late.sequence, late.epoch)

    return state.read_bytes()


@pytest.fixture
def fixed_wall(monkeypatch):
    monkeypatch.setattr(time, "time", lambda: WALL)


def test_current_state_bytes_match_golden(tmp_path, monkeypatch):
    actual = current_state_bytes(tmp_path, monkeypatch)
    if os.environ.get(REGEN_ENV) == "1":
        GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
        CURRENT_GOLDEN.write_bytes(actual)
    assert normalize_state_seq(actual) == normalize_state_seq(CURRENT_GOLDEN.read_bytes())


def test_current_golden_restores_the_persisted_state(tmp_path, fixed_wall):
    document = json.loads(CURRENT_GOLDEN.read_text(encoding="utf-8"))
    state = tmp_path / "controller.json"
    state.write_bytes(CURRENT_GOLDEN.read_bytes())
    clock = FakeClock()

    controller = controller_at(state, clock=clock)

    assert controller.state_warning is None
    assert controller.epoch == (document["epoch"] + 1) & 0xFF
    assert controller._id_ledger.request_ids == {
        int(node): ids for node, ids in document["request_ids"].items()
    }
    assert controller._id_ledger.closed_ids == {
        int(node): set(ids) for node, ids in document["closed_ids"].items()
    }
    assert controller._id_ledger.next_sequence == {
        int(node): sequence for node, sequence in document["next_sequence"].items()
    }
    assert [
        (
            node,
            item.original.sequence,
            item.original.epoch,
            item.original.action,
            item.original.value,
            item.rounds,
            item.passed,
            item.important,
            list(item.query_sequences),
            item.first_sent,
        )
        for node, queue in sorted(controller._queues.items())
        for item in queue
    ] == [
        (
            entry["node"],
            entry["sequence"],
            entry["epoch"],
            entry["action"],
            entry["value"],
            entry["rounds"],
            entry["passed"],
            entry["important"],
            entry["queries"],
            START - (WALL - entry["since"]),
        )
        for entry in document["confirming"]
    ]
    assert controller.late_confirmations() == []
    assert {
        (entry.node, entry.epoch << 8 | entry.sequence): (
            entry.action,
            entry.value,
            entry.first_sent,
            list(entry.queries),
            entry.important,
            entry.result,
            entry.via,
            entry.elapsed,
            entry.acked,
        )
        for node, entries in controller._late.items()
        for entry in entries.values()
    } == {
        (entry["node"], entry["epoch"] << 8 | entry["sequence"]): (
            entry["action"],
            entry["value"],
            START - (WALL - entry["since"]),
            entry["queries"],
            entry["important"],
            entry["result"],
            entry["via"],
            entry["elapsed"],
            entry["acked"],
        )
        for entry in document["late"]
    }


def test_legacy_golden_defaults_missing_fields_and_warns(tmp_path, fixed_wall):
    document = json.loads(LEGACY_GOLDEN.read_text(encoding="utf-8"))
    state = tmp_path / "controller.json"
    state.write_bytes(LEGACY_GOLDEN.read_bytes())

    controller = controller_at(state, clock=FakeClock())

    assert controller.epoch == (document["epoch"] + 1) & 0xFF
    assert controller._id_ledger.request_ids == {
        int(node): ids for node, ids in document["request_ids"].items()
    }
    assert controller._id_ledger.closed_ids == {}
    assert controller._id_ledger.next_sequence == {
        int(node): sequence for node, sequence in document["next_sequence"].items()
    }
    assert [
        (
            node,
            item.original.sequence,
            item.original.epoch,
            item.original.action,
            item.original.value,
            item.rounds,
            item.passed,
            item.important,
            list(item.query_sequences),
            item.first_sent,
        )
        for node, queue in sorted(controller._queues.items())
        for item in queue
    ] == [
        (
            entry["node"],
            entry["sequence"],
            entry["epoch"],
            entry["action"],
            entry["value"],
            entry["rounds"],
            entry["passed"],
            True,
            [],
            START - (WALL - entry["since"]),
        )
        for entry in document["confirming"]
    ]
    assert controller.late_confirmations() == [] and controller._late == {}
    assert controller.state_warning == (
        f"状态文件里有 {len(document['confirming'])} 条命令没有重要性标记（旧版本写的）："
        "按“重要”处理，继续长期确认，不在到点时结束；需要时请操作员放弃"
    )


@pytest.mark.parametrize("damage", ["missing", "wrong_type", "out_of_range"])
def test_damaged_late_entry_is_skipped_and_the_good_entry_survives(tmp_path, fixed_wall, damage):
    document = json.loads(CURRENT_GOLDEN.read_text(encoding="utf-8"))
    good = document["late"][0]
    good["acked"] = False
    if damage == "missing":
        bad = {"node": good["node"]}
    elif damage == "wrong_type":
        bad = {**good, "epoch": "not-an-integer", "sequence": (good["sequence"] + 1) & 0xFF}
    else:
        bad = {**good, "sequence": 256}
    document["late"] = [good, bad]
    state = tmp_path / "controller.json"
    write_state_document(state, document)

    controller = controller_at(state, clock=FakeClock())

    assert controller.state_warning == (
        "状态文件的 late 表有 1 个损坏条目，已跳过"
        "（对应命令的迟到确认与编号保护丢失，保险期限内请留意）"
    )
    assert [entry.sequence for entry in controller.late_confirmations()] == [
        good["sequence"]
    ]


def test_a_late_table_that_is_not_a_list_is_skipped_with_a_warning(tmp_path, fixed_wall):
    document = json.loads(CURRENT_GOLDEN.read_text(encoding="utf-8"))
    document["late"] = {"node": 2}
    state = tmp_path / "controller.json"
    write_state_document(state, document)

    controller = controller_at(state, clock=FakeClock())

    assert controller.state_warning == (
        "状态文件的 late 表有 1 个损坏条目，已跳过"
        "（对应命令的迟到确认与编号保护丢失，保险期限内请留意）"
    )
    assert controller._late == {} and controller.late_confirmations() == []


@pytest.mark.parametrize(
    ("damage", "error", "message"),
    [
        ("missing_node", KeyError, "node"),
        ("sequence_type", ValueError, None),
        ("missing_action", KeyError, "action"),
    ],
)
def test_confirming_entry_damage_known_gap_raises_instead_of_warning(
    tmp_path, fixed_wall, damage, error, message
):
    # Current behaviour, deliberately pinned: only late entries are isolated.
    document = json.loads(CURRENT_GOLDEN.read_text(encoding="utf-8"))
    if damage == "missing_node":
        del document["confirming"][0]["node"]
    elif damage == "sequence_type":
        document["confirming"][0]["sequence"] = "not-an-integer"
    else:
        del document["confirming"][0]["action"]
    state = tmp_path / "controller.json"
    write_state_document(state, document)

    with pytest.raises(error) as raised:
        controller_at(state, clock=FakeClock())
    if message is not None:
        assert message in str(raised.value)


def test_write_restore_write_is_byte_idempotent_with_epoch_held_fixed(tmp_path, monkeypatch):
    # Holding EPOCH isolates serialization/idempotence; a normal restart advances
    # EPOCH by design and is covered by the existing layer-3 tests.
    document = json.loads(CURRENT_GOLDEN.read_text(encoding="utf-8"))
    once = current_state_bytes(tmp_path, monkeypatch)
    assert normalize_state_seq(once) == normalize_state_seq(CURRENT_GOLDEN.read_bytes())

    first_path = tmp_path / "first.json"
    first_path.write_bytes(once)
    first = controller_at(first_path, clock=FakeClock(), epoch=document["epoch"])
    assert first.state_warning is None
    assert normalize_state_seq(first_path.read_bytes()) == normalize_state_seq(once)

    second_path = tmp_path / "second.json"
    second_path.write_bytes(first_path.read_bytes())
    second = controller_at(second_path, clock=FakeClock(), epoch=document["epoch"])
    assert second.state_warning is None
    assert normalize_state_seq(second_path.read_bytes()) == normalize_state_seq(once)
