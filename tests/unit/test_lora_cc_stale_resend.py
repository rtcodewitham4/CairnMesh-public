"""The gateway never sends again a command that is already over.

Found by stress_bench (dormant network, 30 % loss): a command held in
``session.waiting`` over a path break was answered by flood, the copy
stayed held, and the next session opening sent it again, minutes later,
past the device's dedup window: the command ran twice.
"""

from __future__ import annotations

import pytest

pytest.importorskip("cryptography")

from cairnmesh.frames import ReplyFrame, ReplyResult, RequestFrame

from _session_helpers import LINE, build, open_session, run

QUIET = {"auto_trace": False, "idle_timeout": None}


def answered_command():
    clock, network, controller = build(LINE, seed=2, gateway_options=QUIET)
    session = open_session(clock, network, controller, 5)
    future = network.sessions.request(5, 3, 1)
    run(clock, network, controller, 30.0, future.done)
    assert future.result(timeout=0).ok
    delivery = network.sessions.deliveries(5)[-1]
    plain = RequestFrame(
        sender_id=controller.sender_id,
        target_id=5,
        sequence=delivery.sequence,
        epoch=delivery.epoch,
        action=3,
        value=1,
    ).encode()
    key = (5, delivery.epoch << 8 | delivery.sequence)
    return clock, network, controller, session, key, plain


def test_the_controller_says_which_command_is_still_on_the_air():
    clock, network, controller = build(LINE, seed=2, gateway_options=QUIET)
    open_session(clock, network, controller, 5)
    future = network.sessions.request(5, 3, 1)
    run(clock, network, controller, 0.01)
    delivery = network.sessions.deliveries(5)[-1]
    message_id = delivery.epoch << 8 | delivery.sequence
    assert controller.awaiting(5, message_id)
    assert not controller.awaiting(5, message_id + 1)
    run(clock, network, controller, 30.0, future.done)
    assert not controller.awaiting(5, message_id)


def test_a_held_command_answered_meanwhile_is_dropped_not_resent():
    clock, network, controller, session, key, plain = answered_command()
    gateway = network.sessions
    routed = gateway.stats["routed"]
    session.waiting = (key, plain)  # as left by a break while the network slept
    gateway._release_waiting(session)
    assert session.waiting is None
    assert gateway.stats["routed"] == routed
    run(clock, network, controller, 10.0)
    assert network.devices[5].handled_count == 1


def test_a_queued_flood_of_a_finished_command_never_goes_out():
    clock, network, controller, session, key, plain = answered_command()
    gateway = network.sessions
    gateway.close_session(5)  # no path: the copy would go as a flood
    sent = []
    emit = gateway._emit
    gateway._emit = lambda data, radio=0: (sent.append(bytes(data)), emit(data, radio))
    gateway._submit_flood(key, plain)
    run(clock, network, controller, 10.0)  # past the last flood's settle time
    assert gateway._flood is None and not gateway._floods
    assert plain not in sent  # the device's dedup would hide a resend here
    assert network.devices[5].handled_count == 1


def test_an_answer_clears_the_held_copy():
    clock, network, controller, session, key, plain = answered_command()
    session.waiting = (key, plain)
    reply = ReplyFrame(
        sender_id=5,
        target_id=controller.sender_id,
        sequence=plain[3],
        epoch=plain[4],
        result=ReplyResult.OK,
    )
    network.sessions._answered(reply, clock.now, routed=False)
    assert session.waiting is None


# ------------------------------------------- 2026-10-21 review: bookkeeping


def muted_target(network):
    """Node 5 runs every command, but its answers never go out (keepalives still do)."""

    device = network.devices[5]
    real = device.transport.send
    state = {"on": True}
    device.transport.send = lambda data: None if state["on"] else real(data)
    return state


def test_unanswered_queries_leave_no_delivery_behind():
    # Each round asks under a new id; the deliveries of the unanswered ones
    # stayed in ``_open`` for good, and a later command whose id came round
    # was matched to one of them.
    clock, network, controller = build(LINE, seed=3)
    gateway = network.sessions
    open_session(clock, network, controller, 5)
    mute = muted_target(network)
    future = gateway.request(5, 1, 0)
    run(clock, network, controller, 200.0, lambda: controller.confirming() and controller.confirming()[0].rounds >= 2)
    assert controller.confirming()
    assert len([key for key in gateway.book.deliveries if key[0] == 5]) <= 1  # at most the query on the air
    mute["on"] = False
    run(clock, network, controller, 300.0, future.done)
    assert future.result(timeout=0).ok
    assert not [key for key in gateway.book.deliveries if key[0] == 5]


def test_a_finished_command_leaves_no_message_in_flight():
    clock, network, controller = build(LINE, seed=2, gateway_options=QUIET)
    gateway = network.sessions
    session = open_session(clock, network, controller, 5)
    key, plain = (5, controller.epoch << 8 | 0x77), b"\x01\x42\x05\x77" + bytes([controller.epoch, 3, 1])
    gateway.book.dispatch(5, key, plain)  # a command the controller gave up on, its answer never came
    from cairnmesh.events import RequestFailed
    from cairnmesh.frames import FrameKind

    controller.events.publish(RequestFailed(5, 0x77, FrameKind.REQUEST, 3, "重试次数耗尽", epoch=controller.epoch))
    assert 5 not in gateway.book.inflight
    before = session.counts["keepalives"]
    run(clock, network, controller, 2 * gateway.keepalive_interval)
    assert session.counts["keepalives"] > before  # the path is watched again


def test_a_failure_from_another_epoch_leaves_the_current_command_alone():
    # A request restored from before a restart has an older EPOCH; its end
    # must not finish or forget today's command with the same SEQUENCE.
    clock, network, controller = build(LINE, seed=2, gateway_options=QUIET)
    gateway = network.sessions
    open_session(clock, network, controller, 5)
    mute = muted_target(network)
    future = gateway.request(5, 3, 1)
    run(clock, network, controller, 0.2)
    key = next(key for key in gateway.book.deliveries if key[0] == 5)
    from cairnmesh.events import RequestFailed
    from cairnmesh.frames import FrameKind

    old = (controller.epoch - 1) & 0xFF
    controller.events.publish(RequestFailed(5, key[1] & 0xFF, FrameKind.REQUEST, 3, "操作员放弃", epoch=old))
    assert key in gateway.book.deliveries and gateway.book.inflight[5][0] == key
    mute["on"] = False
    run(clock, network, controller, 30.0, future.done)
    assert future.result(timeout=0).ok
