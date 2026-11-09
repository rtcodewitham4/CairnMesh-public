"""Layer 3 (Q21, 2026-10-21): a command is never left uncertain while its node can still be reached."""

from __future__ import annotations

from cairnmesh.device import DEFAULT_RECORD_KEEP, Device
from cairnmesh.frames import QUERY_ACTION, ReplyResult, RequestFrame
from cairnmesh.channel import SimulatedMedium

from lora_helpers import FakeClock

G = 0x42
OTHER = 0x43


def device_at(clock: FakeClock) -> Device:
    return Device(SimulatedMedium(clock=clock).node(1), 1, awake=True, clock=clock)


def request(sequence: int, sender: int = G) -> RequestFrame:
    return RequestFrame(sender_id=sender, target_id=1, sequence=sequence, action=1, value=0)


def query(sequence: int, own: int = 200) -> RequestFrame:
    return RequestFrame(sender_id=G, target_id=1, sequence=own, action=QUERY_ACTION, value=sequence)


# ---------------------------------------------------------------- device side


def test_a_record_outlives_the_old_two_minute_expiry():
    clock = FakeClock()
    device = device_at(clock)
    device.handle_frame(request(7))
    clock.now += 6 * 24 * 3600.0  # node unreachable for six days

    assert device.handle_frame(query(7)).result == ReplyResult.OK
    assert device.handle_frame(request(7)).result == ReplyResult.OK
    assert device.handled_count == 1


def test_a_record_expires_after_the_keep_period():
    clock = FakeClock()
    device = device_at(clock)
    device.handle_frame(request(7))
    clock.now += DEFAULT_RECORD_KEEP + 1.0

    assert device.handle_frame(query(7)).result == ReplyResult.NOT_RUN


def test_the_keep_period_restarts_at_boot():
    clock = FakeClock()
    device = device_at(clock)
    device.handle_frame(request(7))
    clock.now += DEFAULT_RECORD_KEEP - 10.0
    device.reset()  # no RTC: read back at boot counts as written at boot
    clock.now += 20.0

    assert device.handle_frame(query(7)).result == ReplyResult.OK


def test_four_newer_requests_push_a_record_out_three_do_not():
    clock = FakeClock()
    device = device_at(clock)
    device.handle_frame(request(7))
    for sequence in (8, 9, 10):
        device.handle_frame(request(sequence))
    assert device.handle_frame(query(7)).result == ReplyResult.OK

    device.handle_frame(request(11))
    assert device.handle_frame(query(7)).result == ReplyResult.NOT_RUN


def test_another_sender_cannot_push_a_record_out():
    clock = FakeClock()
    device = device_at(clock)
    device.handle_frame(request(7))
    for sequence in range(20):
        device.handle_frame(request(sequence, sender=OTHER))

    assert device.handle_frame(query(7)).result == ReplyResult.OK
    assert device.handle_frame(request(7)).result == ReplyResult.OK
    assert device.handled_count == 21


def test_the_sender_heard_longest_ago_is_forgotten_first():
    clock = FakeClock()
    device = Device(SimulatedMedium(clock=clock).node(1), 1, awake=True, max_senders=2, clock=clock)
    device.handle_frame(request(1, sender=0x50))
    device.handle_frame(request(7))
    device.handle_frame(request(1, sender=OTHER))  # third sender: 0x50 goes, G stays

    assert set(device._recent) == {G, OTHER}
    assert device.last_sequence == 1


# ------------------------------------------------------------ controller side

import pytest  # noqa: E402

from cairnmesh.controller import (  # noqa: E402
    DONE,
    MAX_PAST_CONFIRMING,
    NOT_RUN,
    UNCONFIRMED,
    Controller,
    NodeConfirming,
)
from cairnmesh.events import ReplyReceived, RequestConfirming, RequestFailed  # noqa: E402

from lora_helpers import build, run  # noqa: E402

FAST = {"confirm_window": 5.0, "confirm_schedule": (10.0, 60.0, 30.0)}


def link(state_file=None, **medium_options):
    """``build`` with a controller that starts confirming after 5 s and asks every 10 s, then 30 s."""

    clock, medium, devices, _, _ = build(**medium_options)
    return clock, medium, devices, controller_on(clock, medium, state_file)


def controller_on(clock, medium, state_file=None, **options):
    controller = Controller(
        medium.gateway, timeout=0.5, max_retries=3, clock=clock, state_file=state_file, **{**FAST, **options}
    )
    controller.events_seen = []
    controller.events.subscribe(controller.events_seen.append)
    return controller


def go(clock, medium, devices, controller, seconds):
    run(clock, medium, devices, controller, seconds, step=0.05)


def test_an_unreachable_node_is_confirmed_once_it_is_back():
    clock, medium, devices, controller = link(uplink_drop=1.0)
    future = controller.request(1, action=1, value=0)
    go(clock, medium, devices, controller, 300.0)  # five minutes: every reply lost
    assert not future.done()
    assert [e for e in controller.events_seen if isinstance(e, RequestConfirming)]
    assert controller.confirming()[0].rounds >= 5

    medium.uplink_drop = 0.0
    go(clock, medium, devices, controller, 40.0)
    outcome = future.result(timeout=0)
    assert outcome.status == DONE and outcome.ok and outcome.queried
    assert devices[1].handled_count == 1
    assert any(isinstance(e, ReplyReceived) and e.queried for e in controller.events_seen)
    assert controller.confirming() == []


def test_a_request_that_never_arrived_is_confirmed_not_run_and_never_runs_later():
    clock, medium, devices, controller = link(downlink_drop=1.0)
    future = controller.request(1, action=1, value=0)
    go(clock, medium, devices, controller, 120.0)
    assert not future.done()

    medium.downlink_drop = 0.0
    go(clock, medium, devices, controller, 40.0)
    assert future.result(timeout=0).status == NOT_RUN
    late = RequestFrame(sender_id=controller.sender_id, target_id=1, sequence=future.result().sequence,
                        epoch=controller.epoch, action=1, value=0)
    assert devices[1].handle_frame(late).result == ReplyResult.NOT_RUN  # a late copy is refused
    assert devices[1].handled_count == 0


def test_hearing_the_node_asks_at_once():
    clock, medium, devices, controller = link(uplink_drop=1.0)
    controller.confirm.schedule = (1000.0, 6000.0, 1000.0)
    future = controller.request(1, action=1, value=0)
    go(clock, medium, devices, controller, 20.0)
    assert controller.confirming()[0].next_query > clock.now + 900.0

    medium.uplink_drop = 0.0
    controller.nudge(1)
    go(clock, medium, devices, controller, 15.0)
    assert future.result(timeout=0).status == DONE


def test_a_new_request_is_refused_while_one_is_confirming_unless_forced():
    clock, medium, devices, controller = link(uplink_drop=1.0)
    first = controller.request(1, action=1, value=0)
    go(clock, medium, devices, controller, 20.0)
    with pytest.raises(NodeConfirming):
        controller.request(1, action=1, value=1)
    controller.wake(1)  # a WAKE enters no record: allowed

    medium.uplink_drop = 0.0
    forced = [controller.request(1, action=1, value=v, force=True) for v in range(MAX_PAST_CONFIRMING)]
    with pytest.raises(NodeConfirming):  # a fourth would push the record out of the device
        controller.request(1, action=1, value=9, force=True)
    go(clock, medium, devices, controller, 30.0)
    assert all(f.result(timeout=0).ok for f in forced)
    assert first.result(timeout=0).status == DONE  # asked again once the forced ones were done
    assert devices[1].handled_count == 1 + MAX_PAST_CONFIRMING


def test_the_operator_may_abandon_and_that_is_recorded():
    clock, medium, devices, controller = link(uplink_drop=1.0)
    future = controller.request(1, action=1, value=0)
    go(clock, medium, devices, controller, 20.0)
    assert controller.abandon(1) == 1
    outcome = future.result(timeout=0)
    assert outcome.status == UNCONFIRMED and "操作员放弃" in outcome.reason
    assert [e for e in controller.events_seen if isinstance(e, RequestFailed) and e.abandoned]
    assert controller.request(1, action=1, value=1)  # the node is free again


def test_asking_stops_before_the_devices_may_have_forgotten():
    clock, medium, devices, controller = link(uplink_drop=1.0)
    controller.confirm.record_keep = 200.0
    future = controller.request(1, action=1, value=0)
    go(clock, medium, devices, controller, 200.0)
    outcome = future.result(timeout=0)
    assert outcome.status == UNCONFIRMED and "记忆期限" in outcome.reason
    assert 0.95 * 200.0 <= outcome.elapsed < 200.0


def test_request_ids_never_repeat_one_the_device_still_keeps():
    clock, medium, devices, controller = link()
    controller.confirm.record_keep = None
    rng = __import__("random").Random(3)
    for value in range(300):
        for _ in range(rng.randrange(400)):  # keepalives and probes turn the counter many times
            controller.allocate_id(1)
        future = controller.request(1, action=1, value=value % 256)
        go(clock, medium, devices, controller, 0.2)
        assert future.result(timeout=0).ok
    assert devices[1].handled_count == 300  # none was taken for a repeat


def test_a_restart_with_the_state_file_resumes_confirming(tmp_path):
    state = str(tmp_path / "controller.json")
    clock, medium, devices, first = link(state_file=state, uplink_drop=1.0)
    assert first.state_warning is not None  # no file yet
    future = first.request(1, action=1, value=0)
    go(clock, medium, devices, first, 20.0)
    assert first.confirming()

    second = controller_on(clock, medium, state)  # takes the medium over: a restart
    assert second.state_warning is None
    assert second.epoch == (first.epoch + 1) & 0xFF
    assert [(c.sequence, c.epoch) for c in second.confirming()] == [(future_seq(first), first.epoch)]
    medium.uplink_drop = 0.0
    go(clock, medium, devices, second, 20.0)
    done = [e for e in second.events_seen if isinstance(e, ReplyReceived)]
    assert done and done[0].ok and done[0].queried
    assert second.confirming() == [] and devices[1].handled_count == 1
    third = controller_on(clock, medium, state)
    assert third.confirming() == [] and third.epoch == (second.epoch + 1) & 0xFF


def future_seq(controller):
    return controller.confirming()[0].sequence


def test_a_restart_keeps_request_ids_apart_in_a_repeated_epoch(tmp_path):
    state = str(tmp_path / "controller.json")
    clock, medium, devices, first = link(state_file=state)
    first.request(1, action=1, value=0)
    go(clock, medium, devices, first, 0.5)
    sent = first._id_ledger.request_ids[1][-1]
    second = controller_on(clock, medium, state, epoch=first.epoch, initial_sequence=sent & 0xFF)
    frame = second.build_request(1, 0, 1)
    assert frame.message_id != sent  # same epoch forced: the saved id is still skipped


# ------------------------------------------------- review fixes (2026-10-21)


def test_a_crash_before_confirming_still_asks_after_the_restart(tmp_path):
    state = str(tmp_path / "controller.json")
    clock, medium, devices, first = link(state_file=state, uplink_drop=1.0)
    first.request(1, action=1, value=0)
    go(clock, medium, devices, first, 1.0)  # on the air, still retrying: not yet confirming
    assert not first.confirming()

    second = controller_on(clock, medium, state)
    assert [c.epoch for c in second.confirming()] == [first.epoch]
    medium.uplink_drop = 0.0
    go(clock, medium, devices, second, 15.0)
    done = [e for e in second.events_seen if isinstance(e, ReplyReceived)]
    assert done and done[0].ok and devices[1].handled_count == 1


def test_a_finished_request_leaves_the_state_file(tmp_path):
    state = str(tmp_path / "controller.json")
    clock, medium, devices, first = link(state_file=state)
    first.request(1, action=1, value=0)
    go(clock, medium, devices, first, 0.5)
    assert controller_on(clock, medium, state).confirming() == []


def test_a_query_after_a_restart_skips_the_old_epochs_ids(tmp_path):
    state = str(tmp_path / "controller.json")
    clock, medium, devices, first = link(state_file=state)
    for value in range(3):
        first.request(1, action=1, value=value)
        go(clock, medium, devices, first, 0.3)
    medium.uplink_drop = 1.0
    first.request(1, action=1, value=9)
    go(clock, medium, devices, first, 1.0)
    old = {ident & 0xFF for ident in first._id_ledger.request_ids[1] if ident >> 8 == first.epoch}

    # The new run's counter starts right on the old ids: the query must step over them.
    second = controller_on(clock, medium, state, initial_sequence=min(old))
    second._id_ledger.next_sequence[1] = min(old)
    go(clock, medium, devices, second, 1.0)
    pending = second._queues[1][0]
    assert pending.query_sequences and not set(pending.query_sequences) & old


def test_a_round_takes_its_timing_from_the_gateway():
    clock, medium, devices, controller = link(uplink_drop=1.0)
    controller.timeouts_for = lambda node: (1.5, 1)
    controller.request(1, action=1, value=0)
    go(clock, medium, devices, controller, 25.0)  # past the window: rounds have started
    pending = controller._queues[1][0]
    assert pending.confirming and (pending.timeout, pending.max_retries) == (1.5, 1)


def test_only_a_wake_join_puts_every_confirming_request_on_the_fast_pace():
    pytest.importorskip("cryptography")
    from cairnmesh.frames import PROBE_WAKE, JoinFrame
    from cairnmesh.mesh_sim import MeshNetwork, line

    network = MeshNetwork(line(2), key=bytes(range(16)), seed=1, gateway_options={})
    calls = []

    class Stub:
        def nudge(self, node):
            calls.append(("nudge", node))

        def nudge_all(self, fresh=True):
            calls.append(("all", fresh))

    network.sessions.controller = Stub()
    network.sessions._on_heard(JoinFrame(2, 0x42, 5, 2, 10).encode(), -90.0)
    assert calls == [("nudge", 2)]  # a rejoin: only that node
    calls.clear()
    network.sessions._on_heard(JoinFrame(2, 0x42, 6, 2, 10 | PROBE_WAKE).encode(), -90.0)
    assert ("all", True) in calls and ("nudge", 2) in calls


def test_a_request_sent_past_a_confirming_one_never_takes_its_query_ids():
    # 2026-10-21 review: the counter (shared with the gateway's keepalives and
    # probes) came round to X's query id; R sent with ``force`` took it, and a
    # late copy of R's answer then resolved X, never run, as DONE.
    from cairnmesh.controller import NOT_RUN
    from cairnmesh.frames import ReplyFrame
    from lora_helpers import build, run

    clock, medium, devices, controller, _ = build(downlink_drop=1.0)
    x = controller.request(1, action=1, value=0)
    run(clock, medium, devices, controller, 95.0)  # retries, queries, CONFIRMING
    head = controller._queues[1][0]
    assert head.confirming
    asked = list(head.query_sequences)
    for _ in range(600):  # what keepalives and probes do (they skip X's ids too now: review A1)
        if controller._id_ledger.next_sequence[1] == asked[0]:
            break
        controller.allocate_id(1)
    medium.downlink_drop = 0.0
    r = controller.request(1, action=2, value=7, force=True)
    r_sequence = controller._queues[1][0].frame.sequence
    assert r_sequence not in asked
    run(clock, medium, devices, controller, 1.0)
    assert r.result(timeout=0).ok and controller._queues[1][0] is head

    late = ReplyFrame(sender_id=1, target_id=controller.sender_id, sequence=r_sequence, epoch=controller.epoch, result=ReplyResult.OK)
    controller.on_frame(late.encode())  # a late copy of R's answer
    assert not x.done()
    run(clock, medium, devices, controller, 30.0)  # X's next round asks the device
    assert x.result(timeout=0).status == NOT_RUN and devices[1].handled_count == 1


def test_entering_confirming_does_not_burn_a_query_id():
    from lora_helpers import build, run

    clock, medium, devices, controller, _ = build(downlink_drop=1.0)
    controller.confirm.window = 1.0  # retries end past the window: CONFIRMING straight away
    controller.request(1, action=1, value=0)
    run(clock, medium, devices, controller, 3.0)
    head = controller._queues[1][0]
    assert head.confirming and head.rounds == 0
    made = list(head.query_sequences)
    clock.now = head.deadline
    run(clock, medium, devices, controller, 0.05)  # the first round
    assert head.rounds == 1 and list(head.query_sequences) == made
