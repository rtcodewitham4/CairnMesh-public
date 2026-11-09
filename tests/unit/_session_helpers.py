"""Shared whole-network fixtures for the routed-session tests.

These used to live in ``test_lora_cc_sessions`` and were imported from there by
the other session tests. They live in their own module so test modules need not
import one another. Behaviour, signatures and names are unchanged.

``tests/lora_helpers.py`` already has ``FakeClock``, ``build`` and ``run``, but
those model a single controller/device link (clock starts at 1000.0 and a
different ``build`` signature); the mesh fixtures here start at 0.0 and build a
``MeshNetwork``. Keeping them separate avoids renaming either set.
"""

from __future__ import annotations

import pytest

pytest.importorskip("cryptography")

from cryptography.hazmat.primitives.ciphers.aead import AESCCM

from cairnmesh.airtime import airtime_for
from cairnmesh.mesh_sim import MeshNetwork
from cairnmesh.secure import MIC_LEN

KEY = bytes(range(16))
AEAD = AESCCM(KEY, tag_length=MIC_LEN)
G = 0x42
SF7 = airtime_for(7)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def build(positions, **options):
    clock = FakeClock()
    gateway_options = options.pop("gateway_options", {})
    network = MeshNetwork(
        positions, key=KEY, clock=clock, airtime=SF7, gateway_options=gateway_options, **options
    )
    controller = network.controller(network.sessions, clock=clock, timeout=8.0, max_retries=2)
    network.sessions.attach(controller)
    return clock, network, controller


def run(clock, network, controller, seconds, until=None) -> None:
    end = clock.now + seconds
    while clock.now < end and not (until is not None and until()):
        clock.now += 0.005
        network.tick(clock.now)
        controller.tick(clock.now)


def open_session(clock, network, controller, target):
    session = network.sessions.open_session(target)
    run(clock, network, controller, 20.0, lambda: session.state == "open")
    assert session.state == "open"
    run(clock, network, controller, 0.5)
    return session


LINE = {node: (80.0 * node, 0.0) for node in range(1, 6)}  # 5 hops, range 100
# Two rows 50 apart: every node has a neighbour to go round it.
LADDER = {**LINE, **{10 + node: (80.0 * node, 50.0) for node in range(1, 6)}}
