"""Shared fakes for the protocol tests: a fake clock, a scripted RNG and a wired-up simulated link.

No TUI imports here, so ``cairnmesh`` stays testable on its own.
"""

from __future__ import annotations

from cairnmesh.channel import SimulatedMedium
from cairnmesh.controller import Controller
from cairnmesh.device import Device


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class ScriptedRng:
    """Drop decisions come from ``draws`` (then never drop); latency is fixed."""

    def __init__(self, draws) -> None:
        self._draws = iter(draws)

    def random(self) -> float:
        return next(self._draws, 1.0)

    def uniform(self, low: float, high: float) -> float:
        return low


def build(node_ids=(1,), *, heartbeat_interval=100.0, awake=True, **medium_options):
    clock = FakeClock()
    medium = SimulatedMedium(latency=(0.01, 0.01), clock=clock, **medium_options)
    devices = {
        node_id: Device(
            medium.node(node_id),
            node_id,
            heartbeat_interval=heartbeat_interval,
            awake=awake,
            clock=clock,
        )
        for node_id in node_ids
    }
    controller = Controller(medium.gateway, timeout=0.5, max_retries=3, clock=clock)
    events = []
    controller.events.subscribe(events.append)
    return clock, medium, devices, controller, events


def run(clock, medium, devices, controller, seconds, step=0.01):
    for _ in range(int(seconds / step)):
        clock.now += step
        medium.tick()
        for device in devices.values():
            device.tick()
        controller.tick()
