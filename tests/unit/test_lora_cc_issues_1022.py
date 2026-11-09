"""Regression tests for GitHub issues #3 and #7 (2026-10-22).

#3: commands abandoned while a node was offline pushed the ids it still keeps
out of the controller's skip list; after a wrap a new command took one, and
the device answered it with the old result, never run.

#7: frames with a correct MIC but contradicting themselves (fuzzing) threw
out of the main loop: a scan drew an id for 0xFF, a shorter second routed
copy indexed past its route, a probe answer built an invalid return route.
They are dropped and counted now.
"""

from __future__ import annotations

from cairnmesh import controller as C
from cairnmesh.frames import (
    BROADCAST_ID,
    LinkQuality,
    ProbeFrame,
    ProbeHop,
    RequestFrame,
    add_route,
)
from cairnmesh.mesh import _Done, _Probe

from test_lora_cc_review_1021 import TRIANGLE, Link, mesh, ran_list


# ------------------------------------------------------------------ id reuse


def offline_link():
    link = Link()
    ran = ran_list(link.device)
    controller = link.controller

    def command(action, value, budget=30.0):
        future = controller.request(1, action, value)
        link.run(budget)
        return future

    return link, controller, ran, command


def test_a_new_command_after_an_offline_wrap_runs():
    link, controller, ran, command = offline_link()
    for value in range(4):  # the device keeps these four (one EPOCH, consecutive ids)
        assert command(1, value).result(0).ok
    kept = {mid for mid, *_ in link.device._recent[controller.sender_id]}
    link.down_ok = False
    abandoned = 0
    while abandoned < 13 or controller._id_ledger.next_sequence[1] > 3:  # until the counter is back on them
        command(2, abandoned, 400.0)
        controller.abandon(1)
        link.run(1.0)
        abandoned += 1
        assert abandoned < 200
    link.down_ok = True
    outcome = command(3, 99).result(0)
    assert outcome.ok and (controller.epoch << 8 | outcome.sequence) not in kept
    assert ran[-1][1:] == (3, 99)  # really run, not answered from the old record


def test_ids_the_device_no_longer_keeps_are_released():
    link, controller, ran, command = offline_link()
    for value in range(20):
        assert command(1, value).result(0).ok
    # Settled in order: only the last DEDUP_WINDOW can still be on the device.
    assert len(controller._id_ledger.request_ids[1]) == C.DEDUP_WINDOW


def test_too_many_unsettled_ids_move_the_epoch_on():
    link, controller, ran, command = offline_link()
    epoch = controller.epoch
    link.down_ok = False
    for value in range(C.KEPT_IDS_LIMIT + 5):
        command(2, value & 0xFF, 400.0)  # retries, then confirming
        assert controller.abandon(1) == 1
    assert controller.epoch_moves >= 1 and controller.epoch != epoch
    link.down_ok = True
    assert command(3, 7).result(0).ok and ran[-1][1:] == (3, 7)


def test_the_state_file_keeps_which_ids_are_settled(tmp_path):
    from test_lora_cc_layer3 import controller_on, go, link

    state = str(tmp_path / "controller.json")
    clock, medium, devices, first = link(state_file=state)
    for value in range(6):
        first.request(1, action=1, value=value)
        go(clock, medium, devices, first, 0.5)
    second = controller_on(clock, medium, state)
    assert second._id_ledger.request_ids[1] == first._id_ledger.request_ids[1]
    assert second._id_ledger.closed_ids[1] == first._id_ledger.closed_ids[1]


# ------------------------------------------------------------------ scan and route bookkeeping


def test_a_scan_never_takes_broadcast_or_the_gateway_for_a_node():
    _, network, controller, run = mesh(TRIANGLE)
    gateway = network.sessions
    bad = gateway.stats["bad"]
    gateway._found(BROADCAST_ID, (), (), "scan")
    gateway._found(network.controller_id, (), (), "scan")
    assert BROADCAST_ID not in gateway.nodes and network.controller_id not in gateway.nodes
    assert gateway.stats["bad"] == bad + 2
    gateway._found(2, (), (), "scan", neighbours=(ProbeHop(BROADCAST_ID, LinkQuality(-80, 5)),))
    assert all(hop.node_id != BROADCAST_ID for hop in gateway.nodes[2].neighbours)
    gateway.scan()
    run(120.0)  # the scan runs to its end without asking 0xFF


def test_a_shorter_second_routed_copy_is_dropped():
    _, network, controller, run = mesh(TRIANGLE)
    relay = network.relays[3]
    # Route G, 2, 3, 1: relay 3 hears relay 2 repeat and would acknowledge with
    # the copy of the furthest node it knows has the frame.
    plain = add_route(RequestFrame(network.controller_id, 1, 5, action=1, value=0, epoch=1).encode(), (2, 3))
    fcnt = 77
    ident = (network.controller_id, fcnt)
    relay._done[ident] = _Done(furthest=5)  # recorded from a longer copy under the same counter
    before = relay.stats["bad_route"]
    relay._on_routed_copy(ident, plain, fcnt, 2, -80.0)
    assert relay.stats["bad_route"] == before + 1
    assert not relay._flights


def test_a_probe_with_no_valid_way_back_is_dropped():
    _, network, controller, run = mesh(TRIANGLE)
    relay = network.relays[3]
    link = LinkQuality(-80, 5)
    # Names relay 3 among its hops (allowed for a broadcast target): the way back would pass through 3 itself.
    frame = ProbeFrame(3, BROADCAST_ID, 9, network.controller_id, 1, hops=(ProbeHop(2, link), ProbeHop(3, link)))
    before = relay.stats["bad_route"]
    relay._pass_probe(_Probe(frame, (link, link, link), 0.0, 1.0, target=True))
    assert relay.stats["bad_route"] == before + 1
    assert relay.stats["probes_answered"] == 0
