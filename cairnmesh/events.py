"""Events published by the link layer.

The controller reports everything that happens on the link through these
plain dataclasses. Front ends (TUI, demo printer, tests, a future logger)
subscribe to an ``EventBus`` and never reach into controller internals, so
the protocol can evolve without touching UI code.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from .frames import FrameKind, NodeState, ReplyResult, ReportEvent

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LinkEvent:
    #: Wall-clock timestamp (``time.time()``) when the event was created.
    at: float = field(default_factory=time.time, kw_only=True)


@dataclass(frozen=True)
class HeartbeatReceived(LinkEvent):
    """A device reported its state."""

    node_id: int
    sequence: int
    state: NodeState


@dataclass(frozen=True)
class RequestSent(LinkEvent):
    """A WAKE or REQUEST frame left the controller (first try or retry)."""

    node_id: int
    sequence: int
    kind: FrameKind
    attempt: int
    max_attempts: int
    action: int | None = None
    value: int | None = None


@dataclass(frozen=True)
class ReplyReceived(LinkEvent):
    """The in-flight request to ``node_id`` was answered."""

    node_id: int
    sequence: int
    kind: FrameKind
    result: int
    attempts: int
    rtt: float
    #: The answer came from a status query (Q18, layer 3), not the request's own reply.
    queried: bool = False

    @property
    def ok(self) -> bool:
        return self.result == ReplyResult.OK


@dataclass(frozen=True)
class RequestFailed(LinkEvent):
    """An unconfirmed result, or a confirmed refusal: no answer, or ``NOT_RUN``. (An unknown result code is not reported here: see ``ReplyIgnored``.)"""

    node_id: int
    sequence: int
    kind: FrameKind
    attempts: int
    reason: str
    #: ``"unconfirmed"``: it may or may not have run; ``"not_run"``: the
    #: device confirmed it never ran and never will (Q18).
    status: str = "unconfirmed"
    #: Layer 3: ``"unconfirmed"`` after no answer,
    #: the operator's ``abandon``, or the devices' record keep period;
    #: ``True`` only for the operator's ``abandon``.
    abandoned: bool = False
    #: The request's EPOCH (an older one for a request restored after a
    #: restart); ``None`` only from code that predates the field.
    epoch: Optional[int] = None
    #: An ordinary (not ``important``) command reached its confirm limit
    #: (spec 4.2, 2026-11-08): ``"unconfirmed"``, it **may have run**, and is
    #: never sent again by the controller. A positive result may still come
    #: late: ``LateConfirmed``.
    expired: bool = False


@dataclass(frozen=True)
class LateConfirmed(LinkEvent):
    """A positive result reached a command already ended ``UNCONFIRMED`` (spec 4.2).

    The command's ``Future`` is not changed (it is done). After the controller
    gave up on an ordinary command, or the operator abandoned one, it sends
    nothing more about it, but takes a late ``OK`` / ``ERROR`` /
    ``INTERRUPTED`` from its REPLY (own id or one of its last query ids) or
    its RESULT. ``NOT_RUN`` and ``ACCEPTED`` after that point are not taken.
    The record is kept in the state file until ``Controller.acknowledge_late``
    (or the devices' keep period), and published again by the first ``tick``
    after a restart (``replayed``).
    """

    node_id: int
    sequence: int
    epoch: int
    #: ``ReplyResult.OK`` / ``ERROR`` / ``INTERRUPTED``.
    result: int
    #: ``"reply"`` (the request's own or a query's answer) or ``"result"`` (a RESULT frame).
    via: str
    #: Seconds from the command's first send to the late result.
    elapsed: float
    #: Published again after a restart: the first time was never acknowledged.
    replayed: bool = False


@dataclass(frozen=True)
class RequestCompleted(LinkEvent):
    """An ACCEPTED request's action is over (2026-10-21): its RESULT came, or a status query found it.

    ``result`` is ``None`` when it can no longer be learnt: the device's
    record is gone, or its result frame carried an unknown result code. That
    the device ran it, once, was settled by ACCEPTED.
    """

    node_id: int
    sequence: int
    epoch: int
    result: Optional[int]
    #: Seconds from ACCEPTED to this.
    elapsed: float
    #: Learnt by a status query, not from the RESULT frame.
    queried: bool = False


@dataclass(frozen=True)
class RequestQuerying(LinkEvent):
    """A request's retries got no answer: the controller now asks the device whether it ran (Q18)."""

    node_id: int
    #: The request asked about, and the query's own sequence.
    sequence: int
    query_sequence: int
    attempts: int
    #: The asked request's EPOCH (also the query's).
    epoch: Optional[int] = None


@dataclass(frozen=True)
class RequestConfirming(LinkEvent):
    """Layer 3: the queries of ``confirm_window`` got no answer either; the controller keeps asking.

    Published when the request enters CONFIRMING and after every round of
    queries that got no answer. Durations in seconds, as of ``at``.
    """

    node_id: int
    sequence: int
    epoch: int
    #: Since the request was first sent.
    age: float
    #: Since the node was last heard (any frame); ``None``: not since the request.
    heard_ago: Optional[float]
    rounds: int
    #: Until the next round of queries (sooner if the node is heard).
    next_in: float
    #: ``important`` commands are confirmed on to the keep period; an ordinary one ends at its confirm limit.
    important: bool = True
    #: Ordinary commands: seconds until that limit (a hard point, whatever ``next_in`` says); ``None`` for important ones.
    limit_in: Optional[float] = None


@dataclass(frozen=True)
class LinkTestFinished(LinkEvent):
    """A link test (``Gateway.test_link``) is over; ``test`` is the finished ``gateway.LinkTest``."""

    node_id: int
    test: object


@dataclass(frozen=True)
class HealthChanged(LinkEvent):
    """A node's health in operation changed (``gateway.Health``, layer 2); ``reasons`` empty when OK again."""

    node_id: int
    ok: bool
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class AcceptanceFinished(LinkEvent):
    """Deployment acceptance of a node is over; ``acceptance`` is the ``gateway.Acceptance``."""

    node_id: int
    acceptance: object


@dataclass(frozen=True)
class NodeReported(LinkEvent):
    """A node reported that its neighbour ``node_id`` was lost or came back.

    Published once per report, however many retries of it arrive.
    """

    node_id: int
    reporter: int
    event: ReportEvent


@dataclass(frozen=True)
class NodeEvent(LinkEvent):
    """A module on node ``node_id`` raised ``event_id`` (an EVENT frame, wire format v4).

    The gateway defines what the id means. Published once per event, however
    many retries of it arrive.
    """

    node_id: int
    event_id: int


@dataclass(frozen=True)
class SessionChanged(LinkEvent):
    """A routed session with ``node_id`` changed state (published by ``LinkSession``).

    ``state`` is one of ``probing``, ``open``, ``broken``, ``failed``,
    ``closed``; ``detail`` says why it broke. On ``open`` the path and the
    latency model's figures come along; on ``broken`` the path that broke
    and, when a relay or the gateway's own first hop said so, the hop
    ``broken_at`` (``(sender, receiver)``; a missed keepalive has none).
    """

    node_id: int
    state: str
    detail: str = ""
    route: tuple[int, ...] = ()
    expected: Optional[float] = None
    worst: Optional[float] = None
    within_target: Optional[bool] = None
    realtime_target: Optional[float] = None
    #: Seconds from ``open_session`` to the path being up (``open`` only).
    setup: Optional[float] = None
    broken_at: Optional[tuple[int, int]] = None


@dataclass(frozen=True)
class NodeFound(LinkEvent):
    """The gateway learnt of ``node_id`` and a path to it (task C).

    ``how`` is ``join`` (the node announced itself after starting) or
    ``scan`` (it answered a DISCOVER). ``route`` lists the relays from the
    gateway outward, ``links`` one ``(RSSI dBm, SNR dB)`` per hop the same
    way; a scan answer adds ``state`` and ``neighbours`` (``(id, RSSI dBm)``,
    strongest first). ``new`` says the gateway did not know the node yet.
    """

    node_id: int
    how: str
    route: tuple[int, ...] = ()
    links: tuple[tuple[int, float], ...] = ()
    state: Optional[int] = None
    neighbours: tuple[tuple[int, int], ...] = ()
    new: bool = False


@dataclass(frozen=True)
class ScanFinished(LinkEvent):
    """A DISCOVER scan ended (every round to everyone, then the one-by-one asks). Node ids are sorted."""

    found: tuple[int, ...]
    #: Known before the scan or named by a neighbour, yet silent to the end.
    missing: tuple[int, ...]
    #: Found, and not known before.
    new: tuple[int, ...]
    #: Seconds the scan took, and what the latency model said it would.
    duration: float
    expected: float
    #: Silent because they were on a session's channel (task Q12): alive,
    #: not in ``missing``.
    in_session: tuple[int, ...] = ()


@dataclass(frozen=True)
class TraceArrived(LinkEvent):
    """A trace of command ``sequence`` to ``node_id`` came back."""

    node_id: int
    sequence: int
    #: Nodes on the path that answered, of all on it (target and relays).
    answered: int
    expected: int


@dataclass(frozen=True)
class ReplyIgnored(LinkEvent):
    """A reply arrived that matches nothing in flight (late or duplicate)."""

    node_id: int
    sequence: int
    reason: str


@dataclass(frozen=True)
class FrameRejected(LinkEvent):
    """An incoming frame could not be decoded."""

    raw: bytes
    reason: str


@dataclass(frozen=True)
class LinkError(LinkEvent):
    """An unexpected exception inside the link loop."""

    message: str


Subscriber = Callable[[LinkEvent], None]


class EventBus:
    """Thread-safe fan-out of link events to any number of subscribers.

    Subscribers are called on the publishing thread and must return quickly;
    a UI should hand events over to its own thread.
    """

    def __init__(self) -> None:
        # [MEM] event subscribers | 端: controller | 上限: 无上限 | 每项: 1 个回调引用
        self._subscribers: list[Subscriber] = []
        self._lock = threading.Lock()

    def subscribe(self, callback: Subscriber) -> Callable[[], None]:
        """Register ``callback``; returns a function that unsubscribes it."""

        with self._lock:
            self._subscribers.append(callback)

        def unsubscribe() -> None:
            with self._lock:
                if callback in self._subscribers:
                    self._subscribers.remove(callback)

        return unsubscribe

    def publish(self, event: LinkEvent) -> None:
        with self._lock:
            subscribers = list(self._subscribers)
        for callback in subscribers:
            try:
                callback(event)
            except Exception:  # noqa: BLE001 - a broken subscriber must not stop the link
                logger.exception("link event subscriber failed")
