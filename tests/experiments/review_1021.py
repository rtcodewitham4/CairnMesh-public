"""2026-10-21 second review: one reproduction per finding.

Run from the repository root:

    PYTHONPATH=. .venv/bin/python tests/experiments/review_1021.py          # every case
    PYTHONPATH=. .venv/bin/python tests/experiments/review_1021.py a1 b4    # some

Each case prints ``缺陷仍在`` (the defect still shows) or ``已修复`` and one
line of detail. Before any fix every case shows its defect; once the plan is
carried out every case reads ``已修复`` (B2 and C1 were decided by the user on
2026-10-22; ``b2s`` is a known risk). ``d1mc`` is an estimate, not a check. About 2 minutes in all
(``c1`` takes most of it).

Not a unit test or a benchmark: not in ``gate``, not collected by pytest.
The regression tests the plan asks for go to ``tests/unit/test_lora_cc_review_1021.py``.
"""

from __future__ import annotations

import inspect
import json
import os
import random
import sys
import tempfile

from cryptography.hazmat.primitives.ciphers.aead import AESCCM

from cairnmesh.airtime import airtime_for
from cairnmesh.controller import Controller, NodeConfirming, _SideEffects
from cairnmesh.device import Device
from cairnmesh.events import ReplyIgnored
from cairnmesh.frames import (
    QUERY_ACTION,
    ROUTED,
    FrameKind,
    HelloFrame,
    NodeState,
    ReplyFrame,
    RequestFrame,
    WakeNetFrame,
    decode_frame,
)
from cairnmesh.mesh import MeshRelay
from cairnmesh.mesh_sim import MeshNetwork
from cairnmesh.secure import MIC_LEN, AppKeys, SecureTransport, seal

KEY = bytes(range(16))
AEAD = AESCCM(KEY, tag_length=MIC_LEN)
G = 0x42


# ---------------------------------------------------------------- helpers


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class Null:
    """A transport that drops everything."""

    def send(self, data: bytes) -> None:
        pass

    def set_receiver(self, callback) -> None:
        pass


class Port(Null):
    """A transport that keeps what is sent."""

    def __init__(self) -> None:
        self.sent: list[bytes] = []

    def send(self, data: bytes) -> None:
        self.sent.append(bytes(data))


class Radio:
    """A fake radio for one ``MeshRelay`` (also stands in for a ``WakeAgent`` port)."""

    def __init__(self) -> None:
        self.receiver = None
        self.sent: list[bytes] = []

    def send(self, data: bytes, preamble: int = 8) -> None:
        self.sent.append(data)

    def set_receiver(self, callback) -> None:
        self.receiver = callback

    def set_dormant(self, dormant: bool) -> None:
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
        # The controller's end sends down, the device's end sends up.
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


# ---------------------------------------------------------------- cases


def a1() -> tuple[bool, str]:
    """A1: a REPLY to the gateway's own keepalive settles a confirming command as DONE."""

    _, network, controller, run = mesh({1: (80.0, 0.0), 2: (160.0, 0.0), 3: (80.0, 60.0)})
    gateway = network.sessions
    events: list = []
    controller.events.subscribe(events.append)
    ran = ran_list(network.devices[2])
    # 1. the closing keepalive's answer reaches the controller's matching
    gateway.open_session(2)
    run(20.0)
    before = len(events)
    gateway.close_session(2)
    run(3.0)
    leaked = [e for e in events[before:] if isinstance(e, ReplyIgnored)]
    # 2. and settles a confirming command once the app layer's exchange memory has moved on
    network.set_power(2, False)
    x = gateway.request(2, 5, 1)
    x_seq = controller._queues[2][0].frame.sequence
    run(130.0)  # retries, queries, then CONFIRMING
    for value in range(16):  # ordinary traffic to another node
        gateway.request(3, 4, value)
        run(6.0)
    network.set_power(2, True)
    session = gateway.open_session(2)
    for _ in range(4000):  # the path opens (network only: the nudge's query would settle X first)
        if session.state == "open":
            break
        run(0.005, controller_too=False)
    for _ in range(600):  # stands for one turn of the shared counter (keepalives, probes)
        if controller._id_ledger.next_sequence[2] == x_seq:
            break
        controller.allocate_id(2)
    gateway.close_session(2)  # its KEEPALIVE takes the next id
    run(3.0, controller_too=False)
    status = x.result(0).status if x.done() else "确认中"
    wrong = status == "done" and all(r[0] != x_seq for r in ran)
    return bool(leaked) or wrong, f"关会话保活的回答进了控制端 {len(leaked)} 次；X（设备从未执行）的结论：{status}"


def a2_retry() -> tuple[bool, str]:
    """A2: four open records, then an instant request: its record is pushed out at once, the retry runs it again."""

    device, _, _ = slow_device()
    for sequence in range(4):
        device.on_frame(request(sequence, 1))
    device.on_frame(request(10, 2))
    device.on_frame(request(10, 2))  # its REPLY was lost: the controller retries
    executed = device.handled_count - 4
    return executed > 1, f"瞬时命令 SEQ 10 被执行 {executed} 次（应为 1）"


def a2_not_run() -> tuple[bool, str]:
    """A2: four open records; a query answers NOT_RUN, then the late original copy runs anyway."""

    device, port, _ = slow_device()
    for sequence in range(4):
        device.on_frame(request(sequence, 1))
    device.on_frame(RequestFrame(G, 1, 30, action=QUERY_ACTION, value=20, epoch=7).encode())
    answer = last_result(port)
    before = device.handled_count
    device.on_frame(request(20, 2))
    ran = device.handled_count - before
    return ran > 0, f"查询答 {answer}（2 = NOT_RUN），迟到的原命令随后被执行 {ran} 次（应为 0）"


def a2_refused() -> tuple[bool, str]:
    """A2: a refusal (open slots full, NOT_RUN) is not final: once a slot frees, the retry runs."""

    device, port, clock = slow_device()
    for sequence in range(4):
        device.on_frame(request(sequence, 1))
    device.on_frame(request(5, 1))
    refused = last_result(port)
    clock.now += 61.0
    device.tick(clock.now)  # the four actions are over: their RESULTs go out
    device.on_frame(ReplyFrame(G, 1, 0, 0, epoch=7).encode())  # the controller acknowledges one
    before = device.handled_count
    device.on_frame(request(5, 1))  # a retry of the refused one
    ran = device.handled_count - before
    return ran > 0, f"第 5 条先被拒（{refused}，2 = NOT_RUN），空出槽位后重试被执行 {ran} 次（应为 0）"


def a2_force_open() -> tuple[bool, str]:
    """A2: one open record shrinks the device's room, so three forced requests push a confirming one out."""

    link = Link(action_time=lambda frame: 600.0 if frame.action == 9 else 0.0)
    link.controller.request(1, 9, 0)  # slow: ACCEPTED, open for 10 min
    link.run(3.0)
    link.up_ok = False
    x = link.controller.request(1, 5, 1)  # runs; every answer lost
    link.run(100.0)
    link.up_ok = True
    refused = 0
    for value in (1, 2, 3):  # back to back: X stays parked
        try:
            link.controller.request(1, 6, value, force=True)
        except NodeConfirming:
            refused += 1
    link.run(300.0)
    status = x.result(0).status if x.done() else "确认中"
    ran_x = any(r[1] == 5 for r in link.ran)
    return status == "not_run" and ran_x, f"X 已执行={ran_x}，结论：{status}（force 被拒 {refused} 条）"


def a3() -> tuple[bool, str]:
    """A3: force releases the requests queued behind a confirming one without counting them."""

    link = Link()
    link.up_ok = False
    x = link.controller.request(1, 5, 1)
    link.run(1.0)
    for value in (1, 2, 3):  # wait behind X: allowed while X is not confirming yet
        link.controller.request(1, 6, value)
    link.run(100.0)
    link.up_ok = True
    try:
        link.controller.request(1, 7, 4, force=True)
        forced = "放行"
    except NodeConfirming:
        forced = "拒绝"
    link.run(400.0)
    status = x.result(0).status if x.done() else "确认中"
    ran_x = any(r[1] == 5 for r in link.ran)
    return status == "not_run" and ran_x, f"force {forced}；X 已执行={ran_x}，结论：{status}"


def a4() -> tuple[bool, str]:
    """A4: the 16-id skip list forgets a command still running; a new one takes its id and never runs."""

    link = Link(action_time=lambda frame: 3600.0 if frame.action == 9 else 0.0)
    controller = link.controller
    a = controller.request(1, 9, 0)
    link.run(2.0)
    a_seq = a.result(0).sequence
    for value in range(16):
        controller.request(1, 2, value)
        link.run(2.0)
    for _ in range(600):  # keepalives / probes / queries turn the counter
        if controller._id_ledger.next_sequence[1] == a_seq:
            break
        controller.allocate_id(1)
    b = controller.request(1, 4, 77)
    link.run(2.0)
    outcome = b.result(0)
    ran_b = any(r[1] == 4 for r in link.ran)
    reused = outcome.sequence == a_seq
    return reused and not ran_b, (
        f"新命令 B 的 SEQ {'等于' if reused else '不等于'} A 的；B 结论 {outcome.status}，设备执行 B={ran_b}"
    )


def a4_queued() -> tuple[bool, str]:
    """A4: a request queued (not sent) behind a confirming one is not protected; a forced one may take its id."""

    link = Link()
    controller = link.controller
    link.up_ok = False
    controller.request(1, 5, 1)
    link.run(1.0)
    controller.request(1, 6, 1)  # waits behind X, never sent yet
    queued_seq = controller._queues[1][1].frame.sequence
    link.run(100.0)  # X confirming
    for _ in range(600):  # the counter turns while it waits (hours of rounds, keepalives, probes)
        if controller._id_ledger.next_sequence[1] == queued_seq:
            break
        controller.allocate_id(1)
    try:
        controller.request(1, 7, 2, force=True)
    except NodeConfirming:
        return False, "force 被拒"
    sequences = [item.frame.sequence for item in controller._queues[1]]
    clash = sequences.count(queued_seq) > 1
    return clash, f"排队未发的命令 SEQ {queued_seq}；force 的新命令{'拿到了同一编号' if clash else '编号不同'}（队列 SEQ {sequences}）"


def a5() -> tuple[bool, str]:
    """A5: a RESULT for a parked (confirming) request is acknowledged and thrown away."""

    link = Link(action_time=lambda frame: 5.0 if frame.action == 5 else 0.0)
    controller = link.controller
    link.up_ok = False
    x = controller.request(1, 5, 1)  # ACCEPTED and RESULT lost
    link.run(0.2)
    # Queries must not reach the device: one answered with the final result
    # closes the record and the RESULTs stop (by design). The forced one
    # cannot get through either.
    link.down_ok = False
    link.run(100.0)
    controller.request(1, 6, 1, force=True)  # X parked
    link.up_ok = True
    link.run(80.0)  # X's next RESULT copy (t ~ 155 s) reaches the controller
    sent = link.device.stats["results_sent"]
    settled = x.done() and x.result(0).status == "done"
    return not settled, f"设备发出 RESULT {sent} 份；控制端给 X 下结论（已执行）={settled}"


def a6() -> tuple[bool, str]:
    """A6: a query that meets a short action mid-run gets ACCEPTED, but no RESULT follows."""

    link = Link(action_time=0.4, timeout=0.2, max_retries=0)
    controller = link.controller
    x = controller.request(1, 5, 1)
    link.run(1.0)
    outcome = x.result(0)
    for value in range(4):  # four more commands within the minute push the closed record out
        controller.request(1, 6, value)
        link.run(2.0)
    link.run(120.0)  # the completion check asks
    completion = outcome.completion
    result = completion.result(0) if completion is not None and completion.done() else "未到"
    return result is None, f"X 结论 {outcome.status}；动作结果：{result}（None = 再也得不到）"


def b1() -> tuple[bool, str]:
    """B1: after 16 other REQUEST exchanges a relay (network key only) forges a confirming command's answer."""

    _, network, controller, run = mesh({1: (80.0, 0.0), 2: (160.0, 0.0), 3: (80.0, 60.0)})
    gateway = network.sessions
    network.set_power(2, False)
    x = gateway.request(2, 5, 1)
    x_seq, x_epoch = controller._queues[2][0].frame.sequence, controller.epoch
    run(130.0)
    for value in range(16):
        gateway.request(3, 4, value)
        run(6.0)
    fcnt = network.endpoints[2]._transport.fcnt + 100
    forged = seal(AEAD, ReplyFrame(2, G, x_seq, 0, epoch=x_epoch).encode(), fcnt, tx=1)  # relay 1 forges "OK"
    network.relays[G]._on_radio(forged, -80.0)
    run(1.0)
    status = x.result(0).status if x.done() else "确认中"
    dropped = network.gateway.stats["app_missing"]
    return status == "done", f"伪造的明文回答之后 X 的结论：{status}（app_missing={dropped}）"


def b2() -> tuple[bool, str]:
    """B2 option 2 (user's decision): a WAKE_NET every 4 min (LEASE 255 s) no longer keeps a node awake."""

    from cairnmesh.wake import WakeAgent

    clock = [0.0]
    radio = Radio()
    relay = MeshRelay(radio, KEY, 5, clock=lambda: clock[0], rng=random.Random(1))
    agent = WakeAgent(relay, Radio(), 5, clock=lambda: clock[0])
    awake = 0.0
    fcnt = 1_790_000_000
    sequence = 0
    while clock[0] < 7200.0:
        if clock[0] % 240.0 < 0.5:
            sequence += 1
            fcnt += 1
            radio.receiver(seal(AEAD, WakeNetFrame(G, sequence & 0xFF, 255).encode(), fcnt, tx=G), -80.0)
        relay.tick(clock[0])
        agent.tick(clock[0])
        if clock[0] >= 3600.0 and agent.awake:
            awake += 0.5
        clock[0] += 0.5
    return awake > 900.0, f"每 4 min 一帧 WAKE_NET（LEASE 255 s）：第 2 小时醒着 {awake / 60:.1f} min（限额 10 min/h）"


def b2_silence() -> tuple[bool, str]:
    """B2 known risk (user: options 3/4 later): one forged frame with FCNT near 2^32 silences the gateway at a relay."""

    clock = [0.0]
    radio = Radio()
    relay = MeshRelay(radio, KEY, 5, clock=lambda: clock[0], rng=random.Random(1))

    def relayed_after(plain: bytes, fcnt: int) -> int:
        before = len(radio.sent)
        radio.receiver(seal(AEAD, plain, fcnt, tx=G), -80.0)
        for _ in range(400):
            clock[0] += 0.005
            relay.tick(clock[0])
        return len(radio.sent) - before

    first = relayed_after(WakeNetFrame(G, 1, 20).encode(), 1_790_000_000)
    radio.receiver(seal(AEAD, HelloFrame(G, 0, NodeState.READY).encode(), 0xFFFFFFFF, tx=G), -80.0)
    second = relayed_after(WakeNetFrame(G, 2, 20).encode(), 1_790_000_100)
    return second == 0, f"网关的泛洪：伪造帧之前转发 {first} 份，之后 {second} 份（已知风险，写入 spec 15.4；修法见方案选项 3、4）"


def b3() -> tuple[bool, str]:
    """B3: the secure layer's state file is replaced without fsync."""

    calls: list[int] = []
    real = os.fsync

    def counting(fd: int) -> None:
        calls.append(fd)
        real(fd)

    os.fsync = counting
    try:
        with tempfile.TemporaryDirectory() as directory:
            SecureTransport(Null(), KEY, state_file=os.path.join(directory, "secure.json"))
    finally:
        os.fsync = real
    return len(calls) < 2, f"写一次状态文件调用 fsync {len(calls)} 次（文件 + 目录应为 2）"


def b4() -> tuple[bool, str]:
    """B4: authenticated but malformed frames raise out of the receive path."""

    from cairnmesh.wake import WakeAgent

    raised = []

    def attempt(name, call) -> None:
        try:
            call()
        except Exception as exc:  # noqa: BLE001 - that is what is being looked for
            raised.append(f"{name}: {type(exc).__name__}")

    radio = Radio()
    secure = SecureTransport(radio, KEY, initial_fcnt=0, app=AppKeys({1: bytes(16)}))
    secure.set_receiver(lambda plain: None)
    attempt("secure", lambda: radio.receiver(seal(AEAD, bytes([FrameKind.REPLY | ROUTED, 5, G, 0x1F]), 10)))
    relay_radio = Radio()
    MeshRelay(relay_radio, KEY, 5, clock=lambda: 0.0, rng=random.Random(1))
    keepalive = bytes([FrameKind.KEEPALIVE | ROUTED, G, 9, 1, 7, 60, 0x07, 5, 1])  # RADIO says SF 14
    attempt("keepalive", lambda: relay_radio.receiver(seal(AEAD, keepalive, 11), -80.0))
    trace = bytes([FrameKind.TRACE | ROUTED, G, 9, 1, 7, 5, 1])  # body two bytes short
    attempt("trace", lambda: relay_radio.receiver(seal(AEAD, trace, 12), -80.0))
    wake_radio = Radio()
    wake_relay = MeshRelay(wake_radio, KEY, 6, clock=lambda: 0.0, rng=random.Random(1))
    WakeAgent(wake_relay, Radio(), 6, clock=lambda: 0.0, dormant=False)
    probe = bytes([FrameKind.PROBE, G, 9, 1, 7, G])  # no WINDOW byte
    attempt("wake", lambda: wake_radio.receiver(seal(AEAD, probe, 13), -80.0))
    return bool(raised), "抛异常：" + ("、".join(raised) if raised else "无")


def c1() -> tuple[bool, str]:
    """C1 (user's decision, wire format v3): with the app layer on, REPORT suppression still fires."""

    grid = {r * 4 + c + 1: (60.0 * c + 60.0, 60.0 * r) for r in range(3) for c in range(4)}

    def trial(app: bool, seed: int) -> tuple[int, int]:
        clock = Clock()
        network = MeshNetwork(
            grid, key=KEY, clock=clock, airtime=airtime_for(7), hello_interval=10.0, seed=seed,
            signal=None, app_keys=app,
        )
        controller = Controller(network.gateway, clock=clock)

        def run(seconds: float) -> None:
            end = clock.now + seconds
            while clock.now < end:
                clock.now += 0.01
                network.tick(clock.now)
                controller.tick(clock.now)

        run(120.0)
        network.set_power(6, False)  # node 6 has 8 neighbours on this grid
        run(200.0)
        watches = network.watches.values()
        return sum(w.stats["reports_sent"] for w in watches), sum(w.stats["reports_suppressed"] for w in watches)

    plain = [trial(False, seed) for seed in range(2)]
    sealed = [trial(True, seed) for seed in range(2)]
    broken = all(suppressed == 0 for _, suppressed in sealed) and sum(s for s, _ in sealed) > 2 * sum(
        s for s, _ in plain
    )
    return broken, f"（REPORT 发送, 被抑制）每种子：关应用层 {plain}，开应用层 {sealed}"


def d1() -> tuple[bool, str]:
    """D1: no controller state file and no warning (the default of LinkSession.serial)."""

    controller = Controller(Null())
    return controller.state_warning is None, (
        f"EPOCH 由时钟推出 = {controller.epoch}，state_warning = {controller.state_warning!r}"
    )


def d1mc() -> tuple[bool, str]:
    """D1 estimate: chance a restart without the state file sends a command a device takes for a repeat."""

    rng = random.Random(1)

    def hit(nodes: int, per_node: int, held: int = 4) -> bool:
        for _ in range(nodes):
            records = {(rng.randrange(256), rng.randrange(256)) for _ in range(held)}
            epoch, start = rng.randrange(256), rng.randrange(256)
            if any((epoch, (start + k) & 0xFF) in records for k in range(per_node)):
                return True
        return False

    rates = []
    for per_node in (5, 20, 50):
        trials = 20_000
        rates.append(f"{per_node} 条/节点 {sum(hit(40, per_node) for _ in range(trials)) / trials:.1%}")
    return False, "40 节点，每次无状态文件重启出错概率：" + "、".join(rates) + "（估计值，不判定）"


def d2() -> tuple[bool, str]:
    """D2: snapshots are written outside the lock; an older one written last wins."""

    with tempfile.TemporaryDirectory() as directory:
        path = os.path.join(directory, "controller.json")
        controller = Controller(Null(), state_file=path, epoch=1)
        with controller._lock:
            older = controller._snapshot()
        controller.request(5, 1, 0)  # its id goes into the state
        with controller._lock:
            newer = controller._snapshot()
        controller._apply(_SideEffects(state=newer))
        controller._apply(_SideEffects(state=older))  # the slower thread finishes last
        with open(path, encoding="utf-8") as handle:
            kept = json.load(handle)
    stale = kept.get("request_ids", {}) != newer.get("request_ids", {})
    return stale, f"后写的旧快照{'覆盖了' if stale else '没有覆盖'}新快照（新命令的编号{'丢了' if stale else '还在'}）"


def d3() -> tuple[bool, str]:
    """D3: a WAKE waits behind a confirming request for as long as that one is confirming."""

    link = Link()
    link.up_ok = False
    link.controller.request(1, 5, 1)
    link.run(100.0)
    link.up_ok = True
    wake = link.controller.wake(1)
    link.run(60.0)
    return not wake.done(), f"60 s 后 WAKE 完成={wake.done()}"


def d5() -> tuple[bool, str]:
    """D5: the serial session cannot carry application keys."""

    from cairnmesh.session import LinkSession

    has = any("app" in name for name in inspect.signature(LinkSession.serial).parameters)
    return not has, f"LinkSession.serial 有应用密钥参数={has}"


CASES = {
    "a1": a1,
    "a2": a2_retry,
    "a2b": a2_not_run,
    "a2c": a2_refused,
    "a2d": a2_force_open,
    "a3": a3,
    "a4": a4,
    "a4b": a4_queued,
    "a5": a5,
    "a6": a6,
    "b1": b1,
    "b2": b2,
    "b2s": b2_silence,
    "b3": b3,
    "b4": b4,
    "c1": c1,
    "d1": d1,
    "d1mc": d1mc,
    "d2": d2,
    "d3": d3,
    "d5": d5,
}


def main(names: list[str]) -> None:
    for name in names or list(CASES):
        present, detail = CASES[name]()
        label = "估计" if name == "d1mc" else "已知风险" if name == "b2s" else ("缺陷仍在" if present else "已修复")
        print(f"{name:5s} {label}  {detail}", flush=True)


if __name__ == "__main__":
    unknown = [name for name in sys.argv[1:] if name not in CASES]
    if unknown:
        sys.exit(f"unknown case(s): {', '.join(unknown)}; known: {', '.join(CASES)}")
    main(sys.argv[1:])
