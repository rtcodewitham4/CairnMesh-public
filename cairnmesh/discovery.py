"""Node discovery, device side: JOIN when a node starts, answers to the gateway's scans.

Without this the controller knows only the nodes it was told about, and a
healthy quiet node never shows up ("no news is good news").
``protocol_spec.md`` section 12 has the rules.

* **JOIN.** A node that starts waits a random ``join_spread`` (a whole area
  coming back from a power cut must not join at once), then sends a JOIN:
  a PROBE flowing to the gateway. Relays pass it on like a probe, appending
  themselves; the gateway keeps the best path and acknowledges it with a
  routed REPLY along it. Unacknowledged, the node tries again with a fresh
  message id after ``join_backoff`` (doubling up to its upper bound).
* **DISCOVER.** The gateway's scan reaches every node like a probe. The
  relay keeps the best path for the scan's WINDOW and hands it here; the
  node answers once, at a random point within the scan's SPREAD, with a
  DISCOVER_REPLY back along that path: its state, the path's links and up
  to ``MAX_NEIGHBOURS`` other nodes it hears best (candidates for another
  path). Neighbours come from the copies of the scan heard from them, and
  from the HELLO table when liveness runs. There is no end-to-end
  acknowledgement: the gateway asks a known node that stayed silent again.

Single-threaded, driven by ``tick()``, like ``NeighbourWatch``.
"""

from __future__ import annotations

import math
import random
import time
from collections import OrderedDict, deque
from typing import Callable, Optional

from .frames import (
    BROADCAST_ID,
    DEFAULT_CONTROLLER_ID,
    DISCOVER_SPREAD_UNIT,
    MAX_NEIGHBOURS,
    PATH_KINDS,
    PROBE_WAKE,
    PROBE_WINDOW_UNIT,
    DiscoverFrame,
    DiscoverReplyFrame,
    FrameKind,
    JoinFrame,
    LinkQuality,
    NodeState,
    ProbeFrame,
    ProbeHop,
    ReplyFrame,
    add_route,
    decode_routed,
    is_routed,
    reply_route,
)
from .mesh import MeshRelay
from .transport import Transport

#: ``{neighbour: RSSI in dBm}`` of a node's live neighbours (``NeighbourWatch.neighbours``).
Neighbours = Callable[[], "dict[int, Optional[float]]"]


class DiscoveryAgent:
    """JOIN and DISCOVER answers for one node.

    ``transport`` is the node's secure layer; ``relay`` the ``MeshRelay``
    under it, which hands this agent every scan path it collects and shows
    it every frame it hears.
    """

    def __init__(
        self,
        transport: Transport,
        relay: MeshRelay,
        node_id: int,
        *,
        controller_id: int = DEFAULT_CONTROLLER_ID,
        state: Callable[[], NodeState] = lambda: NodeState.READY,
        neighbours: Optional[Neighbours] = None,
        join: bool = True,
        join_spread: float = 30.0,
        join_window: int = 10,
        join_backoff: tuple[float, float] = (30.0, 300.0),
        join_quiet: Optional[float] = 3.0,
        join_by_scan: bool = True,
        island_after: Optional[int] = None,
        island_backoff: float = 3600.0,
        epoch: Optional[int] = None,
        clock: Callable[[], float] = time.monotonic,
        rng: Optional[random.Random] = None,
    ) -> None:
        self.transport = transport
        self.node_id = node_id
        self.controller_id = controller_id
        self.state = state
        self.neighbours = neighbours
        self.join = join
        self.join_spread = join_spread
        self.join_window = join_window
        self.join_backoff = join_backoff
        #: A JOIN is a flood (about one send per node): after hearing another
        #: node's JOIN, wait this long (plus as much again at random) for the
        #: channel to clear before sending ours. ``None`` turns it off.
        self.join_quiet = join_quiet
        #: Answering a scan (or holding back for one) stands in for a JOIN.
        self.join_by_scan = join_by_scan
        #: An island: ``island_after`` JOINs unanswered and nothing from the
        #: controller heard since the start. Its JOINs only flood the island,
        #: so they go every ``island_backoff``; the first frame the controller
        #: sent (a scan, a flood, routed traffic) brings the usual pace back.
        #: ``None`` (default): never. Measured (2026-10-20): island JOINs −70–80 %,
        #: but a reconnected island was found 5/6 instead of 6/6 within 30 min
        #: (an answered scan counts as joined even when the answer is lost):
        #: off until the user decides (STATUS Q17).
        self.island_after = island_after
        self.island_backoff = island_backoff
        self.noise_floor = relay.noise_floor
        self.clock = clock
        self._rng = rng or random.Random()
        #: Names this run of the node in its JOIN message ids.
        self.epoch = int(time.time()) & 0xFF if epoch is None else epoch
        self._sequence = self._rng.randrange(256)
        self.joined = False
        self._join_at: Optional[float] = None
        self._join_wait = join_backoff[0]
        self._join_id: Optional[tuple[int, int]] = None
        self._unanswered = 0
        self._heard_controller = False
        # [MEM] answers waiting for their slot | 端: device | 上限: 2 | 每项: ≤ 66 B 明文 + 计时
        self._answers: list[tuple[float, bytes]] = []
        # [MEM] scans answered | 端: device | 上限: 4 | 每项: (ORIGIN, 消息 ID) 3 B
        self._answered: deque[tuple[int, int]] = deque(maxlen=4)
        # [MEM] nodes heard passing a scan on | 端: device | 上限: 2 次扫描 × 8 | 每项: 1 B id + 1 B RSSI
        self._heard: OrderedDict[tuple[int, int], dict[int, float]] = OrderedDict()
        self.stats = {"joins": 0, "joined": 0, "answers": 0, "answers_dropped": 0}
        self.relay = relay
        relay.path_found = self._on_path
        relay.observers.append(self._on_heard)
        self.start()

    def start(self) -> None:
        """The node (re)starts: RAM is lost, and it joins again."""

        self.joined = False
        self._answers.clear()
        self._heard.clear()
        self._join_wait = self.join_backoff[0]
        self._join_id = None
        self._unanswered = 0
        self._heard_controller = False
        self._join_at = self.clock() + self._rng.uniform(0.0, self.join_spread) if self.join else None

    @property
    def island(self) -> bool:
        """Joining at the island pace: nothing heard from the controller, JOINs unanswered."""

        return (
            self.island_after is not None
            and not self.joined
            and not self._heard_controller
            and self._unanswered >= self.island_after
        )

    # ---------------------------------------------------------------- driving

    def next_due(self) -> float:
        """The earliest time ``tick`` does anything (``MeshNetwork`` skips idle ticks)."""

        due = self._join_at if not self.joined and self._join_at is not None else math.inf
        for answer in self._answers:
            due = min(due, answer[0])
        return due

    def tick(self, now: Optional[float] = None) -> None:
        now = self.clock() if now is None else now
        if not self.joined and self._join_at is not None and now >= self._join_at:
            self._send_join()
            self._unanswered += 1
            wait = self.island_backoff if self.island else self._join_wait
            self._join_at = now + wait
            self._join_wait = min(2 * self._join_wait, self.join_backoff[1])
        due = [answer for answer in self._answers if now >= answer[0]]
        for answer in due:
            self._answers.remove(answer)
            self.stats["answers"] += 1
            # The scan answer tells the gateway about us: no JOIN needed (a
            # lost answer is asked for again, since neighbours name us).
            if self.join_by_scan:
                self.joined = True
                self._join_id = None
            self.transport.send(answer[1])

    # -------------------------------------------------------------- receiving

    def _on_heard(self, plain: bytes, rssi: Optional[float]) -> None:
        """``MeshRelay`` observer: every authenticated frame heard, first copy only."""

        now = self.clock()
        if not self._heard_controller and self._from_controller(plain):
            if self.island and self._join_at is not None:
                # The controller is out there after all: join at the usual pace.
                self._join_wait = self.join_backoff[0]
                self._join_at = min(self._join_at, now + self._rng.uniform(0.0, self.join_spread))
            self._heard_controller = True
        if plain[0] == FrameKind.JOIN and len(plain) >= 7 and plain[5] != self.node_id:
            self._hold_join(now, self.join_quiet)
            return
        if plain[0] == FrameKind.PROBE and len(plain) >= 7 and plain[6] & PROBE_WAKE:
            # Q22: woken by a wake probe, a JOIN due long ago would go out on
            # top of it (measured: 16–19 JOINs inside each failed query round
            # after an area power cut). Wait until its window is over.
            self._hold_join(now, (plain[6] & ~PROBE_WAKE) * PROBE_WINDOW_UNIT + 2.0)
        if (
            self.join_by_scan
            and plain[0] == FrameKind.DISCOVER
            and len(plain) >= 8
            and plain[2] in (BROADCAST_ID, self.node_id)
        ):
            # About to answer a scan, which makes a JOIN needless.
            self._hold_join(now, plain[6] * PROBE_WINDOW_UNIT + plain[7] * DISCOVER_SPREAD_UNIT)
        if plain[0] == FrameKind.DISCOVER and len(plain) >= 8 and rssi is not None:
            # The copy's SENDER is whoever just passed the scan on: a neighbour.
            heard = self._heard.setdefault((plain[5], plain[4] << 8 | plain[3]), {})
            if plain[1] not in heard and len(heard) >= 8:
                weakest = min(heard, key=heard.get)
                if heard[weakest] >= rssi:
                    return
                del heard[weakest]
            heard[plain[1]] = max(rssi, heard.get(plain[1], rssi))
            while len(self._heard) > 2:
                self._heard.popitem(last=False)
            return
        if self._join_id is None or not is_routed(plain) or plain[0] & 0x1F != FrameKind.REPLY:
            return
        try:
            frame, _ = decode_routed(plain)
        except ValueError:
            return
        if (
            isinstance(frame, ReplyFrame)
            and frame.sender_id == self.controller_id
            and frame.target_id == self.node_id
            and (frame.sequence, frame.epoch) == self._join_id
        ):
            self.joined = True
            self._join_id = None
            self.stats["joined"] += 1

    def _on_path(self, frame: ProbeFrame, links: tuple[LinkQuality, ...]) -> None:
        """``MeshRelay.path_found``: the best path of a scan, once its window is over."""

        if not isinstance(frame, DiscoverFrame):
            return
        key = (frame.origin_id, frame.message_id)
        if key in self._answered:
            return
        self._answered.append(key)
        if len(self._answers) >= 2:
            self.stats["answers_dropped"] += 1
            return
        parent = frame.hops[-1].node_id if frame.hops else frame.origin_id
        reply = DiscoverReplyFrame(
            sender_id=self.node_id,
            target_id=frame.origin_id,
            sequence=frame.sequence,
            state=int(self.state()),
            links=links,
            neighbours=self._best_neighbours(key, parent),
            epoch=frame.epoch,
        )
        plain = add_route(reply.encode(), reply_route(hop.node_id for hop in frame.hops))
        self._answers.append((self.clock() + self._rng.uniform(0.0, frame.spread_s), plain))

    # ---------------------------------------------------------------- helpers

    def _best_neighbours(self, scan: tuple[int, int], parent: int) -> tuple[ProbeHop, ...]:
        heard = dict(self._heard.get(scan, {}))
        if self.neighbours is not None:
            for node, rssi in self.neighbours().items():
                if rssi is not None:
                    heard[node] = max(rssi, heard.get(node, rssi))
        heard.pop(parent, None)
        heard.pop(self.node_id, None)
        best = sorted(heard.items(), key=lambda item: -item[1])[:MAX_NEIGHBOURS]
        return tuple(
            ProbeHop(node, LinkQuality.measured(rssi, rssi - self.noise_floor)) for node, rssi in best
        )

    def _from_controller(self, plain: bytes) -> bool:
        """Did the controller send this (a flood keeps its SENDER, a path frame its ORIGIN)?"""

        if len(plain) < 2:
            return False
        if plain[1] == self.controller_id:
            return True
        return plain[0] & 0x1F in PATH_KINDS and len(plain) >= 6 and plain[5] == self.controller_id

    def _hold_join(self, now: float, quiet: Optional[float]) -> None:
        """Push a pending JOIN back by ``quiet`` plus as much again at random."""

        if quiet is None or self.joined or self._join_at is None:
            return
        self._join_at = max(self._join_at, now + quiet + self._rng.uniform(0.0, quiet))

    def _send_join(self) -> None:
        self._sequence = (self._sequence + 1) & 0xFF
        frame = JoinFrame(
            sender_id=self.node_id,
            target_id=self.controller_id,
            sequence=self._sequence,
            origin_id=self.node_id,
            # Q22: booting in a sleeping network, a wake JOIN (long preamble).
            window=self.join_window | (PROBE_WAKE if self.relay.joins_wake() else 0),
            epoch=self.epoch,
        )
        self._join_id = (self._sequence, self.epoch)
        self.stats["joins"] += 1
        self.transport.send(frame.encode())
