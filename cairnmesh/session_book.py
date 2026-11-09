"""The gateway's book of routed sessions, and the commands in flight over them.

``SessionBook`` is the one place that adds or removes a ``Session``, and the
only writer of the per-node routed message in flight and of the open
deliveries (diagnostics). ``Gateway`` keeps everything radio-side (probes,
keepalives, arms, floods, the latency model) and reaches the book through
its public methods; read access is ``Gateway.sessions`` (a read-only view of
the book's ``sessions``).

This module also holds the records the book keeps (``Session``, ``Delivery``,
``TraceResult``); ``gateway`` re-exports them under their old names.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING, Callable, Optional

from .frames import LinkQuality

if TYPE_CHECKING:  # pragma: no cover
    from .latency import Estimate

PROBING, OPEN, FAILED, CLOSED = "probing", "open", "failed", "closed"


@dataclass
class TraceResult:
    """What every node on the path said about one command (a TRACE's answer)."""

    route: tuple[int, ...]
    #: ``TraceRecord`` per node, the gateway's own included.
    records: dict
    received_at: float


@dataclass
class Delivery:
    """One command on a session as the gateway saw it, for diagnostics.

    Everything here comes for free: the gateway's own first hop (``MeshRelay``
    route log: repeats, the copy that acknowledged it and how strong it was),
    ROUTE_ERROR (where an outbound frame got stuck), the fallback, and the
    time taken against the model. ``inferred_repeats`` turns the extra time
    into a number of hop repeats somewhere on the path; where exactly needs
    the relays' own records (a TRACE: ``trace``).
    """

    target: int
    sequence: int
    epoch: int
    route: tuple[int, ...]
    sent_at: float
    expected: float
    worst: float
    #: Mean hop timeout on this path: the cost of one repeat.
    repeat_cost: float
    #: ``(time, what, node)``.
    events: list[tuple[float, str, Optional[int]]] = field(default_factory=list)
    first_hop_sends: int = 0
    first_hop_ack: Optional[tuple[int, Optional[float]]] = None
    #: ``(relay, next hop)`` it reported it could not reach, and the path it was on.
    broken_at: Optional[tuple[int, int]] = None
    broken_route: Optional[tuple[int, ...]] = None
    fell_back: bool = False
    finished_at: Optional[float] = None
    ok: Optional[bool] = None
    #: The answer to a trace of this command, once one came back.
    trace: Optional[TraceResult] = None

    @property
    def faulty(self) -> bool:
        """Failed, broke the path, fell back, or took longer than the model's worst case."""

        slow = self.elapsed is not None and self.elapsed > self.worst
        return self.ok is False or self.fell_back or self.broken_at is not None or slow

    @property
    def anomalous(self) -> bool:
        """``faulty``, or needed hop repeats on the way."""

        return self.faulty or self.first_hop_sends > 1 or bool(self.inferred_repeats)

    @property
    def message_id(self) -> int:
        return self.epoch << 8 | self.sequence

    @property
    def elapsed(self) -> Optional[float]:
        return None if self.finished_at is None else self.finished_at - self.sent_at

    @property
    def inferred_repeats(self) -> Optional[int]:
        """Hop repeats the extra time stands for (only meaningful for a routed answer)."""

        if self.elapsed is None or self.fell_back or not self.ok:
            return None
        return max(0, round((self.elapsed - self.expected) / self.repeat_cost))


@dataclass
class Session:
    """A routed session with one node, as the gateway sees it."""

    target: int
    state: str = PROBING
    #: Relays R1 .. RN in order from the gateway.
    route: tuple[int, ...] = ()
    #: One per hop, from the gateway's side outward (symmetric links assumed).
    links: tuple[LinkQuality, ...] = ()
    opened_at: float = 0.0
    established_at: Optional[float] = None
    last_used: float = 0.0
    probe_id: Optional[tuple[int, int]] = None
    #: Every probe of the current round; an answer to any of them opens the session.
    probe_ids: list[tuple[int, int]] = field(default_factory=list)
    probe_deadline: float = 0.0
    #: The message falling back to a flood: the re-probe waits for its answer
    #: (or until ``probe_not_before``), so the command keeps the channel.
    fallback: Optional[tuple[int, int]] = None
    probe_not_before: float = 0.0
    probe_attempts: int = 0
    retry_at: float = 0.0
    keepalive_id: Optional[tuple[int, int]] = None
    keepalive_deadline: float = 0.0
    next_keepalive: float = 0.0
    #: A command that came while a keepalive was out: one exchange at a time
    #: on the path (measured: a request meeting the keepalive's reply head on
    #: collided at hidden nodes until the route broke).
    waiting: Optional[tuple[tuple[int, int], bytes]] = None
    # [MEM] diagnostics | 端: gateway(PC) | 上限: 20 条命令 + 每跳计数 | 不上空口
    deliveries: deque = field(default_factory=lambda: deque(maxlen=20))
    #: ROUTE_ERRORs (and first-hop failures) per ``(relay, next hop)``.
    hop_errors: dict[tuple[int, int], int] = field(default_factory=dict)
    #: Latest RSSI the gateway itself measured on its own hop: ``(dBm, time)``.
    first_hop_rssi: Optional[tuple[float, float]] = None
    #: The trace out on the path (one exchange at a time), and the command it asks about.
    trace_id: Optional[tuple[int, int]] = None
    trace_deadline: float = 0.0
    trace_of: Optional[Delivery] = None
    #: A command waiting to be traced once the path is free (policy A or ``Gateway.trace``).
    trace_wanted: Optional[Delivery] = None
    last_trace_at: float = float("-inf")
    last_finished_at: float = 0.0
    #: When the command in flight on the path last went out.
    sent_at: float = float("-inf")
    #: First hops in a row a relay past ``route[0]`` confirmed (``Gateway.skip_reprobe``).
    first_hop_skips: int = 0
    #: Task C5: the hop the last break points at (``(sender, receiver)``), and
    #: whether a probe along a detour round it was tried since.
    suspect: Optional[tuple[int, int]] = None
    detour_tried: bool = False
    #: Task Q11: the SF the path runs at now, and the one asked for (the next
    #: keepalive names it; everyone moves once its answer is through).
    sf: int = 7
    sf_wanted: Optional[int] = None
    #: Task Q12 / B4.5: the gateway radio the session runs on (0: the one on
    #: the common channel), and the one promised to it (same switch as the SF).
    radio: int = 0
    radio_wanted: Optional[int] = None
    #: ``(radio, sf)`` the keepalive in flight asked the path for: it cannot
    #: be taken back, so its answer moves the session there whatever is
    #: wanted by then (the next keepalive asks for that).
    asked: Optional[tuple[int, int]] = None
    #: When the session last moved onto the session radio.
    radio_since: float = float("-inf")
    latency: Optional[Estimate] = None
    within_target: Optional[bool] = None
    counts: dict[str, int] = field(
        default_factory=lambda: {
            "probes": 0,
            "detours": 0,
            "sf_switches": 0,
            "radio_switches": 0,
            "radio_strikes": 0,
            "switch_follows": 0,
            "established": 0,
            "keepalives": 0,
            "keepalive_misses": 0,
            "route_errors": 0,
            "broken": 0,
            "routed": 0,
            "fallbacks": 0,
            "first_hop_skips": 0,
        }
    )

    @property
    def hops(self) -> int:
        return len(self.route) + 1

    def note_first_hop(self, ack_from: Optional[int], sends: int) -> None:
        if ack_from == self.route[0]:
            self.first_hop_skips = 0
        elif sends == 1 and ack_from in (*self.route[1:], self.target):
            # Only on a first copy: after a repeat the first relay answers
            # with the TX of the furthest node it knows has the frame, so a
            # later name then does not mean it was silent.
            self.first_hop_skips += 1
            self.counts["first_hop_skips"] += 1

    def note_first_hop_rssi(self, ack_from: Optional[int], ack_rssi: Optional[float], now: float) -> None:
        """The gateway's own first hop confirmed a frame, and measured how strongly."""

        first = self.route[0] if self.route else self.target
        if ack_from == first and ack_rssi is not None:
            self.first_hop_rssi = (ack_rssi, now)

    def note_path_opened(self) -> None:
        """A probe opened a new path: any old suspect and detour attempt no longer apply."""

        self.suspect, self.detour_tried = None, False

    def note_path_broken(self, hop: Optional[tuple[int, int]]) -> None:
        """The path broke: remember where, and allow one detour round it."""

        self.suspect, self.detour_tried = hop, False

    def claim_detour(self) -> bool:
        """Take the one detour attempt for the current suspect hop, if it is still available."""

        if self.suspect is None or self.detour_tried:
            return False
        self.detour_tried = True
        return True

    def off_common(self, base_sf: int) -> bool:
        """Is the session (or what it asked for or was promised) off the common channel and ``base_sf``?"""

        return (
            self.radio != 0
            or self.sf != base_sf
            or self.radio_wanted not in (None, 0)
            or self.sf_wanted not in (None, base_sf)
            or self.asked not in (None, (0, base_sf))
        )


@dataclass
class SessionConfig:
    """The ``Gateway`` arguments the session book reads (same names, no defaults here: ``Gateway`` owns them)."""

    #: A session unused for this long closes (KEEPALIVE with lease 0); ``None``: never.
    idle_timeout: Optional[float]
    #: Probes of one round beyond the first; the book keeps that many ids plus one.
    probe_retries: int
    #: A failed session probes again this many seconds later.
    reprobe_after: float
    #: Task B1 (measured with shadowing and fading: a dead first relay hid
    #: behind the next one overhearing the gateway, until a fade broke
    #: the session 8–28 s later): this many first hops in a row confirmed
    #: past ``route[0]`` and the gateway probes again; ``None`` never.
    skip_reprobe: Optional[int]


class SessionBook:
    """Open sessions, the routed message in flight over each, and the deliveries open.

    Owns (and is the only writer of) ``sessions``, ``inflight`` and
    ``deliveries``; all three are read-only views outside this class.
    Callbacks from ``Gateway`` (bound methods, so the book never holds the
    gateway itself): ``hang_up(session)`` does the radio side of closing,
    ``probe(session, now)`` sends a first probe, ``stale(key)`` says the
    controller no longer waits on a message, ``log(event, node)`` is the
    gateway's history.
    """

    def __init__(
        self,
        config: SessionConfig,
        *,
        hang_up: Callable[[Session], None],
        probe: Callable[[Session, float], None],
        stale: Callable[[tuple[int, int]], bool],
        log: Callable[[str, int], None],
    ) -> None:
        self.config = config
        self._hang_up = hang_up
        self._probe = probe
        self._stale = stale
        self._log = log
        # [MEM] sessions | 端: gateway(PC 模拟) | 上限: 打开的会话数 | 每项: 路由 ≤16 B + 链路 ≤34 B + 计时
        self._sessions: dict[int, Session] = {}
        # [MEM] routed message in flight per node (for fallback) | 端: gateway | 上限: 会话数 | 每项: ≤7 B 帧
        self._inflight: dict[int, tuple[tuple[int, int], bytes]] = {}
        # [MEM] diagnostics of commands in flight | 端: gateway(PC) | 上限: 会话数 | 不上空口
        self._deliveries: dict[tuple[int, int], Delivery] = {}
        self.sessions = MappingProxyType(self._sessions)
        self.inflight = MappingProxyType(self._inflight)
        self.deliveries = MappingProxyType(self._deliveries)

    # ------------------------------------------------------------ sessions

    def open(self, target: int, now: float) -> Session:
        """Start (or keep) the session with ``target``: one at a time (``Gateway.open_session``)."""

        session = self._sessions.get(target)
        if session is not None and session.state != CLOSED:
            session.last_used = now
            return session
        for other in list(self._sessions):
            self.close(other)
        session = self._sessions[target] = Session(target, opened_at=now, last_used=now)
        assert len(self._sessions) == 1, "one session at a time (user 2026-10-20)"
        self._probe(session, now)
        return session

    def close(self, target: int) -> None:
        session = self._sessions.pop(target, None)
        if session is None:
            return
        self._hang_up(session)
        self._inflight.pop(target, None)  # a retry goes by flood now: no path to fall back from
        session.state = CLOSED
        self._log("closed", target)

    def idle(self, session: Session, now: float) -> bool:
        """Has ``session`` gone unused for longer than ``idle_timeout``?"""

        timeout = self.config.idle_timeout
        return timeout is not None and now - session.last_used > timeout

    def probing(
        self,
        session: Session,
        probe_id: tuple[int, int],
        deadline: float,
        *,
        detour: bool = False,
    ) -> None:
        """A probe is out: its id joins this round's, with the deadline for its answer."""

        session.state = PROBING
        session.probe_id = probe_id
        session.probe_ids.append(session.probe_id)
        del session.probe_ids[: -(self.config.probe_retries + 1)]
        session.probe_attempts += 1
        session.probe_deadline = deadline
        session.counts["probes"] += 1
        if detour:
            session.counts["detours"] += 1

    def reset_probe_attempts(self, session: Session) -> None:
        """A new round starts, while ids from the previous one may still answer."""

        session.probe_attempts = 0

    def reset_probe_round(self, session: Session) -> None:
        """The path broke: neither the old round's probes nor its keepalive can use it."""

        session.probe_attempts = 0
        session.probe_ids.clear()
        session.keepalive_id = None

    def defer_probe(self, session: Session, not_before: float) -> None:
        """Probe again no earlier than ``not_before``, keeping a later existing date."""

        session.state = PROBING
        session.probe_id = None
        session.probe_not_before = max(session.probe_not_before, not_before)

    def probe_after_wake(self, session: Session, not_before: float) -> None:
        """Wake a sleeping network first; the probe waits for the wake to spread."""

        session.state = PROBING
        session.probe_id = None
        session.probe_not_before = not_before

    def may_probe_again(self, session: Session) -> bool:
        return session.probe_attempts <= self.config.probe_retries

    def fail(self, session: Session, now: float) -> None:
        """The round got no answer: the session fails and probes again ``reprobe_after`` later."""

        session.state = FAILED
        session.retry_at = now + self.config.reprobe_after

    def begin_path_break(self, session: Session, hop: Optional[tuple[int, int]]) -> None:
        """The path broke: count it, remember the suspect hop and allow one detour round it."""

        session.counts["broken"] += 1
        session.note_path_broken(hop)

    def note_route_error(self, session: Session, hop: tuple[int, int]) -> None:
        """A relay reported that a path hop was unreachable (counted like an unacknowledged hop: ``note_first_hop_error`` is the shared per-hop counter, not only for the first hop)."""

        session.counts["route_errors"] += 1
        self.note_first_hop_error(session, hop)

    def note_first_hop_error(self, session: Session, hop: tuple[int, int]) -> None:
        """The gateway's own first hop went unacknowledged."""

        session.hop_errors[hop] = session.hop_errors.get(hop, 0) + 1

    def open_from_probe(
        self,
        session: Session,
        route: tuple[int, ...],
        links: tuple[LinkQuality, ...],
        now: float,
        next_keepalive: float,
    ) -> None:
        """A probe of this round came back: its path opens the session."""

        session.probe_ids.clear()
        session.route = route
        session.first_hop_skips = 0
        session.note_path_opened()
        session.links = links
        session.state = OPEN
        session.established_at = now
        session.probe_id = None
        session.keepalive_id = None
        session.next_keepalive = next_keepalive
        session.counts["established"] += 1

    def note_latency(self, session: Session, latency: "Estimate", within_target: bool) -> None:
        """Record the model for the path the session now uses."""

        session.latency = latency
        session.within_target = within_target

    def begin_fallback(self, session: Session, key: tuple[int, int], not_before: float) -> None:
        """The command falls back to a flood while the book waits to probe the new path."""

        session.state = PROBING
        session.probe_id = None
        session.fallback = key
        session.probe_not_before = not_before
        session.counts["fallbacks"] += 1

    def clear_fallback(self, session: Session, key: tuple[int, int]) -> bool:
        """The fallback for ``key`` came back; does a probe still wait for it?"""

        if session.fallback != key:
            return False
        session.fallback = None
        return True

    def skipped_out(self, session: Session) -> bool:
        """Has the first relay stayed silent ``skip_reprobe`` times in a row (path idle)?"""

        skip = self.config.skip_reprobe
        return (
            skip is not None
            and session.first_hop_skips >= skip
            and session.target not in self._inflight
            and session.waiting is None
        )

    def away_nodes(self) -> set[int]:
        """Nodes on the path of a session off the common channel: they cannot hear it (Q12)."""

        return {
            node
            for session in self._sessions.values()
            if session.state == OPEN and session.radio != 0
            for node in (*session.route, session.target)
        }

    def request_radio_change(self, session: Session, radio: Optional[int], sf: int, now: float) -> bool:
        """Ask the next keepalive to move the path to ``radio`` and ``sf``.

        ``radio`` is ``None`` when no radio arm is free. Only a setting that
        differs from the path's current one becomes wanted: asking for the
        current setting is not a change. The only caller
        (``Gateway._ask_radio``) comes here only with ``session.radio == 0``;
        the difference check on ``radio`` defends that premise rather than
        resting on it.
        A real request asks the next keepalive to go now.
        """

        if radio is not None and radio != session.radio:
            session.radio_wanted = radio
        if sf != session.sf:
            session.sf_wanted = sf
        requested = session.radio_wanted is not None or session.sf_wanted is not None
        if requested:
            session.next_keepalive = min(session.next_keepalive, now)
        return requested

    def return_to_common(self, session: Session, base_sf: int, now: float) -> bool:
        """Abandon another destination and ask the path back to radio 0 and ``base_sf`` now."""

        session.radio_wanted = 0 if session.radio != 0 else None
        session.sf_wanted = base_sf if session.sf != base_sf else None
        requested = session.radio_wanted is not None or session.sf_wanted is not None
        if requested:
            session.next_keepalive = min(session.next_keepalive, now)
        return requested

    def keepalive_target(self, session: Session) -> tuple[int, int]:
        """Take the setting this keepalive asks for and remember it as ``asked``."""

        radio = session.radio if session.radio_wanted is None else session.radio_wanted
        sf = session.sf if session.sf_wanted is None else session.sf_wanted
        session.asked = None if (radio, sf) == (session.radio, session.sf) else (radio, sf)
        return radio, sf

    def begin_keepalive(self, session: Session, keepalive_id: tuple[int, int], deadline: float) -> None:
        """A keepalive exchange is now out on the path, and counts as this session's keepalive."""

        session.keepalive_id = keepalive_id
        session.keepalive_deadline = deadline
        session.counts["keepalives"] += 1

    def note_keepalive_missed(self, session: Session) -> None:
        """The outstanding keepalive had no answer by its deadline."""

        session.counts["keepalive_misses"] += 1

    def keepalive_answered(self, session: Session, now: float) -> Optional[tuple[int, int]]:
        """The keepalive's answer is in: return the move it asked for, if any.

        With no answer move to follow, a setting requested meanwhile asks the
        next keepalive to go now.
        """

        session.keepalive_id = None
        asked = session.asked
        if asked is None and (session.radio_wanted is not None or session.sf_wanted is not None):
            session.next_keepalive = now
        return asked

    def follow_lost_keepalive(self, session: Session) -> tuple[int, int]:
        """A keepalive asked the path to move and was lost: follow the move it asked for."""

        assert session.asked is not None, "follow_lost_keepalive needs the move that was asked"
        session.keepalive_id = None
        session.counts["switch_follows"] += 1
        return session.asked

    def note_path_traffic(self, session: Session, now: float, keepalive_interval: float) -> None:
        """Traffic proved the path: the next keepalive can wait a full interval."""

        session.next_keepalive = now + keepalive_interval

    def switch_radio(self, session: Session, radio: int, sf: int, now: float) -> bool:
        """The path followed the setting its keepalive asked for; the session moves there.

        Any other destination wanted while the answer was in flight remains
        pending and asks the next keepalive to go now. The caller still owns
        the radio arms.
        """

        session.asked = None
        if radio != session.radio:
            session.counts["radio_switches"] += 1
            if radio:
                session.radio_since = now
        if sf != session.sf:
            session.counts["sf_switches"] += 1
        session.radio, session.sf = radio, sf
        if session.radio_wanted == radio:
            session.radio_wanted = None
        if session.sf_wanted == sf:
            session.sf_wanted = None
        pending = session.radio_wanted is not None or session.sf_wanted is not None
        if pending:
            session.next_keepalive = now
        return pending

    def leave_radio(self, session: Session, base_sf: int) -> None:
        """Give up every radio-side setting and return the session's view to the common one."""

        session.asked = None
        session.radio, session.radio_wanted = 0, None
        session.sf, session.sf_wanted = base_sf, None

    def note_radio_strike(self, session: Session) -> None:
        """A path broke soon after moving it onto the session radio."""

        session.counts["radio_strikes"] += 1

    def yield_radio(self, base_sf: int) -> None:
        """A one-radio gateway scans on the common setting: a session that slowed down there closes (Q11)."""

        for target, session in list(self._sessions.items()):
            if session.state == OPEN and session.radio == 0 and session.sf != base_sf:
                self.close(target)

    # ------------------------------------------------- messages over a session

    def dispatch(self, target: int, key: tuple[int, int], plain: bytes) -> None:
        """``plain`` goes out routed to ``target``: it is the message in flight there."""

        self._inflight[target] = (key, plain)

    def dispatch_command(
        self,
        session: Session,
        key: tuple[int, int],
        plain: bytes,
        now: float,
        keepalive_interval: float,
    ) -> None:
        """A routed command starts: its delivery starts now and it tests the path."""

        self.dispatch(session.target, key, plain)
        session.sent_at = now
        session.next_keepalive = now + keepalive_interval
        session.counts["routed"] += 1

    def take_inflight(self, target: int) -> Optional[tuple[tuple[int, int], bytes]]:
        return self._inflight.pop(target, None)

    def hold_command(self, session: Session, key: tuple[int, int], plain: bytes) -> None:
        """Keep ``key`` until the session's current exchange is over."""

        session.waiting = (key, plain)

    def take_waiting(self, session: Session) -> Optional[tuple[tuple[int, int], bytes]]:
        """Take the command waiting for the session's path, if there is one."""

        waiting = session.waiting
        session.waiting = None
        return waiting

    def release_inflight(self, key: tuple[int, int]) -> None:
        """``key`` was answered: it is no longer the message in flight (another one may be)."""

        inflight = self._inflight.get(key[0])
        if inflight is not None and inflight[0] == key:
            del self._inflight[key[0]]

    def record(self, key: tuple[int, int], delivery: Delivery) -> Delivery:
        """Keep ``delivery`` under ``key`` and, if its target has a session, add it to that session's ``deliveries`` (one caller today: Gateway)."""

        self._deliveries[key] = delivery
        session = self._sessions.get(delivery.target)
        if session is not None:
            session.deliveries.append(delivery)
        return delivery

    def finish(self, key: tuple[int, int]) -> Optional[Delivery]:
        return self._deliveries.pop(key, None)

    def finish_command(
        self,
        session: Session,
        delivery: Delivery,
        now: float,
        *,
        auto_trace: bool,
    ) -> None:
        """A command finished; remember when, and optionally wait to trace it."""

        session.last_finished_at = now
        if auto_trace:
            self.request_trace(session, delivery)

    def request_trace(self, session: Session, delivery: Delivery, *, bypass_interval: bool = False) -> None:
        """A command waits for the path before it can be traced; a manual request skips both delays."""

        session.trace_wanted = delivery
        if bypass_interval:
            session.last_trace_at = float("-inf")
            session.last_finished_at = float("-inf")

    def cancel_trace_request_if_path_changed(self, session: Session) -> bool:
        """The traced command went another way: the nodes on this path cannot answer for it."""

        wanted = session.trace_wanted
        if wanted is None or wanted.route == session.route:
            return False
        session.trace_wanted = None
        return True

    def begin_trace(
        self,
        session: Session,
        trace_id: tuple[int, int],
        deadline: float,
        now: float,
        keepalive_interval: float,
    ) -> Delivery:
        """The waiting trace starts: it tests the path, so the next keepalive can wait."""

        delivery = session.trace_wanted
        assert delivery is not None, "begin_trace needs a requested trace"
        session.trace_wanted = None
        session.trace_id = trace_id
        session.trace_of = delivery
        session.trace_deadline = deadline
        session.last_trace_at = now
        session.next_keepalive = now + keepalive_interval
        return delivery

    def finish_trace(self, session: Session, result: TraceResult, keepalive_interval: float) -> None:
        """The active trace was answered: attach its result and defer the next keepalive."""

        delivery = session.trace_of
        assert delivery is not None, "finish_trace needs an active trace"
        delivery.trace = result
        session.trace_id = session.trace_of = None
        session.next_keepalive = result.received_at + keepalive_interval

    def expire_trace(self, session: Session, now: float) -> None:
        """The active trace had no answer by its deadline (diagnostics only)."""

        if session.trace_of is not None:
            session.trace_of.events.append((now, "追踪无回应", None))
        session.trace_id = session.trace_of = None

    def interrupt_trace(self, session: Session, now: float) -> None:
        """The active trace was on the path that broke, so it ends here."""

        if session.trace_of is not None:
            session.trace_of.events.append((now, "追踪中断（路径断了）", None))
        session.trace_id = session.trace_of = None

    def forget_waiting(self, key: tuple[int, int]) -> None:
        """The command ``key`` is over: it no longer waits for its session's path."""

        session = self._sessions.get(key[0])
        if session is not None and session.waiting is not None and session.waiting[0] == key:
            session.waiting = None

    def settle(self, target: int) -> None:
        """Drop what the gateway keeps of ``target``'s messages the controller no longer waits on.

        The controller is stop-and-wait per node, so only the message it
        awaits is live. Without this, the delivery of a query that got no
        answer stayed in ``_open`` for good (each round asks under a new
        id), and once the per-node counter came round a new command was
        matched to it (an old start time: judged faulty, traced); a command
        given up on left ``_inflight`` set, which holds back keepalives,
        traces and the skipped-hop check (2026-10-21 review).
        """

        for key in [key for key in self._deliveries if key[0] == target and self._stale(key)]:
            self._deliveries.pop(key)  # superseded, not failed: no trace of its own
        inflight = self._inflight.get(target)
        if inflight is not None and self._stale(inflight[0]):
            del self._inflight[target]
