"""Skipping idle ticks (``MeshNetwork.skip_idle``) changes nothing: every frame on air is the same, bit for bit.

Guards each component's ``next_due``: a timer it leaves out shows here as
a frame sent at another time, or not at all.
"""

from __future__ import annotations

import pytest

pytest.importorskip("cryptography")

from cairnmesh.airtime import airtime_for
from cairnmesh.mesh_sim import MeshNetwork, line

KEY = bytes(range(16))


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def frames(skip: bool) -> tuple[list, list]:
    clock = Clock()
    network = MeshNetwork(
        line(5), key=KEY, clock=clock, airtime=airtime_for(7), seed=4, drop=0.2, record=True,
        hello_interval=10.0, wake_options={"period": 0.5}, discovery_options={},
        gateway_options={"idle_timeout": 30.0, "lease": 15},
    )
    network.skip_idle = skip
    controller = network.controller(network.sessions, clock=clock, timeout=8.0, max_retries=2)
    network.sessions.attach(controller)
    outcomes = []

    def run(seconds: float) -> None:
        end = clock.now + seconds
        while clock.now < end:
            clock.now += 0.005
            network.tick(clock.now)
            controller.tick(clock.now)

    run(40.0)  # joins, HELLOs, the network falls asleep
    network.sessions.scan()
    run(30.0)
    for value in range(3):
        future = network.sessions.request(5, 3, value)
        future.add_done_callback(lambda f: outcomes.append(f.result().status))
        run(15.0)
    network.set_power(3, False)
    run(20.0)  # a relay dies: neighbours notice, the session breaks
    network.set_power(3, True)
    network.devices[2].send_heartbeat()
    run(60.0)  # idle: leases run out, sessions close
    # Layer 3: a command whose outcome is not known yet is still being confirmed.
    outcomes += [("confirming", item.sequence, item.rounds) for item in controller.confirming()]
    return [(round(t, 9), sender, bytes(data)) for t, sender, data in network.medium.log], outcomes


def test_skipping_idle_ticks_sends_exactly_the_same_frames():
    skipped, skipped_outcomes = frames(skip=True)
    ticked, ticked_outcomes = frames(skip=False)
    assert len(ticked) > 200  # a busy run: joins, a scan, commands, a break
    assert skipped == ticked
    assert skipped_outcomes == ticked_outcomes and ticked_outcomes  # commands queue per node: some are still confirming
