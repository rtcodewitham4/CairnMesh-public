"""Liveness without flooded heartbeats: one-hop HELLO, a neighbour table, reports.

A heartbeat flooded from every node costs about nodes² transmissions per
interval. In the simulator, 60 nodes at one heartbeat per 30 s already need
twice the channel's capacity and still lose half the heartbeats. Instead:

* every node broadcasts a HELLO every ``hello_interval`` (±15 %). Relays
  never forward it, so the network sends one frame per node per interval;
* every node keeps a table of the neighbours it hears (``max_neighbours``);
* a neighbour silent for ``miss_limit`` intervals is reported LOST to the
  controller, and RECOVERED when it is heard again; only a **watched**
  neighbour, whose smoothed HELLO RSSI is at least ``watch_rssi``. A weak
  link misses HELLOs on its own (fading), so its silence proves nothing:
  measured with the link-budget model (task B1), watching every neighbour
  raised 351 false alarms in 60 nodes where the disc had 1. A node waits a random
  ``report_delay`` first and drops its report when it hears another node
  report the same subject and event, so one report floods per event;
* the controller acknowledges a REPORT with a REPLY; the reporter repeats it
  every ``report_timeout``, at most ``report_retries`` more times.

No news is good news: the controller treats a node as alive until a report
says otherwise. A node whose every neighbour fails cannot be reported by
anyone, but the neighbours of those neighbours report them, which shows
where the network broke.

Measured in the simulator (``liveness_demo``): ``miss_limit=5`` brings false
alarms near zero; 3 raises dozens, because HELLOs collide with request
floods. Detection then takes about ``miss_limit × hello_interval``.
"""

from __future__ import annotations

import math
import random
import time
from dataclasses import dataclass
from typing import Callable, Optional

from .frames import (
    APP,
    APP_KINDS,
    DEFAULT_CONTROLLER_ID,
    FrameKind,
    HelloFrame,
    NodeState,
    ReplyFrame,
    ReportEvent,
    ReportFrame,
    decode_frame,
)
from .mesh import MeshRelay
from .transport import Transport

#: A REPORT under the application layer (``secure``, v3): head 6 B (KEYVER
#: last), SUBJECT, EVENT in clear, AMIC 4 B.
REPORT_APP_LENGTH = APP_KINDS[FrameKind.REPORT] + 1 + 4


@dataclass
class _Neighbour:
    last_heard: float
    rssi: Optional[float]
    #: Smoothed RSSI (``None`` until a HELLO with one).
    level: Optional[float] = None
    lost: bool = False
    #: It went silent while watched: the controller was told (or will be).
    reported: bool = False
    #: HELLOs heard and since when: how often this link carries one.
    heard: int = 1
    since: float = 0.0


@dataclass
class _Report:
    event: ReportEvent
    sequence: int
    due: float
    sent: int = 0


class NeighbourWatch:
    """HELLO beacons, a neighbour table and LOST / RECOVERED reports for one node.

    ``transport`` is the node's secure layer (frames are sealed and flooded
    like any other); ``relay`` is the ``MeshRelay`` under it, which shows
    this watch every frame it hears. Single-threaded, driven by ``tick()``.
    """

    def __init__(
        self,
        transport: Transport,
        relay: MeshRelay,
        node_id: int,
        *,
        controller_id: int = DEFAULT_CONTROLLER_ID,
        state: Callable[[], NodeState] = lambda: NodeState.READY,
        hello_interval: float = 60.0,
        miss_limit: int = 5,
        report_delay: tuple[float, float] = (0.0, 10.0),
        report_timeout: float = 15.0,
        report_retries: int = 3,
        max_neighbours: int = 16,
        max_reports: int = 4,
        watch_rssi: Optional[float] = -118.5,
        weak_window: Optional[int] = 10,
        weak_false_alarm: float = 1e-4,
        max_miss: int = 30,
        epoch: Optional[int] = None,
        clock: Callable[[], float] = time.monotonic,
        rng: Optional[random.Random] = None,
    ) -> None:
        self.transport = transport
        self.node_id = node_id
        self.controller_id = controller_id
        self.state = state
        self.hello_interval = hello_interval
        self.miss_limit = miss_limit
        self.report_delay = report_delay
        self.report_timeout = report_timeout
        self.report_retries = report_retries
        self.max_neighbours = max_neighbours
        self.max_reports = max_reports
        #: SF7 sensitivity −124.5 dBm + 6 dB (estimate): weaker neighbours are
        #: kept but never reported; ``None`` watches all.
        self.watch_rssi = watch_rssi
        #: A weaker neighbour heard for ``weak_window`` intervals is watched
        #: too, on a longer fuse: LOST after ``n`` silent intervals, the least
        #: with (1 − its HELLO rate)ⁿ ≤ ``weak_false_alarm``, if that is at
        #: most ``max_miss``. ``None``: weak neighbours are never watched.
        self.weak_window = weak_window
        self.weak_false_alarm = weak_false_alarm
        self.max_miss = max_miss
        self.clock = clock
        self._rng = rng or random.Random()
        #: Names this run of the node in its REPORT message ids.
        self.epoch = int(time.time()) & 0xFF if epoch is None else epoch
        # [MEM] neighbour table | 端: device | 上限: max_neighbours(默认 16) | 每项: 1 B id + 4 B 时间 + 1 B RSSI + 1 B 平滑 RSSI + 2 B 听到数 + 4 B 起点 + 失联位 / 报告位
        self._neighbours: dict[int, _Neighbour] = {}
        # [MEM] reports waiting to be sent or acknowledged | 端: device | 上限: max_reports(默认 4) | 每项: 1 B subject + 1 B event + 1 B seq + 计时
        self._reports: dict[int, _Report] = {}
        self._hello_sequence = 0
        self._report_sequence = self._rng.randrange(256)
        self._next_hello = clock() + self._rng.uniform(0, hello_interval)
        self.stats = {
            "hellos": 0,
            "lost": 0,
            "weak_silent": 0,
            "recovered": 0,
            "reports_sent": 0,
            "reports_suppressed": 0,
            "reports_acked": 0,
            "reports_abandoned": 0,
            "reports_overflow": 0,
        }
        relay.observer = self.on_heard

    # ------------------------------------------------------------------ query

    def neighbour_count(self) -> int:
        """Neighbours heard within the last ``miss_limit`` intervals."""

        return sum(1 for n in self._neighbours.values() if not n.lost)

    def neighbours(self) -> dict[int, Optional[float]]:
        """Live neighbours and the RSSI of their last HELLO."""

        return {node: n.rssi for node, n in self._neighbours.items() if not n.lost}

    def forget(self) -> None:
        """Start over with an empty table, as after a reboot (RAM is lost).

        Without this, a node back from a long silence would find every
        entry stale and report all its neighbours lost.
        """

        self._neighbours.clear()
        self._reports.clear()

    def resume(self, now: float) -> None:
        """Back from dormant (task D): silence while the network slept proves nothing.

        Every neighbour counts as heard just now, so one only goes LOST
        after ``miss_limit`` intervals of silence while awake; the first
        HELLO goes out within one interval.
        """

        for neighbour in self._neighbours.values():
            gap = max(0.0, now - neighbour.last_heard)
            neighbour.last_heard += gap
            neighbour.since += gap  # no HELLOs while asleep: not a weaker link
        self._next_hello = now + self._rng.uniform(0, self.hello_interval)

    def next_sequence(self) -> int:
        """This node's next own message SEQUENCE, shared by its REPORTs and EVENTs (``uplink``).

        One counter for both, so a REPLY acknowledging one names no other.
        """

        self._report_sequence = (self._report_sequence + 1) & 0xFF
        return self._report_sequence

    # ---------------------------------------------------------------- driving

    def next_due(self) -> float:
        """The earliest time ``tick`` can do anything (``MeshNetwork`` skips idle ticks).

        A neighbour is looked at when its silence reaches its limit as it
        stands now; a weak one's limit only grows while it stays silent (its
        rate falls), so the time named is never in the past and ``tick``
        either acts then or names a later one: firmware sleeping until it
        must not spin. An unwatched weak neighbour is looked at again once
        it has been known for ``weak_window`` intervals (it may become
        watched).
        """

        due = self._next_hello
        now = self.clock()
        for neighbour in self._neighbours.values():
            if neighbour.lost:
                continue
            limit = self._miss_limit(neighbour, now)
            due = min(due, neighbour.last_heard + (limit or self.miss_limit) * self.hello_interval)
            if limit is None and self.weak_window is not None and neighbour.level is not None:
                watched_from = neighbour.since + self.weak_window * self.hello_interval
                if watched_from > now:
                    due = min(due, watched_from)
        for report in self._reports.values():
            due = min(due, report.due)
        return due

    def tick(self, now: Optional[float] = None) -> None:
        now = self.clock() if now is None else now
        if now >= self._next_hello:
            self._send_hello()
            self._next_hello = now + self.hello_interval * self._rng.uniform(0.85, 1.15)

        for node, neighbour in self._neighbours.items():
            if neighbour.lost:
                continue
            limit = self._miss_limit(neighbour, now)
            if now - neighbour.last_heard >= (limit or self.miss_limit) * self.hello_interval:
                neighbour.lost = True
                if limit is None:
                    self.stats["weak_silent"] += 1
                    continue
                neighbour.reported = True
                self.stats["lost"] += 1
                self._raise(node, ReportEvent.LOST, now)

        for subject, report in list(self._reports.items()):
            if now < report.due:
                continue
            if report.sent > self.report_retries:
                del self._reports[subject]
                self.stats["reports_abandoned"] += 1
                continue
            self._send_report(subject, report)
            report.sent += 1
            report.due = now + self.report_timeout

    def on_heard(self, plain: bytes, rssi: Optional[float]) -> None:
        """``MeshRelay`` observer: every authenticated frame heard, first copy only."""

        now = self.clock()
        if plain[0] == FrameKind.REPORT | APP and len(plain) == REPORT_APP_LENGTH:
            # Wire format v3 (review C1): SUBJECT and EVENT are in clear,
            # authenticated by the reporter's application key we cannot check;
            # enough to stay quiet about a node someone else reported.
            self._heard_report(plain[1], plain[6], plain[7])
            return
        try:
            frame = decode_frame(plain)
        except ValueError:
            return
        if isinstance(frame, HelloFrame):
            self._on_hello(frame.sender_id, rssi, now)
        elif isinstance(frame, ReportFrame):
            self._heard_report(frame.sender_id, frame.subject, frame.event)
        elif (
            isinstance(frame, ReplyFrame)
            and frame.target_id == self.node_id
            and frame.sender_id == self.controller_id
            and frame.epoch == self.epoch
        ):
            for subject, report in list(self._reports.items()):
                if report.sent and report.sequence == frame.sequence:
                    del self._reports[subject]
                    self.stats["reports_acked"] += 1

    # ---------------------------------------------------------------- helpers

    def _heard_report(self, sender: int, subject: int, event: int) -> None:
        if sender == self.node_id:
            return
        # Someone else already told the controller.
        pending = self._reports.get(subject)
        if pending is not None and pending.sent == 0 and pending.event == event:
            del self._reports[subject]
            self.stats["reports_suppressed"] += 1

    def _on_hello(self, node: int, rssi: Optional[float], now: float) -> None:
        neighbour = self._neighbours.get(node)
        if neighbour is None:
            if len(self._neighbours) >= self.max_neighbours:
                # Make room: forget whoever was heard least recently.
                oldest = min(self._neighbours, key=lambda n: self._neighbours[n].last_heard)
                del self._neighbours[oldest]
            self._neighbours[node] = _Neighbour(now, rssi, level=rssi, since=now)
            return
        neighbour.last_heard, neighbour.rssi = now, rssi
        neighbour.heard += 1
        if rssi is not None:
            level = neighbour.level
            neighbour.level = rssi if level is None else 0.75 * level + 0.25 * rssi
        if neighbour.lost:
            neighbour.lost = False
            if neighbour.reported:
                neighbour.reported = False
                self.stats["recovered"] += 1
                self._raise(node, ReportEvent.RECOVERED, now)

    def _miss_limit(self, neighbour: _Neighbour, now: float) -> Optional[int]:
        """Silent intervals before ``neighbour`` counts as LOST; ``None``: not watched."""

        if self.watch_rssi is None or neighbour.level is None or neighbour.level >= self.watch_rssi:
            return self.miss_limit
        span = now - neighbour.since
        if self.weak_window is None or span < self.weak_window * self.hello_interval:
            return None  # not heard for long enough to know how often it gets through
        # HELLOs after the first, over the intervals since, the silence so far
        # included (measured: counting up to the last one heard read a link
        # that carries a quarter of them as 0.62, and raised false alarms).
        rate = min(1.0, (neighbour.heard - 1) * self.hello_interval / span)
        if rate >= 1.0:
            return self.miss_limit
        limit = math.ceil(math.log(self.weak_false_alarm) / math.log(1.0 - rate))
        return max(self.miss_limit, limit) if limit <= self.max_miss else None

    def _raise(self, subject: int, event: ReportEvent, now: float) -> None:
        pending = self._reports.get(subject)
        if pending is not None and pending.sent == 0 and pending.event != event:
            # It flapped back before anyone was told: nothing to report.
            del self._reports[subject]
            return
        if pending is None and len(self._reports) >= self.max_reports:
            self.stats["reports_overflow"] += 1
            return
        self._reports[subject] = _Report(
            event, self.next_sequence(), now + self._rng.uniform(*self.report_delay)
        )

    def _send_hello(self) -> None:
        frame = HelloFrame(sender_id=self.node_id, sequence=self._hello_sequence, state=self.state())
        self._hello_sequence = (self._hello_sequence + 1) & 0xFF
        self.stats["hellos"] += 1
        self.transport.send(frame.encode())

    def _send_report(self, subject: int, report: _Report) -> None:
        frame = ReportFrame(
            sender_id=self.node_id,
            target_id=self.controller_id,
            sequence=report.sequence,
            epoch=self.epoch,
            subject=subject,
            event=report.event,
        )
        self.stats["reports_sent"] += 1
        self.transport.send(frame.encode())
