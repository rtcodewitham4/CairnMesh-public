"""Wire a simulated medium and several devices into one tickable network."""

from __future__ import annotations

import random
import threading
import time
from typing import Callable, Iterable, Optional

from .channel import MediumPort, SimulatedMedium
from .device import Device
from .frames import DEFAULT_CONTROLLER_ID


class SimulatedNetwork:
    """Several ``Device`` instances sharing one ``SimulatedMedium``.

    ``gateway`` is the transport the controller should use. Call ``tick()``
    from the same loop that drives the controller; ``set_power()`` may be
    called from any other thread (e.g. a UI command).
    """

    def __init__(
        self,
        node_ids: Iterable[int],
        *,
        controller_id: int = DEFAULT_CONTROLLER_ID,
        downlink_drop: float = 0.1,
        uplink_drop: float = 0.1,
        heartbeat_interval: float = 3.0,
        seed: Optional[int] = None,
        clock: Callable[[], float] = time.monotonic,
        trace: Optional[Callable[[str], None]] = None,
    ) -> None:
        # One seeded source spawns an independent stream per component, so a
        # seed reproduces the whole run.
        rng = random.Random(seed)
        self.medium = SimulatedMedium(
            downlink_drop=downlink_drop,
            uplink_drop=uplink_drop,
            rng=random.Random(rng.random()),
            clock=clock,
            trace=trace,
        )
        # [MEM] simulated devices | 端: sim(仅 PC) | 上限: node_ids 数量 | 每项: 1 个 Device
        self.devices: dict[int, Device] = {}
        for node_id in node_ids:
            self.devices[node_id] = Device(
                self.medium.node(node_id),
                node_id,
                controller_id=controller_id,
                heartbeat_interval=heartbeat_interval,
                rng=random.Random(rng.random()),
                clock=clock,
                log=trace,
            )
        self._powered = set(self.devices)
        # Devices are single-threaded; this lock keeps a power switch from
        # resetting a device while the link loop is inside its tick().
        self._lock = threading.RLock()

    @property
    def gateway(self) -> MediumPort:
        return self.medium.gateway

    def device(self, node_id: int) -> Device:
        return self.devices[node_id]

    def tick(self, now: Optional[float] = None) -> None:
        with self._lock:
            self.medium.tick(now)
            for node_id, device in self.devices.items():
                if node_id in self._powered:
                    device.tick(now)

    def is_powered(self, node_id: int) -> bool:
        with self._lock:
            return node_id in self._powered

    def set_power(self, node_id: int, on: bool) -> None:
        """Switch a simulated node off (silent) or on (reboots into SLEEP)."""

        device = self.devices[node_id]
        port = self.medium.node(node_id)
        with self._lock:
            if on and node_id not in self._powered:
                device.reset()
                self._powered.add(node_id)
            elif not on:
                self._powered.discard(node_id)
            port.enabled = on

    def set_drop(self, downlink: float, uplink: float) -> None:
        self.medium.downlink_drop = downlink
        self.medium.uplink_drop = uplink
