"""Restart safety: persisted counters, the retry window cap, session clock."""

from __future__ import annotations

import json
import os
import threading
import time

import pytest

pytest.importorskip("cryptography")

from cairnmesh import secure
from cairnmesh.controller import Controller
from cairnmesh.secure import TX_RESERVE, SecureTransport
from cairnmesh.session import LinkSession

KEY = b"0123456789abcdef"
FRAME = bytes([0x01, 0x42, 0x01, 0x05, 0x03, 0x64])


class Wire:
    """Records what is sent; ``receiver`` is the layer's delivery callback."""

    def __init__(self) -> None:
        self.sent: list[bytes] = []
        self.receiver = None

    def send(self, data: bytes) -> None:
        self.sent.append(data)

    def set_receiver(self, callback) -> None:
        self.receiver = callback


def link(sender_state=None, receiver_state=None, sender_fcnt=0):
    """Two secure layers joined back to back, each with its own wire."""

    a, b = Wire(), Wire()
    sender = SecureTransport(a, KEY, initial_fcnt=sender_fcnt, state_file=sender_state)
    receiver = SecureTransport(b, KEY, initial_fcnt=0, state_file=receiver_state)
    got = []
    receiver.set_receiver(got.append)
    return a, b, sender, receiver, got


# --------------------------------------------------------- transmit counter


def test_counter_restart_point_is_never_behind_what_was_sent(tmp_path):
    state = tmp_path / "secure.json"
    wire = Wire()
    busy = SecureTransport(wire, KEY, initial_fcnt=1_000, state_file=state)
    for _ in range(1_000):  # far more than one reservation block
        busy.send(FRAME)
    last_sent = busy.fcnt

    # A crash: no close(). The clock-based seed is far behind the counter.
    restarted = SecureTransport(Wire(), KEY, initial_fcnt=500, state_file=state)
    assert restarted.fcnt >= last_sent


def test_without_a_state_file_a_busy_sender_restarts_behind():
    busy = SecureTransport(Wire(), KEY, initial_fcnt=1_000)
    for _ in range(1_000):
        busy.send(FRAME)
    assert SecureTransport(Wire(), KEY, initial_fcnt=500).fcnt < busy.fcnt


def test_state_is_written_once_per_block_not_per_frame(tmp_path, monkeypatch):
    writes = []
    real_replace = os.replace

    def counting_replace(*args):
        writes.append(args)
        return real_replace(*args)

    monkeypatch.setattr(secure.os, "replace", counting_replace)

    sender = SecureTransport(Wire(), KEY, initial_fcnt=0, state_file=tmp_path / "s.json")
    for _ in range(3 * TX_RESERVE):
        sender.send(FRAME)

    assert 1 < len(writes) <= 5


def test_sealed_frames_still_open_after_a_counter_restart(tmp_path):
    state = tmp_path / "secure.json"
    a, b, sender, receiver, got = link(sender_state=state)
    sender.send(FRAME)
    b.receiver(a.sent[-1])

    restarted = SecureTransport(a, KEY, initial_fcnt=0, state_file=state)
    restarted.send(FRAME)
    b.receiver(a.sent[-1])

    assert got == [FRAME, FRAME]  # the new run is ahead, so the receiver accepts it


# ----------------------------------------------------------- replay marks


def test_replay_marks_survive_a_receiver_restart(tmp_path):
    state = tmp_path / "rx.json"
    a, b, sender, receiver, got = link(receiver_state=state)
    for _ in range(5):
        sender.send(FRAME)
    for sealed in a.sent:
        b.receiver(sealed)
    receiver.close()
    assert len(got) == 5

    # The receiver restarts; an attacker plays back what was recorded earlier.
    revived = SecureTransport(Wire(), KEY, initial_fcnt=0, state_file=state)
    replayed = []
    revived.set_receiver(replayed.append)
    for sealed in a.sent:
        revived._on_frame(sealed)
    assert replayed == []
    assert revived.stats["replayed"] == 5

    sender.send(FRAME)  # a genuinely new frame is still accepted
    revived._on_frame(a.sent[-1])
    assert replayed == [FRAME]


def test_replay_marks_reach_the_file_after_traffic_stops(tmp_path, monkeypatch):
    monkeypatch.setattr(secure, "RX_FLUSH_INTERVAL", 0.05)
    state = tmp_path / "rx.json"
    a, b, sender, receiver, got = link(receiver_state=state)
    for _ in range(4):
        sender.send(FRAME)
        b.receiver(a.sent[-1])  # back to back: only the first write is immediate
    last_fcnt = sender.fcnt

    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        marks = json.loads(state.read_text()).get("rx", {})
        if marks.get("66") == last_fcnt:  # the sender id of FRAME is 0x42 = 66
            break
        time.sleep(0.02)
    receiver.close()
    assert json.loads(state.read_text())["rx"]["66"] == last_fcnt


def test_close_flushes_pending_marks(tmp_path):
    state = tmp_path / "rx.json"
    a, b, sender, receiver, got = link(receiver_state=state)
    sender.send(FRAME)
    b.receiver(a.sent[-1])
    sender.send(FRAME)
    b.receiver(a.sent[-1])  # inside the flush interval, still in memory
    receiver.close()

    assert json.loads(state.read_text())["rx"]["66"] == sender.fcnt


def test_corrupt_state_file_is_ignored_and_reported(tmp_path):
    state = tmp_path / "bad.json"
    state.write_text("{not json")
    transport = SecureTransport(Wire(), KEY, initial_fcnt=7, state_file=state)

    assert transport.state_error is not None
    assert transport.fcnt >= 7
    assert json.loads(state.read_text())["tx"] >= transport.fcnt  # replaced by a good one


def test_state_file_path_expands_home_and_creates_the_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    SecureTransport(Wire(), KEY, initial_fcnt=1, state_file="~/.cairnmesh/secure.json")
    assert (tmp_path / ".cairnmesh" / "secure.json").exists()


def test_missing_state_file_is_not_an_error(tmp_path):
    transport = SecureTransport(Wire(), KEY, initial_fcnt=7, state_file=tmp_path / "new.json")
    assert transport.state_error is None


def test_a_failed_reservation_write_stops_the_send(tmp_path):
    wire = Wire()
    sender = SecureTransport(wire, KEY, initial_fcnt=0, state_file=tmp_path / "s.json")
    sender._state_path = str(tmp_path / "missing-dir" / "s.json")
    sender._fcnt = sender._reserved  # the next send needs a new block

    with pytest.raises(OSError):
        sender.send(FRAME)
    with pytest.raises(OSError):  # still unreserved: the next send must try again
        sender.send(FRAME)
    assert wire.sent == []  # never sent under an unreserved counter

    sender._state_path = str(tmp_path / "s.json")  # the disk is back
    sender.send(FRAME)
    assert len(wire.sent) == 1
    assert json.loads((tmp_path / "s.json").read_text())["tx"] >= sender.fcnt


def test_receiving_does_not_touch_the_windows_while_a_send_holds_the_lock(tmp_path):
    # A send writing the state file iterates over the replay windows; a frame
    # from a new sender arriving on the reader thread must wait, or the dict
    # changes size under the writer.
    a, b, sender, receiver, got = link(receiver_state=tmp_path / "rx.json")
    sender.send(FRAME)
    with receiver._lock:
        reader = threading.Thread(target=b.receiver, args=(a.sent[-1],))
        reader.start()
        reader.join(timeout=0.2)
        assert reader.is_alive() and receiver._windows == {}
    reader.join(timeout=2.0)
    assert got == [FRAME]
    receiver.close()


# ------------------------------------------------------------- session


def test_state_file_without_a_key_is_refused(tmp_path):
    class Port:
        def read(self, n=1):
            return b""

        def write(self, data):
            pass

    with pytest.raises(ValueError):
        LinkSession.serial(Port(), nodes=[1], state_file=tmp_path / "s.json")


def test_serial_session_stops_the_port_before_the_final_flush(tmp_path, monkeypatch):
    closed = []

    class Port:
        def read(self, n=1):
            time.sleep(0.01)
            return b""

        def write(self, data):
            pass

        def close(self):
            closed.append("port")

    real_flush = SecureTransport.flush

    def flush(self):
        closed.append("flush")
        real_flush(self)

    monkeypatch.setattr(SecureTransport, "flush", flush)
    session = LinkSession.serial(Port(), nodes=[1], key=KEY, state_file=tmp_path / "s.json")
    assert isinstance(session.resources[-1], SecureTransport)
    with session:
        pass
    # Frames read after the flush would keep their replay marks only in memory.
    assert closed == ["port", "flush"]


def test_a_failing_close_does_not_leave_the_other_resources_open():
    closed, errors = [], []

    class Resource:
        def __init__(self, name, fail=False):
            self.name, self.fail = name, fail

        def start(self):
            pass

        def close(self):
            closed.append(self.name)
            if self.fail:
                raise OSError("disk full")

    session = LinkSession(
        Controller(Wire()), resources=[Resource("a", fail=True), Resource("b")]
    )
    session.events.subscribe(errors.append)
    with session:
        pass
    assert closed == ["a", "b"]
    assert any("disk full" in e.message for e in errors)


def test_loop_ticks_with_the_controllers_own_clock():
    offset = 1_000_000.0  # a clock far from time.monotonic()

    class Silent:
        def send(self, data):
            pass

        def set_receiver(self, callback):
            pass

    controller = Controller(Silent(), confirm_window=None, clock=lambda: time.monotonic() + offset)
    session = LinkSession(controller, tick_interval=0.005)
    with session:
        future = controller.request(1, 3, 1, timeout=0.05, max_retries=2)
        outcome = future.result(timeout=3.0)

    assert outcome.result is None and outcome.attempts == 3


# ------------------------------------------------------- retry window cap


def test_retry_window_longer_than_the_device_cache_is_refused():
    controller = Controller(Wire(), initial_sequence=3)
    with pytest.raises(ValueError, match="retry window"):
        controller.request(1, 3, 1, timeout=10.0, max_retries=5)
    with pytest.raises(ValueError, match="retry window"):
        controller.wake(1, timeout=10.0)
    # a refused request does not burn a sequence number
    assert controller.build_request(3, 1, 1).sequence == 3


def test_defaults_that_exceed_the_cap_fail_at_construction():
    with pytest.raises(ValueError, match="retry window"):
        Controller(Wire(), timeout=5.0)  # 6 tries x 5 s = 30 s


def test_a_slow_link_gets_fewer_wake_retries_instead_of_an_error():
    # Realistic LoRa timeouts (SF12 airtime alone is ~1.3 s) used to fail at
    # construction because the default 10 WAKE retries overran the cap.
    controller = Controller(Wire(), timeout=3.0)
    assert controller.max_retries <= controller.wake_max_retries < 10
    assert (controller.wake_max_retries + 1) * 3.0 <= controller.max_retry_window
    controller.wake(1)  # the default no longer trips the window check

    for timeout in (0.1, 0.3, 0.7, 2.5, 4.1):
        controller = Controller(Wire(), timeout=timeout)
        assert (controller.wake_max_retries + 1) * timeout <= controller.max_retry_window


def test_an_explicit_wake_retry_count_is_still_checked():
    with pytest.raises(ValueError, match="retry window"):
        Controller(Wire(), timeout=3.0, wake_max_retries=10)


@pytest.mark.parametrize(
    "options", [{"timeout": 0}, {"timeout": -1.0}, {"max_retries": -1}]
)
def test_nonsense_retry_parameters_are_refused(options):
    with pytest.raises(ValueError):
        Controller(Wire(), **options)
    with pytest.raises(ValueError):
        Controller(Wire(), initial_sequence=3).request(1, 3, 1, **options)


def test_the_cap_can_be_raised_or_switched_off():
    Controller(Wire(), timeout=5.0, max_retry_window=120.0)
    controller = Controller(Wire(), max_retry_window=None)
    controller.request(1, 3, 1, timeout=60.0, max_retries=10)


# ------------------------------------------------------------ saved node book


def _gateway():
    from cairnmesh.mesh_sim import MeshNetwork

    network = MeshNetwork({1: (50.0, 0.0), 2: (120.0, 0.0)}, key=KEY, gateway_options={})
    return network.sessions


def test_the_node_book_survives_a_restart_without_its_routes(tmp_path):
    from cairnmesh.frames import LinkQuality, ProbeHop

    gateway = _gateway()
    hop = ProbeHop(1, LinkQuality(-90, 40))
    gateway._found(2, (1,), (LinkQuality(-80, 50), LinkQuality(-90, 40)), "scan", state=1, neighbours=(hop,))
    path = tmp_path / "nodes.json"
    gateway.save_nodes(str(path))

    fresh = _gateway()
    assert fresh.load_nodes(str(path)) == 1
    node = fresh.nodes[2]
    assert node.route == () and not node.measured and node.hops == 2
    assert node.state == 1 and node.neighbours == (hop,) and node.how == "saved"


def test_a_measured_entry_wins_over_a_saved_one_and_bad_files_load_nothing(tmp_path):
    from cairnmesh.frames import LinkQuality

    gateway = _gateway()
    gateway._found(2, (1,), (LinkQuality(-80, 50), LinkQuality(-90, 40)), "scan")
    path = tmp_path / "nodes.json"
    gateway.save_nodes(str(path))
    assert gateway.load_nodes(str(path)) == 0 and gateway.nodes[2].measured
    path.write_text("{not json")
    assert _gateway().load_nodes(str(path)) == 0
    assert _gateway().load_nodes(str(tmp_path / "missing.json")) == 0
