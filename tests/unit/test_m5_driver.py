"""Host-only M5 driver: one gateway radio, several virtual stacks, fake air.

These tests prove the parts that do not need a board: topology at each stack, one-port fan-out,
independent TX/FCNT, serialized hand-down, equivalent behaviour with no filtering when every link
is present, the three driver scenarios, and that every run closes its filters.
"""

from __future__ import annotations

import json
import logging
import os
import random
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

pytest.importorskip("cryptography")

from cryptography.hazmat.primitives.ciphers.aead import AESCCM

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from cairnmesh.bridge import GatewayBridge
from cairnmesh.frames import ReplyFrame, RequestFrame
from cairnmesh.mesh import MeshRelay
from cairnmesh.node import radio_stack
from cairnmesh.secure import KEY_LEN, MIC_LEN, open_sealed, seal, transmitter
from tests.testbed import m5_driver as M
from tests.testbed import rxfilter as F
from tests.testbed import topo as T

KEY = bytes(range(KEY_LEN))
AEAD = AESCCM(KEY, tag_length=MIC_LEN)


@pytest.fixture(autouse=True)
def testbed_env(monkeypatch):
    monkeypatch.setenv(F.ENV, "1")
    yield
    F.close_all()


class Tap:
    """A physical radio whose sends stay in the test."""

    def __init__(self) -> None:
        self.sent: list[bytes] = []
        self.receiver = None
        self.index = 0

    def send(self, data: bytes, preamble: int = 8, **kwargs) -> None:
        self.sent.append(bytes(data))

    def set_receiver(self, callback) -> None:
        self.receiver = callback

    def busy_until(self) -> float:
        return 0.0


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def request(sequence: int = 1, fcnt: int = 1, sender: int = 0x42, target: int = 9) -> bytes:
    frame = RequestFrame(sender_id=sender, target_id=target, sequence=sequence, action=3, value=1, epoch=7)
    return seal(AEAD, frame.encode(), fcnt)


def by(raw: bytes, tx: int) -> bytes:
    """The same authenticated frame as ``tx`` transmitted it."""

    plain, fcnt = open_sealed(AEAD, raw)
    return seal(AEAD, plain, fcnt, tx=tx)


def plain_request(sequence: int = 1, sender: int = 1, target: int = 9) -> bytes:
    return RequestFrame(sender_id=sender, target_id=target, sequence=sequence, action=3, value=1, epoch=7).encode()


def settle(nodes, clock: Clock, seconds: float = 5.0) -> None:
    for _ in range(round(seconds / 0.01)):
        clock.now += 0.01
        for node in nodes:
            node.tick(clock.now)


def relay_snapshot(node: MeshRelay) -> dict:
    return {
        "stats": dict(node.stats),
        "windows": {origin: (window._last, window._bits, window._used) for origin, window in node._windows.items()},
        "links": list(node._links.items()),
        "heard_at": dict(node._heard_at),
        "pending": node.pending(),
    }


def test_the_layout_parser_reads_radio_placement_and_refuses_duplicates():
    assert M.parse_layout("66@0, 1:1  2@1") == [
        M.NodePlacement(1, 1),
        M.NodePlacement(2, 1),
        M.NodePlacement(66, 0),
    ]
    for bad in (None, "", "1-2", "1@-1", "255@0", "1@0,1@1"):
        with pytest.raises(ValueError):
            M.parse_layout(bad)


def test_fanout_requires_activation_and_explicit_topo(monkeypatch):
    radio = Tap()
    monkeypatch.delenv(F.ENV, raising=False)
    with pytest.raises(F.FilterError, match=F.ENV):
        M.FanoutFilteredPort(radio, topo="1-2")
    assert radio.receiver is None

    monkeypatch.setenv(F.ENV, "1")
    with pytest.raises(F.FilterError, match="topo"):
        M.FanoutFilteredPort(radio, topo=None)
    assert radio.receiver is None


def test_the_layout_refuses_a_topology_edge_with_both_ends_on_one_radio(monkeypatch):
    monkeypatch.setenv(F.ENV, "1")
    with pytest.raises(ValueError, match="half-duplex"):
        M.M5Driver(
            topo="66-1,1-2",
            layout="66@0,1@1,2@1",
            key=KEY,
            gateway_id=66,
        )
    assert F.active_filters() == []


def test_one_radio_fans_in_to_each_nodes_own_filter_without_crosstalk():
    radio = Tap()
    fanout = M.FanoutFilteredPort(radio, topo="1-3,2-4")
    first = fanout.add_stack(node=1, key=KEY)
    second = fanout.add_stack(node=2, key=KEY)
    got_first: list[tuple[bytes, object]] = []
    got_second: list[tuple[bytes, object]] = []
    first.set_receiver(lambda raw, rssi: got_first.append((raw, rssi)))
    second.set_receiver(lambda raw, rssi: got_second.append((raw, rssi)))

    from_three = by(request(fcnt=10), 3)
    radio.receiver(from_three, -91.0)

    assert got_first == [(from_three, -91.0)] and got_second == []
    assert first.status()["passed"] == 1 and first.status()["dropped"] == 0
    assert second.status()["passed"] == 0 and second.status()["dropped"] == 1
    assert list(second.log)[0].tx == 3

    from_four = by(request(fcnt=11), 4)
    radio.receiver(from_four, -70.0)
    assert got_first == [(from_three, -91.0)] and got_second == [(from_four, -70.0)]


def test_a_dropped_frame_never_moves_a_stack_window_on_a_shared_radio():
    radio, clock = Tap(), Clock()
    fanout = M.FanoutFilteredPort(radio, topo="1-3,2-3")
    first, second = fanout.add_stack(node=1, key=KEY), fanout.add_stack(node=2, key=KEY)
    node_one = MeshRelay(first, KEY, 1, clock=clock, rng=random.Random(1))
    node_two = MeshRelay(second, KEY, 2, clock=clock, rng=random.Random(2))

    raw = by(request(fcnt=20), 4)
    radio.receiver(raw, -80.0)
    settle((node_one, node_two), clock)
    assert node_one._windows == {} and node_two._windows == {}
    assert radio.sent == [] and fanout.drops() == {1: list(first.log), 2: list(second.log)}

    carried = by(raw, 3)
    radio.receiver(carried, -80.0)
    settle((node_one, node_two), clock)
    assert node_one._windows[0x42].last == 20 and node_two._windows[0x42].last == 20
    assert {transmitter(raw) for raw in radio.sent} == {1, 2}


def test_each_virtual_stack_has_its_own_tx_id_and_fcnt():
    radio = Tap()
    fanout = M.FanoutFilteredPort(radio, topo="1-3,2-3")
    first, second = fanout.add_stack(node=1, key=KEY), fanout.add_stack(node=2, key=KEY)
    clock = Clock()
    relay_one, secure_one = radio_stack(first, KEY, 1, forward=True, clock=clock, rng=random.Random(1), initial_fcnt=100)
    relay_two, secure_two = radio_stack(second, KEY, 2, forward=True, clock=clock, rng=random.Random(2), initial_fcnt=200)

    secure_one.send(plain_request(sequence=1, sender=1))
    secure_two.send(plain_request(sequence=1, sender=2))
    secure_one.send(plain_request(sequence=2, sender=1))
    secure_two.send(plain_request(sequence=2, sender=2))

    decoded = [open_sealed(AEAD, raw) for raw in radio.sent]
    assert [transmitter(raw) for raw in radio.sent] == [1, 2, 1, 2]
    assert [plain[1] for plain, _ in decoded] == [1, 2, 1, 2]
    assert [fcnt for _, fcnt in decoded] == [101, 201, 102, 202]
    assert (secure_one.fcnt, secure_two.fcnt) == (102, 202)
    assert relay_one.radio is first and relay_two.radio is second


def test_m5_driver_uses_each_stacks_actual_tx_and_independent_fcnt():
    driver = M.M5Driver(
        topo="66-1,66-2",
        layout="66@0,1@1,2@1",  # 1 and 2 share a radio but are not topology neighbours
        key=KEY,
        gateway_id=66,
        initial_fcnt={66: 0, 1: 100, 2: 200},
    )
    try:
        driver.nodes[1].secure.send(plain_request(sequence=1, sender=1))
        driver.nodes[2].secure.send(plain_request(sequence=1, sender=2))
        driver.nodes[1].secure.send(plain_request(sequence=2, sender=1))
        driver.nodes[2].secure.send(plain_request(sequence=2, sender=2))

        sent = list(driver.medium.transmissions)
        decoded = [open_sealed(AEAD, item.data) for item in sent]
        assert [item.source for item in sent] == [1, 2, 1, 2]
        assert [item.tx for item in sent] == [1, 2, 1, 2]
        assert [transmitter(item.data) for item in sent] == [1, 2, 1, 2]
        assert [plain[1] for plain, _ in decoded] == [1, 2, 1, 2]
        assert [fcnt for _, fcnt in decoded] == [101, 201, 102, 202]
        assert (driver.nodes[1].secure.fcnt, driver.nodes[2].secure.fcnt) == (102, 202)
    finally:
        driver.close()


def test_summary_tx_comes_from_wire_bytes_not_the_node_dictionary_key():
    driver = M.M5Driver(
        topo="66-1,66-2",
        layout="66@0,1@1,2@1",
        key=KEY,
        gateway_id=66,
    )
    try:
        # Deliberately put a frame whose authenticated TX differs from the branch's node.
        # ``tx`` in the summary must show the bytes actually sent, not simply ``"1"`` from the key.
        wrong_tx = seal(AEAD, RequestFrame(1, 9, 1, 3, 1, epoch=7).encode(), 1, tx=2)
        driver.medium.ports[1].send_from(1, wrong_tx)
        summary = driver._snapshot()
        assert summary["nodes"]["1"]["tx"] == 2
        assert summary["nodes"]["1"]["transmitted_tx"] == [2]
        assert summary["nodes"]["1"]["tx_mismatch"] == [2]
        assert summary["transmission_tx"] == [2]
    finally:
        driver.close()


def test_transmissions_from_one_radio_are_serialized():
    class SlowTap(Tap):
        def __init__(self) -> None:
            super().__init__()
            self.active = 0
            self.max_active = 0
            self.lock = threading.Lock()

        def send(self, data: bytes, preamble: int = 8, **kwargs) -> None:
            with self.lock:
                self.active += 1
                self.max_active = max(self.max_active, self.active)
            time.sleep(0.01)
            super().send(data, preamble=preamble, **kwargs)
            with self.lock:
                self.active -= 1

    radio = SlowTap()
    fanout = M.FanoutFilteredPort(radio, topo="1-2")
    filtered = fanout.add_stack(node=1, key=KEY)
    start = threading.Barrier(3)

    def send(data: bytes) -> None:
        start.wait()
        filtered.send(data)

    threads = [threading.Thread(target=send, args=(data,)) for data in (b"a", b"b")]
    for thread in threads:
        thread.start()
    start.wait()
    for thread in threads:
        thread.join(timeout=2)
    assert all(not thread.is_alive() for thread in threads)
    assert radio.max_active == 1 and len(radio.sent) == 2


def test_two_stacks_on_one_real_bridge_radio():
    class Transport:
        def __init__(self) -> None:
            self.receiver = None
            self.sent: list[bytes] = []

        def send(self, data: bytes) -> None:
            self.sent.append(data)

        def set_receiver(self, callback) -> None:
            self.receiver = callback

    transport = Transport()
    bridge = GatewayBridge(transport, radios=1)
    fanout = M.FanoutFilteredPort(bridge.ports[0], topo="5-1,6-1")
    five, six = fanout.add_stack(node=5, key=KEY), fanout.add_stack(node=6, key=KEY)
    got: list[tuple[int, bytes, object]] = []
    five.set_receiver(lambda raw, rssi: got.append((5, raw, rssi)))
    six.set_receiver(lambda raw, rssi: got.append((6, raw, rssi)))
    raw = by(request(fcnt=8), 1)

    def rx(frame: bytes) -> bytes:
        return bytes([0x81, 0, 0, 0, 0]) + frame  # RX, radio 0, RSSI 0, SNR 0

    transport.receiver(rx(raw))
    assert got == [(5, raw, 0.0), (6, raw, 0.0)]
    assert five.index == 0 and six.index == 0
    five.send(b"one")
    six.send(b"two")
    assert len(transport.sent) == 2
    assert [message[1] for message in transport.sent] == [0, 0]


def test_all_links_present_behaves_the_same_as_no_filter():
    """Every filtered stack keeps exactly the state of the same unfiltered relay."""

    clock_filtered, clock_plain = Clock(), Clock()
    shared_radio = Tap()
    # The filter sees other radios. 9 and 0x42 are senders, never stacked here: a node's own
    # transmission is not returned to it by a half-duplex radio, filtered or not.
    ids = [1, 2]
    fanout = M.FanoutFilteredPort(shared_radio, topo=T.full_mesh([*ids, 9, 0x42]))
    filtered_ports = {node: fanout.add_stack(node=node, key=KEY) for node in ids}
    filtered_relays = {
        node: MeshRelay(port, KEY, node, clock=clock_filtered, rng=random.Random(node))
        for node, port in filtered_ports.items()
    }
    direct_radios = {node: Tap() for node in ids}
    direct_relays = {
        node: MeshRelay(radio, KEY, node, clock=clock_plain, rng=random.Random(node))
        for node, radio in direct_radios.items()
    }

    script = [
        (by(request(sequence=1, fcnt=1), 9), -80.0),
        (by(request(sequence=1, fcnt=1), 9), -80.0),
        (by(request(sequence=2, fcnt=2), 0x42), -100.0),
        (seal(AEAD, ReplyFrame(9, 0x42, 2, 0, epoch=7).encode(), 3, tx=9), -91.0),
        (b"\x01\x02\x03", -70.0),
    ]
    for raw, rssi in script:
        shared_radio.receiver(raw, rssi)
        for radio in direct_radios.values():
            radio.receiver(raw, rssi)
        for node in ids:
            filtered_relays[node].tick(clock_filtered.now)
            direct_relays[node].tick(clock_plain.now)
        settle(filtered_relays.values(), clock_filtered, 0.2)
        settle(direct_relays.values(), clock_plain, 0.2)

    assert {node: relay_snapshot(filtered_relays[node]) for node in ids} == {
        node: relay_snapshot(direct_relays[node]) for node in ids
    }
    assert all(port.dropped == 0 for port in filtered_ports.values())


def test_dedup_proves_the_second_send_was_suppressed(monkeypatch):
    monkeypatch.setenv(F.ENV, "1")
    driver = M.M5Driver(
        topo="66-1,1-2,2-3",
        layout="66@0,1@1,2@2,3@3",
        key=KEY,
        gateway_id=66,
        seed=4,
        airtime=0.005,
        tick_step=0.005,
    )
    summary = driver.run(("dedup",), target=3, dedup_seconds=5.0)
    dedup = summary["scenarios"]["dedup"]

    assert dedup["origin_sends"] == 2
    assert dedup["origin_sealed"] == 2
    assert dedup["handled"]["3"] == 1
    assert dedup["dedup_suppressed"]["3"] >= 1


def test_hello_delta_is_measured_only_inside_the_hello_phase(monkeypatch):
    monkeypatch.setenv(F.ENV, "1")
    driver = M.M5Driver(
        topo="66-1,1-2,2-3",
        layout="66@0,1@1,2@2,3@3",
        key=KEY,
        gateway_id=66,
        hello_interval=0.2,
        seed=4,
        airtime=0.005,
        tick_step=0.005,
    )
    summary = driver.run(("hello",), target=3, hello_seconds=1.0)
    hello = summary["scenarios"]["hello"]

    assert set(hello["hello_delta"]) == {"1", "2", "3", "66"}
    assert all(1 <= count <= 6 for count in hello["hello_delta"].values())
    assert {node: summary["nodes"][node]["hello"]["stats"]["hellos"] for node in hello["hello_delta"]} == {
        node: count for node, count in hello["hello_delta"].items()
    }


def test_run_fails_when_close_leaves_a_filter_active(monkeypatch):
    monkeypatch.setenv(F.ENV, "1")
    driver = M.M5Driver(
        topo="66-1,1-2,2-3",
        layout="66@0,1@1,2@2,3@3",
        key=KEY,
        gateway_id=66,
        airtime=0.005,
        tick_step=0.005,
    )
    monkeypatch.setattr(driver, "close", lambda: None)

    with pytest.raises(AssertionError, match="test receive filter active"):
        driver.run(("flood",), target=3, flood_seconds=0.1)


def test_driver_runs_all_three_scenarios_and_collects_a_secret_free_summary(monkeypatch, caplog):
    secret = bytes.fromhex("00112233445566778899aabbccddeeff")
    monkeypatch.setenv(F.ENV, "1")
    caplog.set_level(logging.WARNING, logger="cairnmesh.testbed")
    driver = M.M5Driver(
        topo="66-1,1-2,2-3",
        layout="66@0,1@1,2@2,3@3",
        key=secret,
        gateway_id=66,
        hello_interval=0.2,
        seed=4,
        airtime=0.005,
        tick_step=0.005,
    )
    summary = driver.run(
        ("flood", "hello", "dedup"),
        target=3,
        flood_seconds=5.0,
        hello_seconds=1.0,
        dedup_seconds=5.0,
    )

    scenarios = summary["scenarios"]
    assert scenarios["flood"]["handled"]["3"] == 1
    assert scenarios["dedup"]["origin_sends"] == 2
    assert scenarios["dedup"]["handled"]["3"] == 1
    assert scenarios["dedup"]["dedup_suppressed"]["3"] >= 1
    assert all(count > 0 for count in scenarios["hello"]["hello_delta"].values())
    assert sorted(scenarios["hello"]["neighbours"]["66"]) == ["1"]
    assert sorted(scenarios["hello"]["neighbours"]["2"]) == ["1", "3"]
    assert scenarios["hello"]["neighbour_count"]["66"] == 1
    assert summary["nodes"]["1"]["tx"] == 1
    assert summary["nodes"]["1"]["transmitted_tx"] == [1]
    assert summary["nodes"]["1"]["tx_mismatch"] == []
    assert {1, 2, 3, 66} <= set(summary["transmission_tx"])
    assert summary["nodes"]["1"]["filter"]["dropped"] > 0
    assert summary["drops"]["3"]
    assert summary["physical_transmissions"] > 0
    rendered = json.dumps(summary, sort_keys=True)
    assert secret.hex() not in rendered and repr(secret) not in rendered
    assert secret.hex() not in caplog.text and repr(secret) not in caplog.text
    assert F.active_filters() == []


def test_driver_requires_topo_and_activation(monkeypatch):
    monkeypatch.delenv(F.ENV, raising=False)
    with pytest.raises(F.FilterError, match=F.ENV):
        M.M5Driver(topo="66-1", layout="66@0,1@1", key=KEY, gateway_id=66)
    monkeypatch.setenv(F.ENV, "1")
    with pytest.raises(F.FilterError, match="topo"):
        M.M5Driver(topo=None, layout="66@0,1@1", key=KEY, gateway_id=66)
    with pytest.raises(ValueError, match="cover exactly"):
        M.M5Driver(topo="66-1,1-2", layout="66@0,1@1", key=KEY, gateway_id=66)
    with pytest.raises(ValueError, match="key"):
        M.M5Driver(topo="66-1", layout="66@0,1@1", gateway_id=66)


def test_the_driver_script_is_host_only(monkeypatch):
    monkeypatch.setenv(F.ENV, "1")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "tests.testbed.m5_driver",
            "--topo",
            "66-1,1-2",
            "--layout",
            "66@0,1@1,2@2",
            "--key",
            KEY.hex(),
            "--gateway",
            "66",
            "--scenario",
            "flood",
            "--target",
            "2",
            "--flood-seconds",
            "2",
        ],
        cwd=ROOT,
        env={**os.environ, F.ENV: "1"},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    summary = json.loads(result.stdout)
    assert summary["scenarios"]["flood"]["handled"]["2"] == 1


@pytest.mark.parametrize(
    "bad",
    ["do-not-echo-this-node-key", "66=do-not-echo-this-hex", "999=00112233445566778899aabbccddeeff"],
)
def test_a_bad_node_key_is_not_echoed(monkeypatch, bad):
    monkeypatch.setenv(F.ENV, "1")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "tests.testbed.m5_driver",
            "--topo",
            "66-1",
            "--layout",
            "66@0,1@1",
            "--key",
            KEY.hex(),
            "--node-key",
            bad,
            "--gateway",
            "66",
            "--target",
            "1",
        ],
        cwd=ROOT,
        env={**os.environ, F.ENV: "1"},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode != 0
    assert "--node-key must be NODE=HEX" in result.stderr
    assert bad not in result.stdout and bad not in result.stderr
