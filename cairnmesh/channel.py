"""A simulated lossy single-hop radio medium (``sim.SimulatedNetwork``, ``LinkSession.simulated``).

Star topology: one gateway port and any number of node ports. A frame sent
by the gateway is heard by every node (each copy may be dropped on its own,
like a real broadcast radio); a frame sent by a node is heard only by the
gateway. Delivery is delayed by a random latency and happens in ``tick()``,
so nothing blocks and the whole medium can be driven by a fake clock.

No radio hardware or network socket is involved.
"""

from __future__ import annotations

import heapq
import itertools
import random
import threading
import time
from typing import Callable, Optional

from .transport import Receiver


class MediumPort:
    """One endpoint on the medium; implements the ``Transport`` protocol."""

    def __init__(self, medium: "SimulatedMedium", name: str) -> None:
        self._medium = medium
        self.name = name
        self.enabled = True
        self._receiver: Optional[Receiver] = None

    def set_receiver(self, callback: Receiver) -> None:
        self._receiver = callback

    def send(self, data: bytes) -> None:
        self._medium._transmit(self, bytes(data))

    def _deliver(self, data: bytes) -> None:
        if self.enabled and self._receiver is not None:
            self._receiver(data)


class SimulatedMedium:
    """Move frames between ports with drop probability and latency.

    ``downlink_drop`` / ``uplink_drop`` are probabilities in the range 0..1.
    """

    def __init__(
        self,
        *,
        downlink_drop: float = 0.0,
        uplink_drop: float = 0.0,
        latency: tuple[float, float] = (0.02, 0.08),
        rng: Optional[random.Random] = None,
        clock: Callable[[], float] = time.monotonic,
        trace: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.downlink_drop = downlink_drop
        self.uplink_drop = uplink_drop
        self.latency = latency
        self.rng = rng or random.Random()
        self.clock = clock
        self.trace = trace
        self.gateway = MediumPort(self, "gateway")
        # [MEM] node ports | 端: sim(仅 PC) | 上限: 255 个节点 | 每项: 1 个端口对象
        self._nodes: dict[int, MediumPort] = {}
        # [MEM] in-flight delivery queue | 端: sim(仅 PC) | 上限: 无上限 | 每项: 1 帧 + 到期时间
        # Heap of (due, send order, target, frame); the counter keeps equal
        # due times in send order and stops heapq from comparing ports.
        self._queue: list[tuple[float, int, MediumPort, bytes]] = []
        self._counter = itertools.count()
        self._lock = threading.Lock()
        # [MEM] link counters | 端: sim(仅 PC) | 上限: 固定 4 项 | 每项: 1 个计数
        self.stats = {
            "downlink_sent": 0,
            "downlink_dropped": 0,
            "uplink_sent": 0,
            "uplink_dropped": 0,
        }

    def node(self, node_id: int) -> MediumPort:
        """Return the port for ``node_id``, creating it on first use."""

        with self._lock:
            port = self._nodes.get(node_id)
            if port is None:
                port = MediumPort(self, f"node 0x{node_id:02X}")
                self._nodes[node_id] = port
            return port

    def _transmit(self, source: MediumPort, data: bytes) -> None:
        if not source.enabled:
            return
        now = self.clock()
        with self._lock:
            if source is self.gateway:
                direction, drop = "downlink", self.downlink_drop
                targets = list(self._nodes.values())
            else:
                direction, drop = "uplink", self.uplink_drop
                targets = [self.gateway]
            for target in targets:
                # Each receiver hears its own copy, so each copy is dropped
                # independently, like a real broadcast radio.
                self.stats[f"{direction}_sent"] += 1
                if self.rng.random() < drop:
                    self.stats[f"{direction}_dropped"] += 1
                    self._log(f"[LINK] {direction.upper()} dropped → {target.name}")
                    continue
                due = now + self.rng.uniform(*self.latency)
                heapq.heappush(self._queue, (due, next(self._counter), target, data))

    def tick(self, now: Optional[float] = None) -> None:
        """Deliver every frame whose latency has elapsed."""

        now = self.clock() if now is None else now
        due: list[tuple[MediumPort, bytes]] = []
        with self._lock:
            while self._queue and self._queue[0][0] <= now:
                _, _, target, data = heapq.heappop(self._queue)
                due.append((target, data))
        # Deliver outside the lock: receivers may transmit in response.
        for target, data in due:
            target._deliver(data)

    def _log(self, message: str) -> None:
        if self.trace is not None:
            self.trace(message)
