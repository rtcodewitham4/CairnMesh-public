"""Regression tests for the 2026-10-21 second review.

Each scenario follows the matching case of ``tests/experiments/review_1021.py``
(copied here, not imported).
"""

from __future__ import annotations

import random

from cryptography.hazmat.primitives.ciphers.aead import AESCCM

from cairnmesh.airtime import airtime_for
from cairnmesh.controller import Controller, NodeConfirming
from cairnmesh.device import Device
from cairnmesh.events import ReplyIgnored
from cairnmesh.frames import APP, QUERY_ACTION, ROUTED, FrameKind, ReplyFrame, ReplyResult, RequestFrame, decode_frame
from cairnmesh.mesh_sim import MeshNetwork
from cairnmesh.secure import MIC_LEN, AppKeys, SecureTransport, seal

KEY = bytes(range(16))
G = 0x42


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class Port:
    """A transport that keeps what is sent."""

    def __init__(self) -> None:
        self.sent: list[bytes] = []

    def send(self, data: bytes) -> None:
        self.sent.append(bytes(data))

    def set_receiver(self, callback) -> None:
        pass


def ran_list(device: Device) -> list[tuple[int, int, int]]:
    """Record ``(sequence, action, value)`` of every action the device really runs."""

    ran: list[tuple[int, int, int]] = []
    original = device.perform_action

    def perform(frame):
        ran.append((frame.sequence, frame.action, frame.value))
        return original(frame)

    device.perform_action = perform
    return ran


class Link:
    """Controller and one Device (id 1) over an in-memory link whose two directions can be cut."""

    class _End:
        def __init__(self, link: "Link", direction: str) -> None:
            self.link, self.direction, self.receiver = link, direction, None

        def send(self, data: bytes) -> None:
            ok = self.link.down_ok if self.direction == "down" else self.link.up_ok
            if ok:
                self.link.queue.append((self.link.clock.now + 0.05, self.direction, bytes(data)))

        def set_receiver(self, callback) -> None:
            self.receiver = callback

    def __init__(self, action_time=0.0, **controller_options) -> None:
        self.clock = Clock()
        self.down_ok = True  # controller -> device
        self.up_ok = True  # device -> controller
        self.queue: list[tuple[float, str, bytes]] = []
        self.controller_end, self.device_end = Link._End(self, "down"), Link._End(self, "up")
        self.device = Device(
            self.device_end, 1, awake=True, heartbeat_interval=1e9, action_time=action_time, clock=self.clock
        )
        self.ran = ran_list(self.device)
        options = dict(timeout=1.0, max_retries=3, epoch=9, initial_sequence=0, clock=self.clock)
        options.update(controller_options)
        self.controller = Controller(self.controller_end, **options)

    def run(self, seconds: float, step: float = 0.05) -> None:
        end = self.clock.now + seconds
        while self.clock.now < end:
            self.clock.now = round(self.clock.now + step, 6)
            due = [m for m in self.queue if m[0] <= self.clock.now]
            self.queue = [m for m in self.queue if m[0] > self.clock.now]
            for _, direction, data in due:
                end_point = self.device_end if direction == "down" else self.controller_end
                end_point.receiver(data)
            self.device.tick(self.clock.now)
            self.controller.tick(self.clock.now)


def mesh(positions, **options):
    """A disc-model ``MeshNetwork`` with the session gateway and an attached controller."""

    clock = Clock()
    network = MeshNetwork(
        positions, key=KEY, clock=clock, airtime=airtime_for(7), gateway_options={}, seed=3, signal=None, **options
    )
    controller = network.controller(network.sessions, clock=clock)
    network.sessions.attach(controller)

    def run(seconds: float, controller_too: bool = True) -> None:
        end = clock.now + seconds
        while clock.now < end:
            clock.now += 0.005
            network.tick(clock.now)
            if controller_too:
                controller.tick(clock.now)

    return clock, network, controller, run


TRIANGLE = {1: (80.0, 0.0), 2: (160.0, 0.0), 3: (80.0, 60.0)}


def request(sequence: int, action: int, value: int = 0, epoch: int = 7) -> bytes:
    return RequestFrame(G, 1, sequence, action=action, value=value, epoch=epoch).encode()


def slow_device(slow: float = 60.0) -> tuple[Device, Port, Clock]:
    """A device whose action 1 takes ``slow`` seconds (ACCEPTED, RESULT later); every other action is instant."""

    clock, port = Clock(), Port()
    device = Device(
        port, 1, awake=True, heartbeat_interval=1e9, clock=clock,
        action_time=lambda frame: slow if frame.action == 1 else 0.0,
    )
    return device, port, clock


def last_result(port: Port) -> int:
    return decode_frame(port.sent[-1]).result


# ------------------------------------------------- A2: open records on top of the window


def test_an_instant_request_among_four_open_ones_runs_once():
    device, _, _ = slow_device()
    for sequence in range(4):
        device.on_frame(request(sequence, 1))
    device.on_frame(request(10, 2))
    device.on_frame(request(10, 2))  # its REPLY was lost: the controller retries
    assert device.handled_count - 4 == 1


def test_not_run_stays_final_while_four_are_open():
    device, port, _ = slow_device()
    for sequence in range(4):
        device.on_frame(request(sequence, 1))
    device.on_frame(RequestFrame(G, 1, 30, action=QUERY_ACTION, value=20, epoch=7).encode())
    assert last_result(port) == ReplyResult.NOT_RUN
    before = device.handled_count
    device.on_frame(request(20, 2))  # the late original copy
    assert device.handled_count == before
    assert last_result(port) == ReplyResult.NOT_RUN


def test_a_refusal_stays_final_after_a_slot_frees():
    device, port, clock = slow_device()
    for sequence in range(4):
        device.on_frame(request(sequence, 1))
    device.on_frame(request(5, 1))
    assert last_result(port) == ReplyResult.NOT_RUN
    clock.now += 61.0
    device.tick(clock.now)
    device.on_frame(ReplyFrame(G, 1, 0, 0, epoch=7).encode())  # one RESULT acknowledged: a slot frees
    before = device.handled_count
    device.on_frame(request(5, 1))
    assert device.handled_count == before
    assert last_result(port) == ReplyResult.NOT_RUN


def test_an_open_record_leaves_the_force_allowance_alone():
    link = Link(action_time=lambda frame: 600.0 if frame.action == 9 else 0.0)
    link.controller.request(1, 9, 0)  # slow: ACCEPTED, open for 10 min
    link.run(3.0)
    link.up_ok = False
    x = link.controller.request(1, 5, 1)  # runs; every answer lost
    link.run(100.0)
    link.up_ok = True
    for value in (1, 2, 3):
        link.controller.request(1, 6, value, force=True)
    link.run(300.0)
    assert any(r[1] == 5 for r in link.ran)
    assert x.result(0).status == "done"


# ------------------------------------- A1 + A4: ids the controller still matches


def test_a_closing_keepalives_answer_stays_in_the_gateway():
    _, network, controller, run = mesh(TRIANGLE)
    events: list = []
    controller.events.subscribe(events.append)
    network.sessions.open_session(2)
    run(20.0)
    before = len(events)
    network.sessions.close_session(2)
    run(3.0)
    assert not [e for e in events[before:] if isinstance(e, ReplyIgnored)]
    assert network.sessions.stats["own_answers"] >= 1


def test_ids_skip_what_a_waiting_request_still_matches():
    link = Link()
    controller = link.controller
    link.up_ok = False
    controller.request(1, 5, 1)
    link.run(100.0)  # confirming: queries sent
    controller.request(1, 6, 1, force=True)  # X parked
    controller.request(1, 6, 2, force=True)  # queued behind the forced one, not sent
    with controller._lock:
        parked = controller._parked[1][0]
        protected = {parked.original.sequence, *parked.query_sequences}
        protected.update(item.frame.sequence for item in controller._queues[1])
        protected.update(mid & 0xFF for mid in controller._id_ledger.request_ids[1] if mid >> 8 == controller.epoch)
    taken = {controller.allocate_id(1)[0] for _ in range(600)}
    assert not taken & protected


def test_a_keepalive_answer_never_settles_a_confirming_command():
    _, network, controller, run = mesh(TRIANGLE)
    gateway = network.sessions
    ran = ran_list(network.devices[2])
    network.set_power(2, False)
    x = gateway.request(2, 5, 1)
    x_seq = controller._queues[2][0].frame.sequence
    run(130.0)
    for value in range(16):
        gateway.request(3, 4, value)
        run(6.0)
    network.set_power(2, True)
    session = gateway.open_session(2)
    for _ in range(4000):
        if session.state == "open":
            break
        run(0.005, controller_too=False)
    for _ in range(600):  # one turn of the shared counter
        if controller._id_ledger.next_sequence[2] == x_seq:
            break
        controller.allocate_id(2)
    gateway.close_session(2)
    run(3.0, controller_too=False)
    assert not x.done()
    run(200.0)
    assert x.result(0).status == "not_run"
    assert all(r[0] != x_seq or r[1] != 5 for r in ran)


def test_an_accepted_commands_id_is_not_reused_while_its_result_is_due():
    link = Link(action_time=lambda frame: 3600.0 if frame.action == 9 else 0.0)
    controller = link.controller
    a = controller.request(1, 9, 0)
    link.run(2.0)
    a_seq = a.result(0).sequence
    for value in range(16):
        controller.request(1, 2, value)
        link.run(2.0)
    for _ in range(600):
        if controller._id_ledger.next_sequence[1] == a_seq:
            break
        controller.allocate_id(1)
    b = controller.request(1, 4, 77)
    link.run(2.0)
    outcome = b.result(0)
    assert outcome.sequence != a_seq
    assert any(r[1] == 4 for r in link.ran)
    assert outcome.completion is not a.result(0).completion


def test_a_queued_requests_id_is_not_reused():
    # While X is still in its query window a command waits behind it, its id
    # taken when it was built (once X goes confirming it ends NOT_SENT, A3b).
    link = Link()
    controller = link.controller
    link.up_ok = False
    controller.request(1, 5, 1)
    link.run(1.0)
    controller.request(1, 6, 1)  # waits behind X, never sent yet
    queued_seq = controller._queues[1][1].frame.sequence
    link.run(30.0)  # X being queried
    taken = {controller.allocate_id(1)[0] for _ in range(600)}  # keepalives, probes, queries
    assert queued_seq not in taken


# ------------------------------------- A3 + D3: what goes past a confirming request


def test_requests_queued_behind_end_not_sent_when_it_goes_confirming():
    # A3 (force released them uncounted), then A3b (user's decision): they end NOT_SENT.
    link = Link()
    controller = link.controller
    link.up_ok = False
    x = controller.request(1, 5, 1)
    link.run(1.0)
    queued = [controller.request(1, 6, value) for value in (1, 2, 3)]  # allowed in X's query window
    link.run(100.0)
    assert [future.result(0).status for future in queued] == ["not_sent"] * 3
    link.up_ok = True
    controller.request(1, 7, 4, force=True)  # nothing waits behind X now: allowed
    link.run(400.0)
    assert x.result(0).status == "done"
    assert [r[1] for r in link.ran] == [5, 7]


def test_force_counts_forced_requests_still_queued():
    link = Link()
    controller = link.controller
    link.up_ok = False
    controller.request(1, 5, 1)
    link.run(100.0)  # X confirming
    link.down_ok = False  # the forced ones queue up behind the first
    for value in (1, 2, 3):
        controller.request(1, 6, value, force=True)
    try:
        controller.request(1, 6, 4, force=True)
        raise AssertionError("a fourth past X must be refused, sent or not")
    except NodeConfirming:
        pass
    assert controller.past_allowance(1)[1] == 0


def test_force_with_one_queued_is_allowed_and_the_record_survives():
    link = Link()
    controller = link.controller
    link.up_ok = False
    x = controller.request(1, 5, 1)
    link.run(1.0)
    controller.request(1, 6, 1)
    link.run(100.0)
    link.up_ok = True
    controller.request(1, 7, 2, force=True)
    link.run(400.0)
    assert x.result(0).status == "done"
    assert [r[1] for r in link.ran].count(5) == 1


def test_a_wake_goes_ahead_of_a_confirming_request():
    link = Link()
    controller = link.controller
    link.up_ok = False
    x = controller.request(1, 5, 1)
    link.run(100.0)
    link.up_ok = True
    wake = controller.wake(1)
    link.run(0.3)
    assert wake.done() and wake.result(0).ok
    link.run(300.0)
    assert x.result(0).status == "done"
    assert [r[1] for r in link.ran] == [5]


# ------------------------------------- A5 + A6: answers for what is not at the head


def test_a_result_for_a_parked_request_settles_it():
    link = Link(action_time=lambda frame: 5.0 if frame.action == 5 else 0.0)
    controller = link.controller
    link.up_ok = False
    x = controller.request(1, 5, 1)  # ACCEPTED and RESULT lost
    link.run(0.2)
    link.down_ok = False  # queries (which would close the record) and the forced one stay out
    link.run(100.0)
    controller.request(1, 6, 1, force=True)  # X parked
    link.up_ok = True
    link.run(100.0)  # X's last RESULT copy (t ~ 155 s, 117.5-192.5 s with the ±25 % jitter) comes in
    assert x.result(0).status == "done" and x.result(0).result == ReplyResult.OK
    assert all(item.sequence != x.result(0).sequence for item in controller.confirming())


def test_a_late_answer_for_a_parked_request_settles_it():
    link = Link()
    controller = link.controller
    link.up_ok = False
    x = controller.request(1, 5, 1)
    link.run(100.0)
    link.down_ok = False
    controller.request(1, 6, 1, force=True)  # X parked
    parked = controller._parked[1][0]
    late = ReplyFrame(1, controller.sender_id, parked.query_sequences[-1], ReplyResult.OK, epoch=controller.epoch)
    controller.on_frame(late.encode())
    assert x.result(0).status == "done"
    assert not controller._parked.get(1)


def test_a_query_that_meets_a_short_action_still_gets_the_result():
    link = Link(action_time=0.4, timeout=0.2, max_retries=0)
    controller = link.controller
    x = controller.request(1, 5, 1)
    link.run(1.0)
    outcome = x.result(0)
    for value in range(4):  # four more commands within the minute push the closed record out
        controller.request(1, 6, value)
        link.run(2.0)
    link.run(120.0)
    assert outcome.completion is not None and outcome.completion.result(0) == ReplyResult.OK


# ------------------------------------- B1: a relay cannot forge a command's answer


class Wire:
    def __init__(self) -> None:
        self.sent: list[bytes] = []
        self.receiver = None

    def set_receiver(self, callback) -> None:
        self.receiver = callback

    def send(self, data: bytes) -> None:
        self.sent.append(bytes(data))


def test_a_plain_reply_to_a_request_we_await_is_dropped_however_old():
    wire = Wire()
    secure = SecureTransport(wire, KEY, initial_fcnt=0, app=AppKeys({1: bytes(16)}))
    heard: list[bytes] = []
    secure.set_receiver(heard.append)
    awaited = (1, 9 << 8 | 33)
    secure.awaits_request_answer = lambda node, mid: (node, mid) == awaited
    for sequence in range(16):  # 16 other exchanges push it out of ``_exchanges``
        secure.send(RequestFrame(G, 2, sequence, action=1, value=0, epoch=9).encode())
    aead = AESCCM(KEY, tag_length=MIC_LEN)
    wire.receiver(seal(aead, ReplyFrame(1, G, 33, ReplyResult.OK, epoch=9).encode(), 500, tx=1))
    assert heard == [] and secure.stats["app_missing"] == 1
    wire.receiver(seal(aead, ReplyFrame(1, G, 34, ReplyResult.OK, epoch=9).encode(), 501, tx=1))
    assert len(heard) == 1  # not awaited (a keepalive's answer): passes


def test_a_relay_cannot_settle_a_confirming_command():
    _, network, controller, run = mesh(TRIANGLE)
    gateway = network.sessions
    network.set_power(2, False)
    x = gateway.request(2, 5, 1)
    x_seq, x_epoch = controller._queues[2][0].frame.sequence, controller.epoch
    run(130.0)
    for value in range(16):
        gateway.request(3, 4, value)
        run(6.0)
    fcnt = network.endpoints[2]._transport.fcnt + 100
    forged = seal(AESCCM(KEY, tag_length=MIC_LEN), ReplyFrame(2, G, x_seq, 0, epoch=x_epoch).encode(), fcnt, tx=1)
    network.relays[G]._on_radio(forged, -80.0)
    run(1.0)
    assert not x.done()
    assert network.gateway.stats["app_missing"] >= 1


def test_keepalive_and_wake_answers_still_pass_without_the_layer():
    _, network, controller, run = mesh(TRIANGLE)
    gateway = network.sessions
    session = gateway.open_session(2)
    run(20.0)
    assert session.state == "open"
    run(10.0)  # keepalives every 2 s, answered in plain
    assert session.state == "open"
    wake = controller.wake(3)
    run(20.0)
    assert wake.result(0).ok
    command = gateway.request(2, 4, 1)
    run(10.0)
    assert command.result(0).status == "done"


# ------------------------------------- B4: a good MIC on a malformed frame


def test_authenticated_garbage_never_raises():
    clock = Clock()
    network = MeshNetwork(
        TRIANGLE, key=KEY, clock=clock, airtime=airtime_for(7), gateway_options={}, seed=3, signal=None,
        hello_interval=10.0, discovery_options={}, wake_options={},
    )
    controller = network.controller(network.sessions, clock=clock)
    network.sessions.attach(controller)
    aead = AESCCM(KEY, tag_length=MIC_LEN)
    rng = random.Random(1021)
    kinds = [kind for kind in FrameKind] + [0x00, 0x1F, 0x3F]
    fcnt = 1_800_000_000
    for node, relay in network.relays.items():
        for _ in range(3000):
            kind = rng.choice(kinds) | rng.choice((0, ROUTED, APP, ROUTED | APP))
            body = bytes([kind & 0xFF]) + bytes(rng.randrange(256) for _ in range(rng.randrange(3, 40)))
            fcnt += 1
            sender = rng.choice([n for n in (G, *TRIANGLE) if n != node])
            relay._on_radio(seal(aead, body, fcnt, tx=sender), -80.0)
            clock.now += 0.01
            network.tick(clock.now)
            controller.tick(clock.now)
    assert all(relay.stats["observer_errors"] == 0 for relay in network.relays.values())


def test_the_review_cases_of_malformed_frames():
    from cairnmesh.mesh import MeshRelay
    from cairnmesh.wake import WakeAgent

    class Radio:
        def __init__(self) -> None:
            self.receiver = None

        def send(self, data: bytes, preamble: int = 8) -> None:
            pass

        def set_receiver(self, callback) -> None:
            self.receiver = callback

        def set_dormant(self, dormant: bool) -> None:
            pass

    aead = AESCCM(KEY, tag_length=MIC_LEN)
    radio = Radio()
    secure = SecureTransport(radio, KEY, initial_fcnt=0, app=AppKeys({1: bytes(16)}))
    secure.set_receiver(lambda plain: None)
    radio.receiver(seal(aead, bytes([FrameKind.REPLY | ROUTED, 5, G, 0x1F]), 10))
    assert secure.stats["app_failed"] == 1
    relay_radio = Radio()
    relay = MeshRelay(relay_radio, KEY, 5, clock=lambda: 0.0, rng=random.Random(1))
    relay_radio.receiver(seal(aead, bytes([FrameKind.KEEPALIVE | ROUTED, G, 9, 1, 7, 60, 0x07, 5, 1]), 11), -80.0)
    relay_radio.receiver(seal(aead, bytes([FrameKind.TRACE | ROUTED, G, 9, 1, 7, 5, 1]), 12), -80.0)
    assert relay.stats["bad_route"] == 2
    wake_radio = Radio()
    wake_relay = MeshRelay(wake_radio, KEY, 6, clock=lambda: 0.0, rng=random.Random(1))
    agent = WakeAgent(wake_relay, Radio(), 6, clock=lambda: 0.0, dormant=False)
    agent.on_heard(bytes([FrameKind.PROBE, G, 9, 1, 7, G]), -80.0)  # no WINDOW byte
    agent.on_heard(bytes([FrameKind.WAKE_NET, G, 9]), -80.0)
    agent.on_heard(bytes([FrameKind.KEEPALIVE | ROUTED, G, 6, 1, 7, 0]), -80.0)


# ------------------------------------- B3 + D2: state files


def test_durable_write_fsyncs_file_and_directory(tmp_path, monkeypatch):
    import json
    import os

    from cairnmesh.durable import write_json

    calls: list[int] = []
    real = os.fsync
    monkeypatch.setattr(os, "fsync", lambda fd: (calls.append(fd), real(fd))[1])
    path = tmp_path / "state.json"
    write_json(str(path), {"a": 1})
    assert len(calls) == 2
    assert json.loads(path.read_text()) == {"a": 1}
    assert [p.name for p in tmp_path.iterdir()] == ["state.json"]


def test_secure_state_is_fsynced(tmp_path, monkeypatch):
    import os

    calls: list[int] = []
    real = os.fsync
    monkeypatch.setattr(os, "fsync", lambda fd: (calls.append(fd), real(fd))[1])
    SecureTransport(Wire(), KEY, state_file=str(tmp_path / "secure.json"))
    assert len(calls) >= 2


def test_two_writers_never_break_the_file(tmp_path):
    import json
    import threading

    from cairnmesh.durable import write_json

    path = str(tmp_path / "state.json")
    errors: list[BaseException] = []

    def writer(tag: int) -> None:
        try:
            for index in range(500):
                write_json(path, {"tag": tag, "index": index})
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(tag,)) for tag in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors
    with open(path, encoding="utf-8") as handle:
        assert json.load(handle)["index"] == 499


def test_an_older_snapshot_never_overwrites_a_newer_one(tmp_path):
    import json

    from cairnmesh.controller import _SideEffects

    path = str(tmp_path / "controller.json")
    controller = Controller(Port(), state_file=path, epoch=1)
    with controller._lock:
        older = controller._snapshot()
    controller.request(5, 1, 0)
    with controller._lock:
        newer = controller._snapshot()
    controller._apply(_SideEffects(state=newer))
    controller._apply(_SideEffects(state=older))  # the slower thread finishes last
    with open(path, encoding="utf-8") as handle:
        assert json.load(handle)["request_ids"] == newer["request_ids"]


def test_a_failed_state_write_holds_new_requests(tmp_path, monkeypatch):
    from cairnmesh import state_store as state_store_module

    clock = Clock()
    port = Port()
    controller = Controller(port, state_file=str(tmp_path / "controller.json"), epoch=1, timeout=1.0, clock=clock)
    real = state_store_module.write_json
    failures = [1]

    def flaky(path, data):
        if failures:
            failures.pop()
            raise OSError("disk full")
        real(path, data)

    monkeypatch.setattr(state_store_module, "write_json", flaky)
    controller.request(5, 1, 0)
    assert port.sent == []  # its id is not on disk: held
    clock.now += 0.1
    controller.tick(clock.now)  # the write succeeds now; the retry is due later
    clock.now += 1.0
    controller.tick(clock.now)
    assert [decode_frame(data).action for data in port.sent] == [1]


# ------------------------------------- D1 + D5: the serial path


class CapturePort:
    """The read/write API of ``serial.Serial``: keeps what is written, reads nothing."""

    def __init__(self) -> None:
        self.written = bytearray()

    def write(self, data: bytes) -> None:
        self.written += data

    def read(self, n: int = 1) -> bytes:
        import time

        time.sleep(0.01)
        return b""

    def close(self) -> None:
        pass


def test_no_state_file_is_warned():
    assert Controller(Port()).state_warning is not None
    assert Controller(Port(), epoch=5).state_warning is None  # an explicit EPOCH is the caller's choice


def test_serial_session_reports_the_missing_state_file():
    from cairnmesh.session import LinkSession

    # ``serial`` publishes ``state_warning`` as a LinkError while it builds the
    # session (before anyone can subscribe); the warning is what goes out.
    session = LinkSession.serial(CapturePort(), nodes=[1])
    assert "状态文件" in session.controller.state_warning


def test_serial_session_seals_requests_with_the_app_layer(tmp_path):
    from cairnmesh.secure import open_sealed
    from cairnmesh.serial_link import SlipDecoder
    from cairnmesh.session import LinkSession

    port = CapturePort()
    session = LinkSession.serial(
        port, nodes=[1], key=KEY, app_keys=AppKeys({1: bytes(16)}), state_file=str(tmp_path / "secure.json")
    )
    session.controller.request(1, 3, 100)
    frames = SlipDecoder().feed(bytes(port.written))
    assert frames
    plain, _ = open_sealed(AESCCM(KEY, tag_length=MIC_LEN), frames[-1])
    assert plain[0] & APP and plain[0] & ~APP == FrameKind.REQUEST
    assert session.controller.transport.awaits_request_answer is not None


# ------------------------------------- user's decisions (2026-10-21): D1, D4


def test_no_state_file_and_no_epoch_refuses_commands(monkeypatch):
    from cairnmesh import controller as controller_module
    from cairnmesh.controller import NoStateFile

    monkeypatch.setattr(controller_module, "VOLATILE_EPOCH_ALLOWED", False)
    bare = Controller(Port())
    try:
        bare.request(1, 3, 0)
        raise AssertionError("must refuse without a state file")
    except NoStateFile:
        pass
    assert bare.wake(1) is not None  # a WAKE is not cached: never refused
    Controller(Port(), allow_volatile_epoch=True).request(1, 3, 0)
    Controller(Port(), epoch=4).request(1, 3, 0)


def test_serial_session_keeps_controller_state_by_default(tmp_path, monkeypatch):
    import os

    from cairnmesh import controller as controller_module
    from cairnmesh.session import LinkSession

    monkeypatch.setattr(controller_module, "VOLATILE_EPOCH_ALLOWED", False)
    monkeypatch.setenv("CAIRNMESH_STATE_DIR", str(tmp_path))
    session = LinkSession.serial(CapturePort(), nodes=[1])
    assert session.controller.state_file == os.path.join(str(tmp_path), "controller.json")
    session.controller.request(1, 3, 0)  # not refused


def test_force_skips_acceptance_only_and_past_goes_past_confirming():
    _, network, controller, run = mesh(TRIANGLE)
    gateway = network.sessions
    network.set_power(2, False)
    x = gateway.request(2, 5, 1)
    run(130.0)  # X confirming
    assert not x.done()
    try:
        gateway.request(2, 6, 1, force=True)
        raise AssertionError("force alone must not go past a confirming command")
    except NodeConfirming:
        pass
    assert controller.past_allowance(2) == ([x_seq := controller._queues[2][0].original.sequence], 3)
    gateway.request(2, 6, 1, past_confirming=True)
    assert controller.past_allowance(2) == ([x_seq], 2)


# ------------------------------------- user's decision: C1 (a), wire format v3


def test_a_reports_subject_is_readable_but_not_forgeable():
    from cairnmesh.frames import ReportEvent, ReportFrame
    from cairnmesh.secure import open_sealed

    keys = AppKeys({1: bytes(16)})
    node_wire, controller_wire = Wire(), Wire()
    node = SecureTransport(node_wire, KEY, initial_fcnt=0, app=keys)
    controller = SecureTransport(controller_wire, KEY, initial_fcnt=0, app=keys)
    heard: list[bytes] = []
    controller.set_receiver(heard.append)
    report = ReportFrame(1, G, 4, subject=9, event=ReportEvent.LOST, epoch=3).encode()
    node.send(report)
    aead = AESCCM(KEY, tag_length=MIC_LEN)
    below, fcnt = open_sealed(aead, node_wire.sent[-1])
    assert below[0] == FrameKind.REPORT | APP and below[6] == 9 and below[7] == ReportEvent.LOST
    tampered = below[:6] + bytes([8]) + below[7:]  # a relay names another node
    controller_wire.receiver(seal(aead, tampered, fcnt, tx=5))
    assert heard == [] and controller.stats["app_failed"] == 1
    controller_wire.receiver(node_wire.sent[-1])
    assert heard == [report]


def test_report_suppression_works_with_the_application_layer():
    grid = {r * 4 + c + 1: (60.0 * c + 60.0, 60.0 * r) for r in range(3) for c in range(4)}
    clock = Clock()
    network = MeshNetwork(
        grid, key=KEY, clock=clock, airtime=airtime_for(7), hello_interval=10.0, seed=0, signal=None, app_keys=True,
    )
    controller = Controller(network.gateway, clock=clock)
    while clock.now < 120.0:
        clock.now += 0.01
        network.tick(clock.now)
        controller.tick(clock.now)
    network.set_power(6, False)
    while clock.now < 320.0:
        clock.now += 0.01
        network.tick(clock.now)
        controller.tick(clock.now)
    watches = network.watches.values()
    assert sum(w.stats["reports_suppressed"] for w in watches) >= 4
    assert sum(w.stats["reports_sent"] for w in watches) <= 3
