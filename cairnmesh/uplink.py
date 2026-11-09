"""Module events to the controller: the EVENT frame's sender on a device (wire format v4, 2026-10-22).

A module on the node (a watched input, a threshold) raises an event whose id
the gateway defined; the node sends no reading (user, 2026-10-22). Rules
(``protocol_spec.md`` section 19):

* **Held while the network sleeps** (user: an event need not happen while
  the gateway is there): ``notify`` works at any time, a dormant node only
  queues it. The main loop does not run this layer while the node is dormant
  (``NodeStack.tick``); when the node wakes (``resume``) every queued event
  goes out within ``wake_spread``, so the nodes that all woke on the same
  WAKE_NET do not flood at once.
* **One per id** (user): an id already queued is not queued again (counted
  ``events_merged``); with ``max_events`` queued, ``notify`` refuses
  (``False``, counted ``events_refused``). Nothing queued is ever dropped.
* **Until acknowledged**: flooded to the controller like a REPORT, which
  answers every copy with a network-only REPLY echoing SEQUENCE and EPOCH.
  Unanswered, it goes again after ``event_timeout``, doubling up to
  ``event_backoff_max`` (±25 %); it is never given up. Waking resets the
  pace: the gateway is likely there.

The message id is the device's own: EPOCH names its run and SEQUENCE comes
from the counter it shares with its REPORTs (``NeighbourWatch.next_sequence``),
so an acknowledgement names one frame of either kind. The application layer
tells these apart from the acknowledgement of a RESULT, which carries it.
The queue is in RAM: a reboot loses it.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass
from typing import Callable, Optional

from .frames import DEFAULT_CONTROLLER_ID, EventFrame, FrameKind, ReplyFrame
from .mesh import MeshRelay
from .transport import Transport


@dataclass
class _Event:
    sequence: int
    due: float
    sent: int = 0


class EventReporter:
    """The queue of module events one node still has to get acknowledged.

    ``transport`` is the node's secure layer; ``relay`` the ``MeshRelay``
    under it (this layer watches it for acknowledgements). Single-threaded,
    driven by ``tick()`` while the node is awake.
    """

    def __init__(
        self,
        transport: Transport,
        relay: MeshRelay,
        node_id: int,
        *,
        controller_id: int = DEFAULT_CONTROLLER_ID,
        next_sequence: Optional[Callable[[], int]] = None,
        epoch: Optional[int] = None,
        max_events: int = 4,
        event_delay: tuple[float, float] = (0.0, 2.0),
        wake_spread: tuple[float, float] = (1.0, 30.0),
        event_timeout: float = 15.0,
        event_backoff_max: float = 600.0,
        clock: Callable[[], float] = time.monotonic,
        rng: Optional[random.Random] = None,
    ) -> None:
        self.transport = transport
        self.node_id = node_id
        self.controller_id = controller_id
        self.max_events = max_events
        #: Raised while awake: sent this long after.
        self.event_delay = event_delay
        #: Queued while dormant (or unanswered) when the node wakes: sent this long after.
        self.wake_spread = wake_spread
        self.event_timeout = event_timeout
        self.event_backoff_max = event_backoff_max
        self.clock = clock
        self._rng = rng or random.Random()
        #: Names this run of the node in its EVENT message ids (the same as its REPORTs').
        self.epoch = int(time.time()) & 0xFF if epoch is None else epoch
        self._sequence = self._rng.randrange(256)
        self.next_sequence = next_sequence or self._own_sequence
        # [MEM] module events waiting to be sent or acknowledged | 端: device | 上限: max_events(默认 4) | 每项: 1 B id + 1 B seq + 1 B 次数 + 4 B 计时
        self._events: dict[int, _Event] = {}
        self.stats = {
            "events_raised": 0,
            "events_merged": 0,
            "events_refused": 0,
            "events_sent": 0,
            "events_acked": 0,
        }
        relay.observers.append(self.on_heard)

    def _own_sequence(self) -> int:
        self._sequence = (self._sequence + 1) & 0xFF
        return self._sequence

    # ------------------------------------------------------------------ query

    def pending(self) -> tuple[int, ...]:
        """Ids queued, in the order they were raised."""

        return tuple(self._events)

    # ---------------------------------------------------------------- driving

    def notify(self, event_id: int, now: Optional[float] = None) -> bool:
        """A module raised ``event_id`` (0..255); ``False`` when the queue is full.

        Callable at any time; a dormant node only queues it.
        """

        if not 0 <= event_id <= 0xFF:
            raise ValueError(f"event id must be 0..255, got {event_id}")
        if event_id in self._events:
            self.stats["events_merged"] += 1
            return True
        if len(self._events) >= self.max_events:
            self.stats["events_refused"] += 1
            return False
        now = self.clock() if now is None else now
        self.stats["events_raised"] += 1
        self._events[event_id] = _Event(self.next_sequence(), now + self._rng.uniform(*self.event_delay))
        return True

    def resume(self, now: float) -> None:
        """The node woke (``WakeAgent.on_wake``): everything queued goes out within ``wake_spread``."""

        for event in self._events.values():
            event.due = now + self._rng.uniform(*self.wake_spread)
            event.sent = 0  # the gateway is likely there: back to the fast pace

    def forget(self) -> None:
        """A reboot: the queue was in RAM."""

        self._events.clear()

    def next_due(self) -> float:
        return min((event.due for event in self._events.values()), default=float("inf"))

    def tick(self, now: Optional[float] = None) -> None:
        now = self.clock() if now is None else now
        for event_id, event in self._events.items():
            if now < event.due:
                continue
            self._send(event_id, event)
            # The exponent is capped as on the board (a week of 600 s retries
            # would reach 2**1024, which no float holds).
            backoff = min(self.event_timeout * 2 ** min(event.sent, 30), self.event_backoff_max)
            event.sent += 1
            event.due = now + backoff * self._rng.uniform(0.75, 1.25)

    def on_heard(self, plain: bytes, rssi: Optional[float]) -> None:
        """``MeshRelay`` observer: the controller's acknowledgement (a network-only REPLY)."""

        if plain[0] != FrameKind.REPLY or len(plain) != 6:
            return
        try:
            frame = ReplyFrame.from_bytes(plain)
        except ValueError:
            return
        if frame.target_id != self.node_id or frame.sender_id != self.controller_id or frame.epoch != self.epoch:
            return
        for event_id, event in list(self._events.items()):
            if event.sent and event.sequence == frame.sequence:
                del self._events[event_id]
                self.stats["events_acked"] += 1

    # ---------------------------------------------------------------- helpers

    def _send(self, event_id: int, event: _Event) -> None:
        frame = EventFrame(
            sender_id=self.node_id,
            target_id=self.controller_id,
            sequence=event.sequence,
            epoch=self.epoch,
            event_id=event_id,
        )
        self.stats["events_sent"] += 1
        self.transport.send(frame.encode())
