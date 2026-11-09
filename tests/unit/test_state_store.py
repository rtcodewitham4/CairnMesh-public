"""StateStore on its own: no Controller, only the records and the file.

The byte-level contract of the whole file is pinned by
``test_controller_state_golden.py``; these tests pin what the store itself
promises (numbering, newest-only writes, the failure flag, the reading rules).
"""

from __future__ import annotations

import json
import time

import pytest

from cairnmesh import state_store
from cairnmesh.state_store import LateRecord, PersistedRequest, PersistedState, StateStore

WALL = 1_700_000_000.0
NOW = 500.0


@pytest.fixture(autouse=True)
def fixed_wall(monkeypatch):
    monkeypatch.setattr(time, "time", lambda: WALL)


def store_at(path, **limits) -> StateStore:
    return StateStore(str(path), lambda: NOW, kept_ids=limits.get("kept_ids", 256), kept_queries=limits.get("kept_queries", 4))


def late(sequence=9, **fields) -> LateRecord:
    values = dict(node=2, epoch=7, sequence=sequence, action=3, value=4, first_sent=NOW - 10.0, queries=[1, 2], important=True)
    values.update(fields)
    return LateRecord(**values)


def request(**fields) -> PersistedRequest:
    values = dict(
        node=1, epoch=7, sequence=5, action=3, value=9, first_sent=NOW - 20.0, rounds=2, passed=1, important=False, queries=[8, 9]
    )
    values.update(fields)
    return PersistedRequest(**values)


def full_state() -> PersistedState:
    return PersistedState(
        epoch=7,
        request_ids={1: [5, 6], 2: [9]},
        closed_ids={1: {6}, 2: set()},
        next_sequence={1: 7, 2: 10},
        confirming=[request()],
        late=[late(result=1, via="query", elapsed=1.5, acked=True)],
    )


def document(path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


# ------------------------------------------------------------------ writing


def test_snapshot_is_the_version_1_document_in_its_key_order():
    snap = store_at("unused").snapshot(full_state())
    assert list(snap) == ["version", "seq", "epoch", "saved_at", "request_ids", "closed_ids", "next_sequence", "confirming", "late"]
    assert (snap["version"], snap["epoch"], snap["saved_at"]) == (1, 7, WALL)
    assert snap["request_ids"] == {"1": [5, 6], "2": [9]}
    assert snap["closed_ids"] == {"1": [6]}  # a node with nothing settled is left out
    assert snap["next_sequence"] == {"1": 7, "2": 10}
    assert list(snap["confirming"][0]) == [
        "node", "epoch", "sequence", "action", "value", "since", "rounds", "passed", "important", "queries"
    ]
    assert snap["confirming"][0]["since"] == WALL - 20.0  # controller clock turned into wall time
    assert list(snap["late"][0]) == [
        "node", "epoch", "sequence", "action", "value", "since", "queries", "important", "result", "via", "elapsed", "acked"
    ]
    assert snap["late"][0]["since"] == WALL - 10.0


def test_snapshots_are_numbered_in_the_order_taken():
    store = store_at("unused")
    assert [store.snapshot(full_state())["seq"] for _ in range(3)] == [1, 2, 3]


def test_an_older_snapshot_never_overwrites_a_newer_one(tmp_path):
    path = tmp_path / "state.json"
    store = store_at(path)
    older, newer = store.snapshot(full_state()), store.snapshot(PersistedState(epoch=8))
    store.write(newer)
    store.write(older)  # the slower thread finishes last
    assert document(path)["epoch"] == 8


def test_a_failed_write_sets_failed_until_one_succeeds(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    store = store_at(path)
    real = state_store.write_json
    failures = [1]

    def flaky(target, data):
        if failures:
            failures.pop()
            raise OSError("disk full")
        real(target, data)

    monkeypatch.setattr(state_store, "write_json", flaky)
    first = store.snapshot(full_state())
    with pytest.raises(OSError):
        store.write(first)
    assert store.failed and not path.exists()
    store.write(first)  # the same snapshot again: nothing was written, so it goes out
    assert not store.failed and document(path)["seq"] == first["seq"]


def test_a_snapshot_is_written_once(tmp_path, monkeypatch):
    store = store_at(tmp_path / "state.json")
    calls = []
    real = state_store.write_json
    monkeypatch.setattr(state_store, "write_json", lambda path, data: (calls.append(data["seq"]), real(path, data)))
    snap = store.snapshot(full_state())
    store.write(snap)
    store.write(snap)
    assert calls == [snap["seq"]]


def test_skipping_an_old_snapshot_does_not_clear_failed(tmp_path, monkeypatch):
    store = store_at(tmp_path / "state.json")
    older, newer = store.snapshot(full_state()), store.snapshot(full_state())
    store.write(newer)
    store.failed = True  # as left by a later write that raised
    store.write(older)  # not written: a newer one is on disk
    assert store.failed


# ------------------------------------------------------------------ reading


def test_write_then_load_gives_the_state_back(tmp_path):
    path = tmp_path / "state.json"
    store = store_at(path)
    store.write(store.snapshot(full_state()))
    reader = store_at(path)
    loaded = reader.load()
    assert loaded is not None and reader.warning is None
    assert loaded.restored_at == NOW
    assert (loaded.epoch, loaded.request_ids, loaded.next_sequence) == (7, {1: [5, 6], 2: [9]}, {1: 7, 2: 10})
    assert loaded.closed_ids == {1: {6}}
    assert loaded.confirming == [request()]
    assert loaded.late == [late(result=1, via="query", elapsed=1.5, acked=True)]


@pytest.mark.parametrize("content", [None, "not json", json.dumps({"version": 2, "epoch": 1}), json.dumps({"version": 1, "epoch": 256}), json.dumps({"version": 1})])
def test_an_unreadable_file_gives_none_and_a_warning(tmp_path, content):
    path = tmp_path / "state.json"
    if content is not None:
        path.write_text(content, encoding="utf-8")
    store = store_at(path)
    assert store.load() is None
    assert store.warning is not None and str(path) in store.warning


def write_document(path, **changes) -> None:
    store = store_at(path)
    snap = store.snapshot(full_state())
    snap.update(changes)
    path.write_text(json.dumps(snap), encoding="utf-8")


def test_request_ids_and_query_ids_are_cut_to_the_limits(tmp_path):
    path = tmp_path / "state.json"
    write_document(path, request_ids={"1": [1, 2, 3, 4, 5]}, closed_ids={"1": [1, 5, 99]})
    loaded = store_at(path, kept_ids=3).load()
    assert loaded.request_ids == {1: [3, 4, 5]}
    assert loaded.closed_ids == {1: {5}}  # only what is still kept can be settled


def test_a_damaged_late_entry_costs_only_itself(tmp_path):
    path = tmp_path / "state.json"
    good = full_state()
    snap = store_at(path).snapshot(good)
    write_document(path, late=[snap["late"][0], {"node": 2}, {**snap["late"][0], "sequence": 256}])
    store = store_at(path)
    loaded = store.load()
    assert [entry.sequence for entry in loaded.late] == [9]
    assert "2 个损坏条目" in store.warning


def test_a_late_table_that_is_not_a_list_is_skipped(tmp_path):
    path = tmp_path / "state.json"
    write_document(path, late={"node": 2})
    store = store_at(path)
    assert store.load().late == []
    assert "1 个损坏条目" in store.warning


def test_a_file_without_importance_marks_means_important_and_warns(tmp_path):
    path = tmp_path / "state.json"
    snap = store_at(path).snapshot(full_state())
    del snap["confirming"][0]["important"]
    del snap["late"][0]["important"]
    path.write_text(json.dumps(snap), encoding="utf-8")
    store = store_at(path)
    loaded = store.load()
    assert loaded.confirming[0].important is True and loaded.late[0].important is True
    assert "1 条命令没有重要性标记" in store.warning  # only the REQUEST is counted, as before


def test_both_warnings_are_joined_in_the_order_found(tmp_path):
    path = tmp_path / "state.json"
    snap = store_at(path).snapshot(full_state())
    del snap["confirming"][0]["important"]
    snap["late"] = [{"node": 2}]
    path.write_text(json.dumps(snap), encoding="utf-8")
    store = store_at(path)
    store.load()
    assert store.warning.index("late 表") < store.warning.index("；状态文件里有 1 条命令没有重要性标记")


@pytest.mark.parametrize("damage", ["node", "action"])
def test_a_damaged_confirming_entry_still_raises(tmp_path, damage):
    # known gap, pinned as it is: only late entries are isolated.
    path = tmp_path / "state.json"
    snap = store_at(path).snapshot(full_state())
    del snap["confirming"][0][damage]
    path.write_text(json.dumps(snap), encoding="utf-8")
    with pytest.raises(KeyError):
        store_at(path).load()
