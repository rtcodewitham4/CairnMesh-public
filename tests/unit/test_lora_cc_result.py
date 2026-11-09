"""Commit, then act (2026-10-21, user: plan A): a slow action answers ACCEPTED at once and reports a RESULT later."""

from __future__ import annotations

import pytest

from cairnmesh.channel import SimulatedMedium
from cairnmesh.controller import ACCEPTED, DONE, Controller
from cairnmesh.device import Device
from cairnmesh.events import RequestCompleted
from cairnmesh.frames import (
    QUERY_ACTION,
    FrameKind,
    ReplyFrame,
    ReplyResult,
    RequestFrame,
    ResultFrame,
    add_route,
    decode_frame,
    decode_routed,
)

from lora_helpers import FakeClock, run

G = 0x42


# ------------------------------------------------------------------ frames


def test_the_result_frame_round_trips_and_may_carry_a_route():
    frame = ResultFrame(sender_id=5, target_id=G, sequence=9, result=ReplyResult.ERROR, epoch=7)
    assert frame.encode() == bytes([0x12, 5, G, 9, 7, 0x01])
    assert decode_frame(frame.encode()) == frame
    assert decode_routed(add_route(frame.encode(), (1, 2))) == (frame, (1, 2))
    assert frame.message_id == 7 << 8 | 9
    assert ReplyResult.ACCEPTED == 0x03


# ------------------------------------------------------------- device side


class Pipe:
    def __init__(self) -> None:
        self.sent: list[bytes] = []
        self._receiver = None

    def send(self, data: bytes) -> None:
        self.sent.append(bytes(data))

    def set_receiver(self, callback) -> None:
        self._receiver = callback

    def frames(self, kind):
        return [decode_frame(data) for data in self.sent if data[0] == kind]


def device(action_time=2.0, **options):
    clock = FakeClock()
    pipe = Pipe()
    return clock, pipe, Device(pipe, 1, awake=True, clock=clock, action_time=action_time, **options)


def request(sequence: int, action: int = 1) -> RequestFrame:
    return RequestFrame(sender_id=G, target_id=1, sequence=sequence, epoch=4, action=action, value=0)


def query(sequence: int, own: int = 200) -> RequestFrame:
    return RequestFrame(sender_id=G, target_id=1, sequence=own, epoch=4, action=QUERY_ACTION, value=sequence)


def ack(sequence: int) -> bytes:
    return ReplyFrame(sender_id=G, target_id=1, sequence=sequence, epoch=4, result=ReplyResult.OK).encode()


def test_a_slow_action_answers_accepted_then_reports_its_result():
    clock, pipe, dev = device()
    assert dev.handle_frame(request(7)).result == ReplyResult.ACCEPTED
    assert dev.handle_frame(request(7)).result == ReplyResult.ACCEPTED  # a retry: not run again
    assert dev.handle_frame(query(7)).result == ReplyResult.ACCEPTED  # still running
    assert dev.handled_count == 1 and not pipe.frames(FrameKind.RESULT)

    clock.now += 2.0
    dev.tick()
    (result,) = pipe.frames(FrameKind.RESULT)
    assert (result.sequence, result.epoch, result.result) == (7, 4, ReplyResult.OK)
    assert dev.handle_frame(query(7, own=201)).result == ReplyResult.OK
    assert dev.handle_frame(request(7)).result == ReplyResult.OK
    assert dev.handled_count == 1


def test_the_result_goes_again_until_acknowledged():
    clock, pipe, dev = device(result_timeout=1.0)
    dev.handle_frame(request(7))
    clock.now += 2.0
    dev.tick()
    clock.now += 1.3
    dev.tick()
    assert len(pipe.frames(FrameKind.RESULT)) == 2
    dev.on_frame(ack(7))
    clock.now += 60.0
    dev.tick()
    assert len(pipe.frames(FrameKind.RESULT)) == 2
    assert dev.stats["results_acked"] == 1


def test_a_short_action_is_waited_for_and_sends_no_result():
    clock, pipe, dev = device(action_time=0.3)
    assert dev.handle_frame(request(7)) is None  # the answer waits for the action
    clock.now += 0.3
    dev.tick()
    (reply,) = pipe.frames(FrameKind.REPLY)
    assert reply.result == ReplyResult.OK
    clock.now += 60.0
    dev.tick()
    assert not pipe.frames(FrameKind.RESULT)


def test_open_records_are_never_pushed_out():
    clock, pipe, dev = device()
    dev.handle_frame(request(7))  # running, then its RESULT unacknowledged
    dev.action_time = 0.0
    for sequence in range(8, 16):
        dev.handle_frame(request(sequence))
    assert dev.handle_frame(query(7)).result == ReplyResult.ACCEPTED
    clock.now += 2.0
    dev.tick()
    for sequence in range(16, 24):
        dev.handle_frame(request(sequence))
    assert dev.handle_frame(query(7)).result == ReplyResult.OK  # a query takes the result: closed now


def test_past_max_open_a_slow_request_is_refused_not_run():
    clock, pipe, dev = device(max_open=2)
    assert dev.handle_frame(request(1)).result == ReplyResult.ACCEPTED
    assert dev.handle_frame(request(2)).result == ReplyResult.ACCEPTED
    assert dev.handle_frame(request(3)).result == ReplyResult.NOT_RUN
    assert dev.handle_frame(request(3)).result == ReplyResult.NOT_RUN  # final
    assert dev.handled_count == 2


def test_a_reboot_cuts_the_action_short_it_reports_interrupted_and_never_runs_again():
    clock, pipe, dev = device()
    dev.handle_frame(request(7))
    clock.now += 1.0
    dev.reset()
    clock.now += dev.result_timeout + 1.0
    dev.tick()
    (result,) = pipe.frames(FrameKind.RESULT)
    assert result.result == ReplyResult.INTERRUPTED
    assert dev.handle_frame(request(7)).result == ReplyResult.INTERRUPTED
    assert dev.handled_count == 0  # reset zeroes the counter: nothing ran after the boot


def test_a_retry_answered_with_the_final_result_closes_the_record_like_a_query():
    # STATUS P4: the controller takes a final answer as the record closed. Left
    # open, the record outlived that, and a reused id got its old answer unrun.
    clock, pipe, dev = device()
    dev.handle_frame(request(7))
    clock.now += 1.0
    dev.reset()
    assert dev.handle_frame(request(7)).result == ReplyResult.INTERRUPTED  # the retry after the boot
    clock.now += dev.result_timeout + 1.0
    dev.tick()
    assert pipe.frames(FrameKind.RESULT) == []  # closed: no RESULT to send any more
    assert dev.stats["results_acked"] == 1


# --------------------------------------------------------- controller side


def link(action_time=2.0, **medium_options):
    clock = FakeClock()
    medium = SimulatedMedium(latency=(0.01, 0.01), clock=clock, **medium_options)
    dev = Device(medium.node(1), 1, awake=True, clock=clock, heartbeat_interval=100.0, action_time=action_time)
    controller = Controller(medium.gateway, timeout=0.5, max_retries=3, clock=clock)
    events = []
    controller.events.subscribe(events.append)
    return clock, medium, {1: dev}, controller, events


def test_the_command_has_its_outcome_at_accepted_and_the_result_follows():
    clock, medium, devices, controller, events = link()
    future = controller.request(1, action=1, value=0)
    run(clock, medium, devices, controller, 0.2)
    outcome = future.result(timeout=0)
    assert outcome.status == ACCEPTED and outcome.accepted and not outcome.completion.done()
    nxt = controller.request(1, action=2, value=0)  # the node's queue is free
    run(clock, medium, devices, controller, 3.0)
    assert outcome.completion.result(timeout=0) == ReplyResult.OK
    assert [e.result for e in events if isinstance(e, RequestCompleted)] == [ReplyResult.OK, ReplyResult.OK]
    assert nxt.result(timeout=0).status == ACCEPTED
    assert devices[1].handled_count == 2
    assert devices[1].stats["results_acked"] == 2 and devices[1].stats["results_sent"] == 2


def test_a_result_lost_on_the_way_is_learnt_by_a_query():
    clock, medium, devices, controller, events = link()
    real = medium.node(1).send
    medium.node(1).send = lambda data: None if data[0] == FrameKind.RESULT else real(data)
    future = controller.request(1, action=1, value=0)
    run(clock, medium, devices, controller, 0.2)
    outcome = future.result(timeout=0)
    run(clock, medium, devices, controller, 70.0)
    assert outcome.completion.result(timeout=0) == ReplyResult.OK
    (completed,) = [e for e in events if isinstance(e, RequestCompleted)]
    assert completed.queried and devices[1].handled_count == 1
    assert not devices[1]._results  # the query took it: no more RESULTs


def test_a_result_before_the_lost_accepted_settles_the_command():
    clock, medium, devices, controller, _ = link(action_time=0.8)
    real = medium.node(1).send
    # Every ACCEPTED answer is lost: the RESULT comes while the command still waits.
    medium.node(1).send = lambda data: None if data[0] == FrameKind.REPLY and data[5] == ReplyResult.ACCEPTED else real(data)
    future = controller.request(1, action=1, value=0)
    run(clock, medium, devices, controller, 1.0)
    outcome = future.result(timeout=0)
    assert outcome.status == DONE and outcome.ok and devices[1].handled_count == 1


# ------------------------------------------------------------- multi-hop


@pytest.mark.parametrize("action_time", [1.0, 2.0])
def test_a_slow_action_no_longer_breaks_the_path(action_time):
    # Measured before (answer only once the action was over): every command
    # broke the path at 1 s on 5 hops, 2.9 × the sends.
    pytest.importorskip("cryptography")
    from cairnmesh.mesh_sim import MeshNetwork
    from _session_helpers import KEY, LINE, SF7, open_session
    from _session_helpers import run as mesh_run

    clock = FakeClock()
    network = MeshNetwork(
        LINE, key=KEY, clock=clock, airtime=SF7, gateway_options={}, seed=1, device_options={"action_time": action_time}
    )
    controller = network.controller(network.sessions, clock=clock, timeout=8.0, max_retries=2)
    network.sessions.attach(controller)
    session = open_session(clock, network, controller, 5)
    results = []
    for value in range(5):
        future = network.sessions.request(5, 3, value)
        mesh_run(clock, network, controller, 30.0, future.done)
        outcome = future.result(timeout=0)
        assert outcome.status == ACCEPTED
        mesh_run(clock, network, controller, 30.0, outcome.completion.done)
        results.append(outcome.completion.result(timeout=0))
    mesh_run(clock, network, controller, 5.0)  # the last acknowledgement on its way
    assert results == [ReplyResult.OK] * 5
    assert session.counts["broken"] == 0
    assert network.devices[5].handled_count == 5
    assert network.devices[5].stats["results_acked"] == 5
