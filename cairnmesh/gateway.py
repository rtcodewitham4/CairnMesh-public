"""Simulated gateway: routed sessions, flood pacing and the latency model.

``Gateway`` is the ``Controller``'s transport and sits on the gateway's
``SecureTransport``. It owns what the controller must not: timing next to
the radio (``gateway-board-architecture``). The controller stays semantic.

* **Sessions.** ``open_session(node)`` floods a PROBE; the target answers
  with the path of fewest expected sends (``probe_metric`` "etx", ``mesh.py``). While the session
  is open, requests and wakes to that node travel routed along the path and
  a KEEPALIVE runs every ``keepalive_interval`` (skipped while traffic shows
  the path works). A ROUTE_ERROR, an unacknowledged first hop or a keepalive
  that goes unanswered breaks the session: the message in flight falls back
  to flooding at once, and the gateway probes again with a fresh id. So does
  a first hop that ``skip_reprobe`` times in a row was taken over by a relay
  further along (the first relay stayed silent: dead or fading out), once
  the path is idle. A session unused for ``idle_timeout`` closes (KEEPALIVE
  with lease 0).
* **One flood at a time.** Floods that expect an answer (REQUEST, WAKE,
  PROBE, a scan's DISCOVER) are queued and sent one by one: the next goes out when the answer
  to the previous arrives or after ``flood_hold``. A controller retry of the
  flood in flight is swallowed while that flood can still be answered, and
  one of a routed command while its routed copy is within expected time.
* **One session at a time** (user 2026-10-20). Opening another closes the
  one open.
* **Radios** (tasks Q11, Q12, B4.5). With a second radio, radio 0 keeps
  the common channel and the second carries the session on a channel of
  its own, at the SF its weakest hop needs; a KEEPALIVE's RADIO byte moves
  the path. A scan calls the session back while it asks nodes one by one.
* **Latency model.** ``LatencyModel`` computes, from the same airtime
  formula the simulated medium uses, what a routed exchange should take
  (no loss, and one repeat on every hop), how long a broken path takes to
  notice and how much of the channel a session's keepalive takes. The
  gateway derives its own timeouts from it, and ``Session.latency`` is what
  a user interface shows, with ``within_target`` against the 5 s goal.

Every timing default suits the simulator only.
"""

from __future__ import annotations

import math
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable, Optional, Sequence

from .airtime import airtime_for
from .frames import (
    BROADCAST_ID,
    DEFAULT_CONTROLLER_ID,
    DISCOVER_SPREAD_UNIT,  # noqa: F401 - historical gateway import surface
    MAX_RELAYS,
    PROBE_WAKE,
    PROBE_WINDOW_UNIT,
    DiscoverFrame,
    DiscoverReplyFrame,
    FrameKind,
    JoinFrame,
    KeepaliveFrame,
    LinkQuality,
    ProbeFrame,
    ProbeHop,
    ProbeReplyFrame,
    ReplyFrame,
    ResultFrame,
    ReplyResult,
    RouteErrorFrame,
    TraceFrame,
    TraceReplyFrame,
    WakeNetFrame,
    add_route,
    radio_byte,
    decode_routed,
    is_routed,
    reply_route,
    split_route,
)
from .latency import (  # noqa: F401 - historical gateway import surface
    SEAL_OVERHEAD, SF_STEP_DB, Estimate, LatencyModel, margin_db,
)
from .linkqa import (  # noqa: F401 - historical gateway import surface
    ACCEPT_DETOUR_DB, ACCEPT_LONG, ACCEPT_SHORT, ACCEPT_SHORT_HOPS,
    DESIGN_INTERFERENCE, HEALTH_MIN_ATTEMPTS, HEALTH_REPEATS, HEALTH_WINDOW, HEALTH_Z,
    LINK_DOWN, LINK_GOOD, LINK_LOSSY, LINK_WEAK_SIGNAL,
    Acceptance, Health, HealthBook, HopCheck, LinkTest, LinkTestBook, NotAccepted, PathView,
    assess as _assess_linkqa,
)
from .mesh import node_report
from .nodebook import KnownNode, NodeBook, WeakLink  # noqa: F401 - KnownNode is re-exported (public surface)
from .secure import APP_OVERHEAD
from .scan_engine import (
    JoinStorm,
    Scan,
    interval_due,
    missing_nodes,
    rescan_due,
    round_plan,
    start_due,
)
from .session_book import (
    CLOSED,
    FAILED,
    OPEN,
    PROBING,
    Delivery,
    Session,
    SessionBook,
    SessionConfig,
    TraceResult,
)
from .transport import Receiver, Transport

if TYPE_CHECKING:  # pragma: no cover
    from .controller import Controller
    from .mesh import MeshRelay

#: Answers to frames the gateway sends itself (keepalives, the closing one too,
#: probes, traces) never reach the controller: their ids come off the same
#: per-node counter as commands (2026-10-21 review A1). Per node: about 2 min
#: of keepalives.
OWN_IDS_KEPT = 64


@dataclass
class _Flood:
    key: tuple[int, int]
    plain: bytes
    until: float = 0.0
    #: How long it holds the queue (default ``flood_hold``; a scan, its answers' window).
    hold: Optional[float] = None


def _message_id(plain: bytes) -> int:
    return plain[4] << 8 | plain[3]


@dataclass
class _Arm:
    """One radio of the gateway (task B4.5)."""

    transport: Transport
    relay: "MeshRelay"
    channel: int
    #: The session on (or promised) this radio; radio 0 is shared.
    holder: Optional[int] = None
    # The relay next to the gateway repeats every routed frame into it
    # right after the first copy: nothing goes out on this radio before
    # that repeat is over, or it would land on top of it.
    # [MEM] frames held back meanwhile | 端: gateway | 上限: 每节点 1 帧 + 保活 | 每项: ≤55 B 帧
    quiet_until: float = 0.0
    held: list = field(default_factory=list)
    #: Closing a session on a slower setting: the radio stays on it until the
    #: answer to the closing keepalive (``leave_id``) is in, or this time;
    #: only the first ``leave_mark`` held frames (sent before) go out meanwhile.
    leaving_until: Optional[float] = None
    leave_id: Optional[tuple[int, int]] = None
    leave_mark: int = 0


class Gateway:
    """The controller's transport: routes sessions, paces floods, keeps time."""

    def __init__(
        self,
        transport: Transport,
        relay: "MeshRelay",
        controller_id: int = DEFAULT_CONTROLLER_ID,
        *,
        keepalive_interval: float = 2.0,
        lease: int = 60,
        probe_window: float = 1.0,
        probe_timeout: Optional[float] = None,
        probe_retries: int = 2,
        reprobe_after: float = 10.0,
        idle_timeout: Optional[float] = 60.0,
        flood_hold: float = 3.0,
        flood_settle: float = 2.0,
        fallback_window: float = 25.0,
        retry_allowance: int = 4,
        auto_trace: bool = True,
        trace_repeats: bool = False,
        trace_delay: float = 1.0,
        trace_interval: float = 10.0,
        serialize_floods: bool = True,
        margin: float = 0.1,
        expected_loss: float = 0.0,
        turnaround: float = 0.005,
        realtime_target: float = 5.0,
        scan_spread_factor: float = 3.0,
        scan_followups: int = 16,
        scan_rounds: int = 3,
        scan_routed: bool = True,
        expected_nodes: int = 0,
        scan_at_start: bool = False,
        scan_interval: Optional[float] = None,
        rescan_after: Optional[float] = 120.0,
        rescans: int = 2,
        join_storm: Optional[int] = 3,
        join_storm_window: float = 15.0,
        skip_reprobe: Optional[int] = 2,
        detours: bool = True,
        session_sf: bool = True,
        sf_margin: float = 4.0,
        max_sf: int = 10,
        test_patience: float = 90.0,
        require_acceptance: bool = False,
        detour_margin: float = 3.0,
        detour_extra_hops: int = 2,
        recall_wait: float = 8.0,
        radio_trial: float = 30.0,
        radio_strikes: int = 2,
        radio_strike_window: float = 600.0,
        radio_ban: float = 600.0,
        wake: Optional[str] = None,
        wake_lease: int = 20,
        wake_settle: float = 2.5,
        wake_window: Optional[float] = 4.0,
        probe_hops: int = 8,
        scan_wake_lease: int = 120,
        scan_wake_settle: float = 10.0,
        radios: Sequence[tuple[Transport, "MeshRelay"]] = (),
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not 0 <= lease <= 0xFF:
            raise ValueError("lease must fit one byte (seconds)")
        if len(radios) > 1:
            raise ValueError("one session at a time (user 2026-10-20): one session radio at most")
        self._transport = transport
        self.relay = relay
        #: Task B4.5: the gateway's radios, one or two (user 2026-10-20: the
        #: board has two). Radio 0 (``transport``, ``relay``) stays on the
        #: common channel when there is a second; that one (``radios``)
        #: carries the session on channel 1 (Q12). With one radio the session
        #: stays on channel 0 (moving would leave the gateway deaf to REPORTs
        #: and JOINs) and only Q11's SF moves it.
        self._arms = [_Arm(transport, relay, 0)] + [
            _Arm(port, arm_relay, k) for k, (port, arm_relay) in enumerate(radios, start=1)
        ]
        for arm in self._arms[1:]:
            arm.relay.set_radio(arm.channel, arm.relay.base_sf)  # off the common channel: never a copy of radio 0
        self.controller_id = controller_id
        self.node_book = NodeBook(controller_id)
        self.nodes = self.node_book.nodes
        self.probed = self.node_book.probed
        self.keepalive_interval = keepalive_interval
        self.lease = lease
        self.probe_window = probe_window
        self.flood_hold = flood_hold
        #: After a flood is answered, its last copies (reply relays repeat after
        #: ``hop_timeout``) still circulate: the next flood waits this long.
        self.flood_settle = flood_settle
        self._settled_at = 0.0
        #: Longest the controller keeps retrying a session message (fallback included).
        #: 25 s = the controller's own cap (``DEFAULT_MAX_RETRY_WINDOW``)
        #: (user, 2026-10-20: random 40 at 5 % loss 98.8 → 99.6 %, 9 % more sends).
        self.fallback_window = fallback_window
        #: Hop repeats one end-to-end try leaves room for.
        self.retry_allowance = retry_allowance
        #: Policy A (user, 2026-10-18): trace a command that went wrong
        #: (``Delivery.faulty``; with ``trace_repeats`` also one that merely
        #: needed hop repeats), once the path has been free for ``trace_delay``,
        #: nothing is queued for the node, and at most every ``trace_interval``
        #: per session; ``trace()`` asks by hand. Measured on 17 hops at 5 %
        #: loss, where nearly every command repeats somewhere: tracing those
        #: too cut delivery from 97 % to 55 % (commands waited behind traces).
        self.auto_trace = auto_trace
        self.trace_repeats = trace_repeats
        self.trace_delay = trace_delay
        self.trace_interval = trace_interval
        self.serialize_floods = serialize_floods
        #: Task C5: after a break that points at a hop, probe first along the
        #: cheapest known path round it (``_detour``); a flood (or a wake
        #: probe to everyone) only when that fails.
        self.detours = detours
        #: Task Q11 (user, 2026-10-19): a session runs at one SF for its whole
        #: path, the fastest whose weakest hop keeps ``sf_margin`` dB over that
        #: SF's floor (2.5 dB more per step), up to ``max_sf``. On a one-radio
        #: gateway a scan closes a session that slowed down (``SessionBook.yield_radio``).
        self.session_sf = session_sf
        self.sf_margin = sf_margin
        self.max_sf = max_sf
        self._models: dict[int, LatencyModel] = {}
        self.detour_margin = detour_margin
        #: Task Q12: nodes on the session's channel hear nothing on the common
        #: one, so nodes behind its path cannot answer a scan: before asking
        #: silent nodes one by one, a scan calls the session back to the
        #: common setting (waiting at most ``recall_wait`` s).
        self.recall_wait = recall_wait
        #: A session that breaks within ``radio_trial``
        #: s of moving onto the session radio counts a strike against its
        #: target there. ``radio_strikes`` strikes no more than
        #: ``radio_strike_window`` s apart, with no healthy traffic between,
        #: keep that target on the common channel for ``radio_ban`` s
        #: (single-radio pace, 15.2). Healthy traffic on the session channel
        #: after the trial clears the target's strikes.
        self.radio_trial = radio_trial
        self.radio_strikes = radio_strikes
        self.radio_strike_window = radio_strike_window
        self.radio_ban = radio_ban
        # [MEM] session-channel strikes and bans | 端: gateway(PC) | 上限: 节点数 | 每项: 最近 strike 时刻 + 截止时刻
        self._radio_strikes: dict[int, list[float]] = {}
        self._radio_banned: dict[int, float] = {}
        self.detour_extra_hops = detour_extra_hops
        #: Task D3, the network sleeps. ``"net"``: before probing, wake the
        #: whole network for ``wake_lease`` seconds (WAKE_NET) unless it is
        #: still awake, and probe ``wake_settle`` later, as often as the
        #: probe retries allow while the wake spreads; the session then keeps
        #: its path awake and everyone else falls asleep with the lease.
        #: ``"probe"`` (what ``MeshNetwork`` uses): every probe is a directed
        #: wake probe (``PROBE_WAKE``, long preamble), window ``wake_window``
        #: (4 s measured best at T = 0.5 s; ``None``: ``probe_window`` plus the
        #: preamble). ``None``: the network is always awake.
        if wake not in (None, "net", "probe"):
            raise ValueError(f"wake must be None, 'net' or 'probe', got {wake!r}")
        self.wake = wake
        self.wake_lease = wake_lease
        self.wake_settle = wake_settle
        self.wake_window = wake_window
        #: Deepest a first probe is expected to go when the node book does not
        #: know the target (≤ 40 nodes): a 17-hop timeout left a lost probe
        #: unretried for 15 s (cost-ordered relays wait up to 0.6 s a hop,
        #: A5), for a minute with wake probes. A later answer still counts.
        self.probe_hops = probe_hops
        #: A scan on a sleeping network (either ``wake`` mode) first wakes it
        #: all for ``scan_wake_lease`` seconds (renewed while the scan runs)
        #: and sends the first DISCOVER ``scan_wake_settle`` later, once the
        #: wake has spread (task D5; about 5–8 s for 40 nodes at T = 0.5 s).
        self.scan_wake_lease = scan_wake_lease
        self.scan_wake_settle = scan_wake_settle
        #: Until when the last WAKE_NET keeps the network awake (estimate).
        self.awake_until = -math.inf
        self._wake_sequence = 0
        self.margin = margin
        #: Per-hop loss the reported expected latency allows for.
        self.expected_loss = expected_loss
        self.clock = clock
        self.model = LatencyModel(
            relay.airtime,
            jitter=relay.route_jitter,
            slack=relay.route_slack,
            answer=relay.route_answer,
            turnaround=turnaround,
            target=realtime_target,
            retries=relay.route_retries,
            app=APP_OVERHEAD if getattr(transport, "app", None) is not None else 0,
            last_hop_ack=relay.last_hop_ack,
        )
        if probe_timeout is None:
            # Long enough for the longest path a route can hold (17 hops).
            probe_timeout = self.model.probe(MAX_RELAYS + 1, *self._probe_timing())
        self.probe_timeout = probe_timeout
        self.controller: Optional["Controller"] = None
        self._receiver: Optional[Receiver] = None
        #: The sessions, the message in flight over each and the deliveries open
        #: live in ``book``, its only writer; this is a read-only view.
        self.book = SessionBook(
            SessionConfig(
                idle_timeout=idle_timeout,
                probe_retries=probe_retries,
                reprobe_after=reprobe_after,
                skip_reprobe=skip_reprobe,
            ),
            hang_up=self._hang_up,
            probe=self._probe,
            stale=self._stale,
            log=self._log,
        )
        self.sessions = self.book.sessions
        # [MEM] routes RESULT frames came in on, for their acknowledgement | 端: gateway | 上限: 8 | 每项: (节点, 消息 ID) + ≤16 B 路由 + 射频
        self._result_routes: dict[tuple[int, int], tuple[tuple[int, ...], int]] = {}
        # [MEM] ids of the gateway's own frames | 端: gateway | 上限: OWN_IDS_KEPT(64) × 节点数 | 每项: 2 B
        self._own_ids: dict[int, OrderedDict[tuple[int, int], None]] = {}
        # [MEM] flood queue | 端: gateway | 上限: 每节点 1 帧在途（控制端停等）× 节点数 | 每项: ≤55 B 帧
        self._floods: deque[_Flood] = deque()
        self._flood: Optional[_Flood] = None
        #: ``(time, event, node)`` for demos and tests: the last ``max_history``
        #: only; ``history_base`` counts the ones dropped from the front.
        # [MEM] event history | 端: gateway(PC) | 上限: max_history(默认 2000) | 每项: 时间 + 事件名 + 节点
        self.history: list[tuple[float, str, int]] = []
        self.history_base = 0
        self.max_history = 2000
        self.stats = {
            "routed": 0,
            "flooded": 0,
            "flood_retries_swallowed": 0,
            "routed_retries_swallowed": 0,
            "stale_failures": 0,
            "bad": 0,
            "traces": 0,
            "wakes": 0,
            "auto_opened": 0,
            "link_tests": 0,
            "own_answers": 0,
            "radio_bans": 0,
        }
        self.link_tests_book = LinkTestBook(self.clock, self._publish, test_patience)
        #: Link tests by target (``test_link``), finished ones kept until the next test of that target.
        self.link_tests = self.link_tests_book.link_tests
        #: Refuse commands to nodes that have not passed acceptance (``NotAccepted``).
        self.require_acceptance = require_acceptance
        #: Latest acceptance per node (``accept``), and the tests that will end in one.
        self.acceptance: dict[int, Acceptance] = {}
        self._accepting = self.link_tests_book._accepting
        #: Layer 2: each node's health in operation (``Health``).
        self.health_book = HealthBook(self.clock, self._publish)
        self.health = self.health_book.health
        #: Task C: answer window = ``scan_spread_factor`` × the gateway hop's
        #: busy time (``LatencyModel.scan_spread``); at most ``scan_followups``
        #: silent known nodes are asked again one by one.
        self.scan_spread_factor = scan_spread_factor
        self.scan_followups = scan_followups
        #: More silent nodes than ``scan_followups`` after a round: everyone
        #: again (re-sized), up to ``scan_rounds`` rounds in all.
        self.scan_rounds = scan_rounds
        #: Ask a silent node one by one with a routed PROBE along the cheapest
        #: path the node book knows (``_book_path``), not a DISCOVER flood that
        #: every node carries; the flood only when that PROBE stays unanswered.
        self.scan_routed = scan_routed
        #: Nodes the operator expects (installed): every scan's window is sized
        #: for at least this many. A scan cannot learn the network's size before
        #: its own answers come in (measured: a JOIN storm's scan sized for the
        #: three nodes that set it off lost most answers).
        self.expected_nodes = expected_nodes
        #: Scan on the first tick, and again ``scan_interval`` after each scan
        #: ends (user, 2026-10-19: events + a slow period, default an hour).
        self.scan_at_start = scan_at_start
        self.scan_interval = scan_interval
        #: A5: a scan that ends knowing fewer than ``expected_nodes`` is
        #: repeated ``rescan_after`` seconds later, ``rescans`` times at most
        #: (after a power-on storm 3–5 % stayed unknown: a node whose scan
        #: answer was lost counts itself joined and never JOINs).
        self.rescan_after = rescan_after
        self.rescans = rescans
        self._rescans_done = 0
        #: ``join_storm`` JOINs within ``join_storm_window`` (an area back from a
        #: power cut) start a scan: every JOIN is a flood (≈ N² sends for N
        #: nodes, measured 4 800–26 000 sends for 60 nodes), a scan ≈ N + Σ hops,
        #: and a node that answers a scan stops joining. ``None`` turns it off.
        self.join_storm = join_storm
        self.join_storm_window = join_storm_window
        self._join_storm = JoinStorm(join_storm, join_storm_window)
        #: The scan running, or the last one.
        self.scan_state: Optional[Scan] = None
        self._scan_sequence = 0
        self._scan_key: tuple[int, int] = (BROADCAST_ID, -1)
        for k, arm in enumerate(self._arms):
            arm.transport.set_receiver(lambda plain, k=k: self._on_frame(plain, k))
            arm.relay.route_failed = lambda plain, broken, k=k: self._on_route_failed(plain, broken, k)
            arm.relay.route_event = self._on_route_event
            arm.relay.path_found = self._on_path
            arm.relay.observers.append(self._on_heard)

    # The session book reads these four; they are the arguments of the same
    # names, read through ``book.config`` (set them there, not here).

    @property
    def idle_timeout(self) -> Optional[float]:
        return self.book.config.idle_timeout

    @property
    def probe_retries(self) -> int:
        return self.book.config.probe_retries

    @property
    def reprobe_after(self) -> float:
        return self.book.config.reprobe_after

    @property
    def skip_reprobe(self) -> Optional[int]:
        return self.book.config.skip_reprobe

    @property
    def test_patience(self) -> float:
        return self.link_tests_book.test_patience

    @test_patience.setter
    def test_patience(self, seconds: float) -> None:
        self.link_tests_book.test_patience = seconds

    def attach(self, controller: "Controller") -> None:
        """Probes and keepalives take their message ids from ``controller``.

        The gateway also learns from it which commands it gave up on.
        """

        from .events import ReplyReceived, RequestCompleted, RequestConfirming, RequestFailed, RequestQuerying

        self.controller = controller
        if hasattr(controller, "awaits_request_answer"):
            # A plain answer to a command still awaited is a forgery (review B1).
            for arm in self._arms:
                if hasattr(arm.transport, "awaits_request_answer"):
                    arm.transport.awaits_request_answer = controller.awaits_request_answer
        if hasattr(controller, "round_wakes"):
            controller.round_wakes = self.round_wakes
            controller.timeouts_for = self.timeouts_for

        def message(event) -> tuple[int, int]:
            # The event's own EPOCH: a request restored after a restart has an older one.
            epoch = controller.epoch if getattr(event, "epoch", None) is None else event.epoch
            return event.node_id, epoch << 8 | event.sequence

        def on_event(event) -> None:
            if isinstance(event, RequestFailed):
                key = message(event)
                self._finish(key, False, "控制端放弃（重试用尽）")
                self._forget(key)
            elif isinstance(event, RequestQuerying):
                # The controller stopped retrying the command and asks about it
                # instead (Q18): its delivery ends here, and copies still queued
                # as floods would only spend air (the device answers them, or
                # refuses them once the query has said NOT_RUN).
                key = message(event)
                self._finish(key, False, "回答未到，转查询")
                self._forget(key)
            if isinstance(event, (RequestFailed, RequestQuerying, RequestConfirming, ReplyReceived, RequestCompleted)):
                self.book.settle(event.node_id)

        controller.events.subscribe(on_event)

    # ---------------------------------------------------------------- sessions

    def open_session(self, target: int) -> Session:
        """Start (or keep) a routed session with ``target``; it is usable once ``OPEN``.

        One session at a time (user 2026-10-20): another one open is closed
        first. Two at once shared relays, collided on the gateway's radio and
        could not both slow down (``protocol_spec.md`` 15.5).
        """

        return self.book.open(target, self.clock())

    def close_session(self, target: int) -> None:
        self.book.close(target)

    def _hang_up(self, session: Session) -> None:
        """The radio side of closing ``session`` (already out of the book): the lease-0 KEEPALIVE, the radio back."""

        target = session.target
        if session.state == OPEN:
            sequence, epoch = self._take_id(target)
            frame = KeepaliveFrame(self.controller_id, target, sequence, lease=0, epoch=epoch)
            arm = self._arms[session.radio]
            self._emit(add_route(frame.encode(), session.route), session.radio)  # best effort
            if session.radio == 0 and session.sf != self.relay.base_sf:
                # The relay sends it, and its repeats, on later ticks, and the
                # path comes home only as the answer passes (RADIO 0 = common):
                # stay until then (measured: switching at once sent every copy
                # at SF7 to a path still at SF8–9, which then stayed deaf to
                # the common setting for ``radio_timeout``; 17 of 123 hop
                # give-ups on random 40 at 5 % loss).
                worst = self._model_for(session.sf).keepalive(session.hops).worst + self.margin
                arm.leave_id, arm.leave_mark = (sequence, epoch), len(arm.held)
                arm.leaving_until = self.clock() + min(worst, self.relay.radio_timeout)
        self._leave_radio(session)  # the path comes back once it has answered, or by itself

    def session(self, target: int) -> Optional[Session]:
        return self.sessions.get(target)

    def timeouts_for(self, target: int) -> tuple[float, int]:
        """``(timeout, max_retries)`` for the controller's next frame to ``target``."""

        session = self.sessions.get(target)
        window = self.fallback_window
        limit = getattr(self.controller, "max_retry_window", None)
        if limit is not None:
            window = min(window, limit)
        if session is not None and session.state == PROBING:
            # The path is being found (again, after a break): a flood every
            # ``flood_hold`` until it opens, then the retry goes over it.
            # Measured (random 40, 5 % loss): with 4 tries (12 s) most failed
            # commands gave up while the re-probe was still out, one 0.1 s
            # before the path opened.
            return self.flood_hold, max(3, math.floor(window / self.flood_hold) - 1)
        if session is None or session.state != OPEN or session.latency is None:
            return self.flood_hold, 3
        # One try: the expected time plus room for ``retry_allowance`` hop
        # repeats (every hop repeating at once is the model's worst case,
        # far too rare to wait for on long paths: 14 s on 17 hops).
        hops = session.hops
        model = self._model_for(session.sf)
        longest = max(
            model.hop_timeout(model.lengths(hops)["request"], left, True) for left in range(1, hops + 1)
        )
        allowance = session.latency.expected + self.retry_allowance * longest
        timeout = min(session.latency.worst, allowance) + self.margin
        # Then room, after the path breaks mid-message, for the fallback
        # flood, the re-probe and the retry over the new path: up to
        # ``fallback_window``, never past the controller's own limit.
        return timeout, max(0, math.floor(window / timeout) - 1)

    def request(
        self,
        target: int,
        action: int,
        value: int,
        force: bool = False,
        past_confirming: bool = False,
        important: bool = False,
    ):
        """``Controller.request`` with the timeouts this gateway computes for ``target``.

        With ``require_acceptance``, a node without a passed acceptance is
        refused (``NotAccepted``) unless ``force``. A node with a command still
        being confirmed is refused (``NodeConfirming``, layer 3) unless
        ``past_confirming``. Two flags, not one (review D4, user's decision):
        an operator skipping acceptance must not go past a confirming command
        without knowing it. ``important``: the controller confirms this command on to the
        devices' keep period, not only to its ``confirm_limit``.
        """

        if self.controller is None:
            raise RuntimeError("attach(controller) first")
        if self.require_acceptance and not force:
            accepted = self.acceptance.get(target)
            if accepted is None or not accepted.passed:
                why = "还没做验收" if accepted is None else "；".join(accepted.reasons) or "验收未通过"
                raise NotAccepted(f"节点 0x{target:02X} 未通过部署验收：{why}")

        timeout, retries = self.timeouts_for(target)
        return self.controller.request(
            target, action, value, timeout=timeout, max_retries=retries, force=past_confirming, important=important
        )

    # ---------------------------------------------------------------- Transport

    def set_receiver(self, callback: Receiver) -> None:
        self._receiver = callback

    def send(self, data: bytes) -> None:
        """A plain frame from the controller."""

        plain = bytes(data)
        if len(plain) >= 5 and plain[0] in (FrameKind.REQUEST, FrameKind.WAKE):
            target = plain[2]
            key = (target, _message_id(plain))
            # From now on that id is the controller's (only after the counter went round).
            self._own_ids.get(target, {}).pop((plain[3], plain[4]), None)
            session = self.sessions.get(target)
            if session is not None:
                session.last_used = self.clock()
            if session is not None and session.state == OPEN:
                if session.keepalive_id is not None or session.trace_id is not None:
                    self.book.hold_command(session, key, plain)  # goes when that exchange is over
                    return
                inflight = self.book.inflight.get(target)
                if (
                    inflight is not None
                    and inflight[0] == key
                    and session.latency is not None
                    and self.clock() < session.sent_at + session.latency.expected
                ):
                    # A retry timed for the flood the command first waited as,
                    # while its routed copy is still within time: a second
                    # copy would meet the first head-on on the path.
                    self.stats["routed_retries_swallowed"] += 1
                    return
                self._send_command(session, key, plain)
                return
            if self._hold_for_path(target, key, plain):
                return
            self._submit_flood(key, plain)
            return
        if len(plain) == 6 and plain[0] == FrameKind.REPLY:
            back = self._result_routes.get((plain[2], _message_id(plain)))
            if back is not None:
                self._emit(add_route(plain, back[0]), back[1])
                return
        self._emit(plain)

    def _hold_for_path(self, target: int, key: tuple[int, int], plain: bytes) -> bool:
        """Q20: a sleeping network hears no ordinary flood, so a command waits for a path.

        No session (or a failed one) to ``target``: one is opened (a wake
        probe) and the command goes when it is (``_release_waiting``), as
        after a break. Retries while it probes flood as before.
        Measured (random 40 dormant, 30 % loss): flooded into the sleeping
        network, 47 of 192 commands never reached anyone. Another session
        with a command on the air is not closed for this: that one floods
        as before.
        """

        now = self.clock()
        if self.wake is None or now < self.awake_until:
            return False
        session = self.sessions.get(target)
        if session is None or session.state == CLOSED:
            busy = any(
                other.target != target and (other.waiting is not None or other.target in self.book.inflight)
                for other in self.sessions.values()
            )
            if busy:
                return False
            session = self.open_session(target)
            self.stats["auto_opened"] += 1  # not a history entry: those are session states for the UI
        elif session.state == FAILED:
            self.book.reset_probe_attempts(session)
            self._probe(session, now)
        else:
            # Probing: its wake probe has the nodes it passed up for a while,
            # and floods meanwhile do reach (measured: holding these too cut
            # direct answers 187 → 163 on grid 40 and doubled the unconfirmed
            # on random 40).
            return False
        self.book.hold_command(session, key, plain)
        session.last_used = now
        delivery = self.book.deliveries.get(key)
        if delivery is not None:
            delivery.events.append((now, "网络在睡：不泛洪，等路径", None))
        return True

    def start(self) -> None:
        """No-op; lets a session start and stop this layer like a port."""

    # ---------------------------------------------------------------- driving

    def tick(self, now: Optional[float] = None) -> None:
        now = self.clock() if now is None else now
        for arm in self._arms:
            if arm.leaving_until is not None and now >= arm.leaving_until:
                arm.leaving_until, arm.leave_id, arm.leave_mark = None, None, 0
                arm.relay.set_radio(arm.channel, arm.relay.base_sf)
            if arm.held and now >= arm.quiet_until:
                count = len(arm.held) if arm.leaving_until is None else arm.leave_mark
                held, arm.held = arm.held[:count], arm.held[count:]
                arm.leave_mark = 0
                for data in held:
                    if not self._stale_copy(data):
                        arm.transport.send(data)
        flood = self._flood
        if flood is not None and now >= flood.until:
            self._flood = None
        self._pump(now)
        self._scan_tick(now)
        self._test_tick(now)
        for session in list(self.sessions.values()):
            if self.book.idle(session, now):
                self.close_session(session.target)
                continue
            if session.state == PROBING and session.probe_id is None:
                if now >= session.probe_not_before:
                    self._probe(session, now)
            elif session.state == PROBING and now >= session.probe_deadline:
                if self.book.may_probe_again(session):
                    self._probe(session, now)
                else:
                    self.book.fail(session, now)
                    self._log("failed", session.target)
                    health = self._health(session.target)
                    health.failed_probes += 1
                    if health.failed_probes >= HEALTH_REPEATS:
                        self._warn(session.target, "unreachable", f"路径连续 {health.failed_probes} 轮探测无回应")
            elif session.state == FAILED and now >= session.retry_at:
                self.book.reset_probe_attempts(session)
                self._probe(session, now)
            elif session.state == OPEN:
                if session.trace_id is not None:
                    if now >= session.trace_deadline:
                        # Diagnostics only: a lost trace breaks nothing (the
                        # keepalive watches the path).
                        self.book.expire_trace(session, now)
                        self._release_waiting(session)
                elif session.keepalive_id is not None:
                    if now >= session.keepalive_deadline:
                        self.book.note_keepalive_missed(session)
                        self._test_result(session, now, answered=False)
                        if session.asked is not None:
                            # It asked the path to move: the path moves once it
                            # has passed the answer on, so a lost answer most
                            # likely means everyone but us moved (measured: two
                            # answers colliding at the gateway left both paths
                            # deaf for 6 s, again and again). Follow and ask
                            # there once; a miss there is a break.
                            asked = self.book.follow_lost_keepalive(session)
                            self._switch_session(session, *asked)
                            self._keepalive(session, now)
                        else:
                            self._broken(session, None, now, "keepalive_miss")
                elif self.book.skipped_out(session):
                    first = session.route[0] if session.route else session.target
                    self._broken(session, None, now, "first_hop_skipped", (self.controller_id, first))
                elif self._trace_ready(session, now):
                    self._send_trace(session, now)
                elif session.target not in self.book.inflight and (
                    now >= session.next_keepalive or self._test_due(session, now)
                ):
                    # A command in flight already tests the path; a keepalive
                    # beside it would only compete with it for the channel.
                    self._keepalive(session, now)

    # ---------------------------------------------------------------- incoming

    def round_wakes(self, target: int) -> bool:
        """Would a frame to ``target`` now have to wake the sleeping network (no open session, Q20)?"""

        if self.wake is None or self.clock() < self.awake_until:
            return False
        session = self.sessions.get(target)
        return session is None or session.state != OPEN

    def _on_heard(self, plain: bytes, rssi: Optional[float]) -> None:
        """Relay observer: any frame from a node (HELLO, JOIN, answers, relayed copies) means it is reachable.

        Layer 3: a command to it still being confirmed is asked about now
        (``Controller.nudge``) instead of at the next scheduled round.
        """

        nudge = getattr(self.controller, "nudge", None)
        if nudge is None:
            return
        if is_routed(plain):
            try:
                sender = decode_routed(plain)[0].sender_id
            except ValueError:
                return
        elif plain[0] in (FrameKind.PROBE, FrameKind.JOIN, FrameKind.DISCOVER) and len(plain) >= 6:
            sender = plain[5]  # ORIGIN: SENDER is the last relay that passed it on
            if plain[0] == FrameKind.JOIN and len(plain) > 6 and plain[6] & PROBE_WAKE:
                # A node booted (a wake JOIN, Q22): power came back around it,
                # maybe for others too (an area cut; their JOINs held back
                # behind this one). Not on every JOIN: a node that never gets
                # in rejoins every 300 s and kept every round on the fast pace.
                nudge_all = getattr(self.controller, "nudge_all", None)
                if nudge_all is not None:
                    nudge_all()
        elif len(plain) >= 2:
            sender = plain[1]
        else:
            return
        if sender not in (self.controller_id, BROADCAST_ID):
            nudge(sender)

    def _on_frame(self, plain: bytes, radio: int = 0) -> None:
        now = self.clock()
        if is_routed(plain):
            try:
                frame, relays = decode_routed(plain)
            except ValueError:
                self.stats["bad"] += 1
                return
            arm = self._arms[radio]
            here = self._model_for(arm.relay.radio_setting[1])
            sealed = getattr(arm.transport, "sealed_length", None)
            length = sealed(plain) if sealed is not None else len(plain) + SEAL_OVERHEAD
            quiet = now + here.repeat_gap(length) + here.slack
            arm.quiet_until = max(arm.quiet_until, quiet)
            if isinstance(frame, TraceReplyFrame):
                self._on_trace_reply(frame, now)
            elif isinstance(frame, ProbeReplyFrame):
                self._on_probe_reply(frame, relays, now)
            elif isinstance(frame, RouteErrorFrame):
                self._on_route_error(frame, now)
            elif isinstance(frame, DiscoverReplyFrame):
                self._on_discover_reply(frame, relays, now)
            elif isinstance(frame, ResultFrame):
                # The controller acknowledges it: that REPLY goes back the same way.
                self._result_routes[(frame.sender_id, frame.message_id)] = (reply_route(relays), radio)
                while len(self._result_routes) > 8:
                    self._result_routes.pop(next(iter(self._result_routes)))
                self._deliver(frame.encode())
            elif isinstance(frame, ReplyFrame):
                if arm.leave_id == (frame.sequence, frame.epoch):
                    arm.leaving_until = now  # the closed path is on its way home: follow at the next tick
                session = self.sessions.get(frame.sender_id)
                if session is not None and session.state == OPEN:
                    # Traffic proves the path: the next keepalive can wait.
                    self.book.note_path_traffic(session, now, self.keepalive_interval)
                    if session.radio and now - session.radio_since > self.radio_trial:
                        self._radio_strikes.pop(session.target, None)  # any answered exchange works there
                    if session.keepalive_id == (frame.sequence, frame.epoch):
                        self._test_result(session, now, answered=True)
                        asked = self.book.keepalive_answered(session, now)
                        if asked is not None:
                            self._switch_session(session, *asked)
                        self._release_waiting(session)
                        return
                if self._own_answer(frame):
                    return
                self._answered(frame, now, routed=True)
                self._deliver(frame.encode())
            return
        if len(plain) == 6 and plain[0] == FrameKind.REPLY:
            reply = ReplyFrame.from_bytes(plain)
            if self._own_answer(reply):
                return
            self._answered(reply, now, routed=False)
        self._deliver(plain)

    def _answered(self, reply: ReplyFrame, now: float, routed: bool) -> None:
        key = (reply.sender_id, reply.message_id)
        self._finish(key, True, "回答到达（经路由）" if routed else "回答到达（经泛洪）")
        self.book.release_inflight(key)
        self._forget(key)
        self._release(key, now)
        session = self.sessions.get(reply.sender_id)
        if session is not None and self.book.clear_fallback(session, key):
            if session.state == PROBING and session.probe_id is None:
                self._probe(session, now)

    def _on_probe_reply(self, frame: ProbeReplyFrame, relays: tuple[int, ...], now: float) -> None:
        session = self.sessions.get(frame.sender_id)
        self._release((frame.sender_id, frame.message_id), now)
        scan = self.scan_state
        if scan is not None and scan.probes.get((frame.sequence, frame.epoch)) == frame.sender_id:
            # A scan's routed ask: the path and links are new, what the node
            # last said of its state and neighbours still stands.
            node, old = frame.sender_id, self.nodes.get(frame.sender_id)
            state, neighbours = (old.state, old.neighbours) if old is not None else (None, ())
            self._found(node, reply_route(relays), frame.links, "scan", state, neighbours)
            scan.answered.setdefault(node, now)
        if session is None or session.state != PROBING:
            return
        if (frame.sequence, frame.epoch) not in session.probe_ids:
            return  # an answer to a probe from before the path last broke
        # Any probe of this round will do: a late answer to an earlier one
        # is still a path tested end to end (on 17 hops the answer can come
        # just after the gateway gave up waiting and probed again).
        self.book.open_from_probe(
            session, reply_route(relays), frame.links, now, now + self.keepalive_interval
        )
        self.probed[session.target] = ((self.controller_id, *session.route, session.target), frame.links, now)
        self._leave_radio(session)  # and picks this session's radio and SF
        latency = self.model.command(session.hops, self.expected_loss)
        self.book.note_latency(session, latency, latency.worst <= self.model.target)
        self._log("open", session.target)
        health = self._health(session.target)
        health.failed_probes = 0
        self._clear(session.target, "unreachable")
        checked = self.assess(session.target)
        if checked.passed:
            health.bad_paths = 0
            self._clear(session.target, "path")
        else:
            health.bad_paths += 1
            if health.bad_paths >= HEALTH_REPEATS:
                self._warn(session.target, "path", "路径连续重测不合格（" + "；".join(checked.reasons) + "）")
        self._release_waiting(session)  # a command held over a break (sleeping network)

    def _on_route_error(self, frame: RouteErrorFrame, now: float) -> None:
        for session in self.sessions.values():
            path = (self.controller_id, *session.route, session.target)
            if session.state != OPEN or frame.sender_id not in path[1:-1]:
                continue
            index = path.index(frame.sender_id)
            if path[index + 1] == frame.broken:
                hop = (frame.sender_id, frame.broken)
                self.book.note_route_error(session, hop)
                delivery = self.book.deliveries.get((session.target, frame.message_id))
                if delivery is not None:
                    delivery.broken_at, delivery.broken_route = hop, session.route
                    delivery.events.append((now, f"ROUTE_ERROR：{frame.sender_id} 到不了 {frame.broken}", frame.sender_id))
                self._broken(session, (frame.sequence, frame.epoch), now, "route_error", hop)
                return

    def _on_route_failed(self, plain: bytes, broken: int, radio: int = 0) -> None:
        """The gateway's own first hop went unacknowledged (``MeshRelay``) on ``radio``."""

        if plain[0] & 0x1F == FrameKind.REPLY:
            return  # a JOIN's acknowledgement: the node tries again, no session breaks
        session = self.sessions.get(plain[2])
        if session is not None and session.state == OPEN and session.radio != radio:
            # A frame from before the session moved (its answer was lost, the
            # session followed the path, ``switch_follows``): the old radio's
            # repeats were heard by nobody. Measured: it broke a good session.
            self.stats["stale_failures"] += 1
            return
        if session is not None and session.state == OPEN:
            hop = (self.controller_id, broken)
            self.book.note_first_hop_error(session, hop)
            delivery = self.book.deliveries.get((plain[2], plain[4] << 8 | plain[3]))
            if delivery is not None:
                delivery.broken_at, delivery.broken_route = hop, session.route
            self._broken(session, (plain[3], plain[4]), self.clock(), "first_hop", hop)

    # ---------------------------------------------------------------- helpers

    def _broken(
        self,
        session: Session,
        failed: Optional[tuple[int, int]],
        now: float,
        why: str,
        hop: Optional[tuple[int, int]] = None,
    ) -> None:
        self.book.begin_path_break(session, hop)
        if session.radio and now - session.radio_since <= self.radio_trial:
            self._strike(session, now)
        test = self._test_of(session)
        if test is not None:
            test.breaks += 1
            self._test_result(session, now, answered=False)  # a round on the broken path is lost
        # Re-probing happens on the common setting; the path's nodes come
        # back on their own (``MeshRelay.radio_timeout``).
        self._leave_radio(session)
        self._log(f"broken:{why}", session.target)
        if session.trace_id is not None:
            # The trace was on the path that broke: it ends here (diagnostics
            # only; the trace's own timeout runs only on an open session).
            self.book.interrupt_trace(session, now)
        self.book.reset_probe_round(session)  # their paths may run through what just broke
        inflight = self.book.take_inflight(session.target)
        waiting = self.book.take_waiting(session)
        if waiting is not None:
            inflight = waiting
        if inflight is not None and self._stale(inflight[0]):
            inflight = None  # answered or given up on already: nothing to carry over
        if inflight is None:
            self._probe(session, now)
            return
        if self.wake is not None and now >= self.awake_until:
            # A sleeping network hears no ordinary flood (task C5 measured
            # the 3 s it waited for one as most of a lost command): probe at
            # once; the command goes on the new path.
            delivery = self.book.deliveries.get(inflight[0])
            if delivery is not None:
                delivery.events.append((now, "网络在睡：不退回泛洪，等新路径", None))
            self._probe(session, now)
            self.book.hold_command(session, *inflight)
            return
        # The message on the broken path falls back to a flood now; the
        # re-probe waits for its answer so the command keeps the channel.
        delivery = self.book.deliveries.get(inflight[0])
        if delivery is not None:
            delivery.fell_back = True
            delivery.events.append((now, "退回泛洪", None))
        # A flood answers within one hold or likely not at all (measured on
        # sparse paths: 16 % at the first try): probe then.
        self.book.begin_fallback(session, inflight[0], now + self.flood_hold)
        self._submit_flood(inflight[0], inflight[1], front=True)

    def _wake_network(self, now: float, lease: Optional[int] = None) -> None:
        lease = self.wake_lease if lease is None else lease
        self._wake_sequence = (self._wake_sequence + 1) & 0xFF
        frame = WakeNetFrame(self.controller_id, self._wake_sequence, lease)
        self.awake_until = max(self.awake_until, now + lease)
        self.stats["wakes"] += 1
        self._log("wake", BROADCAST_ID)
        self._transport.send(frame.encode())  # not queued: every flood waits for it

    def _probe(self, session: Session, now: float) -> None:
        leaving = self._arms[0].leaving_until
        if leaving is not None:
            # Still on a closed session's setting: probe on the common one
            # after it, not with the wait taken out of the probe's timeout.
            self.book.defer_probe(session, leaving)
            return
        if self.wake == "net" and now >= self.awake_until - min(self.probe_timeout, self.wake_lease / 2):
            # Asleep, or about to be before the probe could come back.
            self._wake_network(now)
            self.book.probe_after_wake(session, now + self.wake_settle)
            return
        detour = None
        if self.detours and session.claim_detour():
            detour = self._detour(session)
        sequence, epoch = self._take_id(session.target)
        # A wake probe only while the network may be asleep: after a scan
        # woke everyone, an ordinary probe is faster and wakes nobody.
        waking = self.wake == "probe" and now >= self.awake_until - self.wake_settle
        timing = self._probe_timing(waking)
        timeout = self.probe_timeout
        if self.wake == "probe" and not waking:
            timeout = self.model.probe(MAX_RELAYS + 1, *timing)
        known = self.nodes.get(session.target)
        hops = known.last_hops if known is not None and known.last_hops else self.probe_hops
        timeout = min(timeout, self.model.probe(min(MAX_RELAYS + 1, hops + 2), *timing))
        if session.route:
            # Probing again: the new path is likely not much longer than the old.
            hops = min(MAX_RELAYS + 1, session.hops + 4)
            timeout = min(timeout, self.model.probe(hops, *timing))
        deadline = now + timeout
        window = max(1, min(0x7F, round(timing[0] / PROBE_WINDOW_UNIT)))
        if detour is not None:
            # One copy comes back along one path: no window to collect in.
            window = 1
            deadline = now + self.model.probe(len(detour) + 1, PROBE_WINDOW_UNIT, timing[1], timing[2])
        self.book.probing(session, (sequence, epoch), deadline, detour=detour is not None)
        if waking:
            window |= PROBE_WAKE
        frame = ProbeFrame(
            sender_id=self.controller_id,
            target_id=session.target,
            sequence=sequence,
            origin_id=self.controller_id,
            window=window,
            epoch=epoch,
        )
        if detour is not None:
            self._log("detour", session.target)
            self._emit(add_route(frame.encode(), detour))  # routed: nothing to queue behind
            return
        self._log("probe", session.target)
        self._submit_flood((session.target, epoch << 8 | sequence), frame.encode())

    def _detour(self, session: Session) -> Optional[tuple[int, ...]]:
        """The cheapest path to ``session.target`` round its suspect hop, from what the gateway knows (C5).

        Links come from the node book (each node's measured route and links,
        and the neighbours its scan answer named) and the session's own
        path; each costs the relays' ETX. The suspect relay is avoided, or,
        when the suspect is the target itself, its link. ``None``: no path.
        """

        target, (sender, receiver) = session.target, session.suspect
        longest = min(MAX_RELAYS + 1, len(session.route) + 1 + self.detour_extra_hops)
        avoid_node = receiver if receiver != target else None

        def usable(a: int, b: int) -> bool:
            if avoid_node is None:
                return {a, b} != {sender, receiver}
            return avoid_node not in (a, b)

        path = (self.controller_id, *session.route, target)
        relays = self.node_book._book_path(
            target, usable, longest, tuple(zip(zip(path, path[1:]), session.links)),
            cost=self.relay.path_etx, detour_margin=self.detour_margin,
        )
        return relays if relays else None

    def _model_for(self, sf: int) -> "LatencyModel":
        """The latency model at ``sf`` (the relays' rules, that SF's airtime)."""

        if sf == self.relay.base_sf:
            return self.model
        model = self._models.get(sf)
        if model is None:
            base = self.model
            model = self._models[sf] = LatencyModel(
                airtime_for(sf),
                jitter=base.jitter,
                slack=base.slack,
                answer=base.answer,
                turnaround=base.turnaround,
                target=base.target,
                retries=base.retries,
                app=base.app,
                last_hop_ack=base.last_hop_ack,
            )
        return model

    def _choose_sf(self, links: tuple[LinkQuality, ...]) -> int:
        base = self.relay.base_sf
        if not links:
            return base
        weakest = min(link.snr_db for link in links) - self.relay.probe_weak_snr  # margin at the base SF
        steps = math.ceil((self.sf_margin - weakest) / SF_STEP_DB)
        return max(base, min(self.max_sf, base + steps))

    def _call_back(self, session: Session, now: float) -> None:
        """Ask ``session`` back to the common setting (radio 0, base SF) with its next keepalive, at once."""

        base = self.relay.base_sf
        if session.radio_wanted and (session.asked is None or session.asked[0] != session.radio_wanted):
            self._release_arm(session, session.radio_wanted)
        self.book.return_to_common(session, base, now)

    def _recall(self, now: float) -> bool:
        """Call the session back to the common setting (a scan asks one by one); is it still away?"""

        away = False
        for session in self.sessions.values():
            if session.state != OPEN or not session.off_common(self.relay.base_sf):
                continue
            away = True
            if session.radio_wanted != 0 or session.sf_wanted not in (None, self.relay.base_sf):
                self._call_back(session, now)
        return away

    def _assign_radios(self, now: float) -> None:
        """Give the open session its radio and SF (Q11, Q12); none while a scan asks one by one."""

        scan = self.scan_state
        if scan is not None and not scan.done and scan.away:
            return  # everyone stays on the common setting meanwhile
        for session in self.sessions.values():
            idle = session.radio_wanted is None and session.sf_wanted is None and session.asked is None
            if session.state == OPEN and session.radio == 0 and idle:
                self._ask_radio(session, now)

    def _ask_radio(self, session: Session, now: float) -> None:
        """Pick the session's radio and SF; the next keepalive (at once) asks the path to move."""

        base = self.relay.base_sf
        sf = self._choose_sf(session.links) if self.session_sf else base
        free = next((k for k, arm in enumerate(self._arms) if k and arm.holder is None), None)
        if free is not None and now < self._radio_banned.get(session.target, float("-inf")):
            free = None  # it failed there twice in a row: the common channel's pace
        if free is not None:
            self._arms[free].holder = session.target
        self.book.request_radio_change(session, free, sf, now)

    def _strike(self, session: Session, now: float) -> None:
        """The path broke soon after moving onto the session radio."""

        self.book.note_radio_strike(session)
        history = self._radio_strikes.setdefault(session.target, [])
        history[:] = [at for at in history if now - at <= self.radio_strike_window]
        history.append(now)
        if len(history) < self.radio_strikes:
            return
        self._radio_strikes.pop(session.target, None)
        self._radio_banned[session.target] = now + self.radio_ban
        self.stats["radio_bans"] += 1
        self._log("radio_ban", session.target)

    def _release_arm(self, session: Session, radio: int) -> bool:
        if radio and self._arms[radio].holder == session.target:
            self._arms[radio].holder = None
            return True
        return False

    def _leave_radio(self, session: Session) -> None:
        """Back to radio 0 on the common setting (closed, broken, or about to probe again)."""

        base = self.relay.base_sf
        self._release_arm(session, session.radio)
        self._release_arm(session, session.radio_wanted or 0)
        self._release_arm(session, session.asked[0] if session.asked else 0)
        if session.radio == 0 and session.sf != base and self._arms[0].leaving_until is None:
            self.relay.set_radio(0, base)
        self.book.leave_radio(session, base)
        self._assign_radios(self.clock())

    def _probe_timing(self, waking: Optional[bool] = None) -> tuple[float, float, float]:
        """``(window, relay backoff, preamble lead)`` of this gateway's probes (wake probes by default)."""

        if self.wake != "probe" or waking is False:
            return self.probe_window, self.relay.probe_backoff[1], 0.0
        lead = self.relay.wake_lead
        window = self.probe_window + lead if self.wake_window is None else self.wake_window
        return window, self.relay.probe_backoff[1], lead

    def _send_command(self, session: Session, key: tuple[int, int], plain: bytes) -> None:
        now = self.clock()
        self.book.settle(session.target)  # an older delivery with this key is another message
        self.book.dispatch_command(session, key, plain, now, self.keepalive_interval)
        # The same message queued as a flood (its fallback, or a retry from
        # before the path opened) would follow as a second routed copy.
        for queued in [flood for flood in self._floods if flood.key == key]:
            self._floods.remove(queued)
        self.stats["routed"] += 1
        delivery = self.book.deliveries.get(key)
        if delivery is None:
            hops = session.hops
            model = self._model_for(session.sf)
            sizes = model.lengths(hops)
            costs = [model.hop_timeout(sizes["request"], left, True) for left in range(1, hops + 1)]
            costs += [model.hop_timeout(sizes["reply"], 0, False)] * (hops - 1)
            estimate = model.command(hops)
            delivery = self.book.record(
                key,
                Delivery(
                    session.target,
                    plain[3],
                    plain[4],
                    session.route,
                    now,
                    estimate.expected,
                    estimate.worst,
                    sum(costs) / len(costs),
                ),
            )
            delivery.events.append((now, "经路由发出", None))
        else:
            delivery.events.append((now, f"端到端重试（{'沿新路径' if delivery.route != session.route else '同一路径'}）", None))
            delivery.route = session.route
        self._emit(add_route(plain, session.route), session.radio)

    # ------------------------------------------------------------ diagnostics

    def _on_route_event(self, record, event: str) -> None:
        """``MeshRelay.route_event`` of the gateway's own relay (lock held there: record only)."""

        now = self.clock()
        if record.target == self.controller_id and event == "delivered":
            # A routed frame in from the first relay: a fresh look at our hop.
            session = self.sessions.get(record.sender)
            if session is not None and session.route:
                session.note_first_hop_rssi(record.ack_from, record.ack_rssi, now)
            return
        if record.sender != self.controller_id or record.index != 0:
            return
        session = self.sessions.get(record.target)
        if event in ("acked", "answered", "gave_up") and record.sends:
            self._note_attempts(record.target, record.sends, event != "gave_up")
        if session is not None and event in ("acked", "answered") and session.route:
            session.note_first_hop(record.ack_from, record.sends)
        delivery = self.book.deliveries.get((record.target, record.message_id))
        if delivery is None or session is None:
            return
        if event == "sent":
            delivery.first_hop_sends = record.sends  # this attempt's
            if record.sends > 1:
                delivery.events.append((now, f"第一跳重发（第 {record.sends - 1} 次）", self.controller_id))
        elif event in ("acked", "answered"):
            delivery.first_hop_ack = (record.ack_from, record.ack_rssi)
            session.note_first_hop_rssi(record.ack_from, record.ack_rssi, now)
            if record.repeats:
                delivery.events.append((now, f"第一跳已确认（重发 {record.repeats} 次后）", record.ack_from))
        elif event == "gave_up":
            delivery.events.append((now, "第一跳无确认，放弃", self.controller_id))

    def _finish(self, key: tuple[int, int], ok: bool, how: str) -> None:
        delivery = self.book.finish(key)
        if delivery is None:
            return
        now = self.clock()
        delivery.finished_at, delivery.ok = now, ok
        delivery.events.append((now, how, None))
        session = self.sessions.get(delivery.target)
        if session is not None:
            wanted = delivery.anomalous if self.trace_repeats else delivery.faulty
            self.book.finish_command(session, delivery, now, auto_trace=self.auto_trace and wanted)

    # ----------------------------------------------------------------- traces

    def trace(self, target: int, delivery: Optional[Delivery] = None) -> bool:
        """Ask every node on ``target``'s path about ``delivery`` (default: the last command).

        Sent as soon as the path is free; the answer lands in ``delivery.trace``.
        Returns ``False`` when there is no session or nothing to ask about.
        """

        session = self.sessions.get(target)
        if session is None or not session.deliveries:
            return False
        self.book.request_trace(session, delivery or session.deliveries[-1], bypass_interval=True)
        return True

    def _trace_ready(self, session: Session, now: float) -> bool:
        if self.book.cancel_trace_request_if_path_changed(session):
            # The command went another way (the path broke since): the nodes
            # on this path know nothing of it; ROUTE_ERROR said where it stuck.
            return False
        wanted = session.trace_wanted
        if wanted is None:
            return False
        queued = self.controller is not None and self.controller.pending_count(session.target) > 0
        free = (
            session.target not in self.book.inflight
            and session.waiting is None
            and not queued  # commands first: a trace only fills idle time
            and now >= session.last_finished_at + self.trace_delay
        )
        return free and now >= session.last_trace_at + self.trace_interval

    def _send_trace(self, session: Session, now: float) -> None:
        sequence, epoch = self._take_id(session.target)
        hops = session.hops
        out = 7 + hops + SEAL_OVERHEAD
        back = 5 + 4 * hops + hops + SEAL_OVERHEAD
        deadline = now + self._model_for(session.sf).exchange(hops, out, back).worst + self.margin
        delivery = self.book.begin_trace(
            session, (sequence, epoch), deadline, now, self.keepalive_interval
        )
        self.stats["traces"] += 1
        delivery.events.append((now, "发出追踪", None))
        frame = TraceFrame(
            self.controller_id, session.target, sequence, delivery.sequence, delivery.epoch, epoch
        )
        self._emit(add_route(frame.encode(), session.route), session.radio)

    def _on_trace_reply(self, frame: TraceReplyFrame, now: float) -> None:
        session = next(
            (s for s in self.sessions.values() if s.trace_id == (frame.sequence, frame.epoch)), None
        )
        if session is None or session.trace_of is None:
            return  # a late or repeated answer
        delivery = session.trace_of
        # Records come target first, then each relay on the way back.
        nodes = (session.target, *reversed(session.route))
        records = dict(zip(nodes, frame.records))
        records[self.controller_id] = node_report(
            self._arms[session.radio].relay.route_log, self.controller_id, session.target, delivery.sequence, delivery.epoch
        )
        result = TraceResult(session.route, records, now)
        delivery.events.append((now, f"追踪结果到达（{len(frame.records)}/{len(nodes)} 个节点）", None))
        self.book.finish_trace(session, result, self.keepalive_interval)
        self._release_waiting(session)

    def _switch_session(self, session: Session, radio: int, sf: int) -> None:
        """The path has answered on the old setting and moves: so does the gateway (Q11, Q12)."""

        if radio != session.radio:
            self._release_arm(session, session.radio)
        self.book.switch_radio(session, radio, sf, self.clock())
        arm = self._arms[radio]
        arm.relay.set_radio(arm.channel, sf)
        latency = self._model_for(sf).command(session.hops, self.expected_loss)
        self.book.note_latency(session, latency, latency.worst <= self.model.target)
        self._log(f"radio:{radio}/sf:{sf}", session.target)

    def _release_waiting(self, session: Session) -> None:
        waiting = self.book.take_waiting(session) if session.state == OPEN else None
        if waiting is not None:
            key, plain = waiting
            if not self._stale(key):
                self._send_command(session, key, plain)

    def _forget(self, key: tuple[int, int]) -> None:
        """Drop every copy of ``key`` the gateway still holds: the command is over."""

        self.book.forget_waiting(key)
        for queued in [flood for flood in self._floods if flood.key == key]:
            self._floods.remove(queued)

    def _stale(self, key: tuple[int, int]) -> bool:
        """A held-back REQUEST/WAKE the controller no longer waits on (answered, given up, queried).

        The last guard before the gateway sends a copy of its own: one sent
        after the device's dedup entry is gone would run the command twice.
        """

        return self.controller is not None and not self.controller.awaiting(*key)

    def _stale_copy(self, data: bytes) -> bool:
        plain = split_route(data)[0] if is_routed(data) else data
        return (
            len(plain) >= 5
            and plain[0] in (FrameKind.REQUEST, FrameKind.WAKE)
            and self._stale((plain[2], _message_id(plain)))
        )

    # -------------------------------------------------------------- discovery

    # ------------------------------------------------------- saved node book

    def save_nodes(self, path: str) -> None:
        self.node_book.save_nodes(path)

    def load_nodes(self, path: str) -> int:
        return self.node_book.load_nodes(path, self.clock())

    def scan(self, expected_nodes: Optional[int] = None) -> Scan:
        """Ask every node to answer (task C); what comes back builds up ``nodes``.

        The answer window is sized for ``max(known nodes, expected_nodes)``.
        Afterwards each known node that stayed silent is asked on its own
        (``scan_followups`` at most). The scan holds the flood queue while
        answers come in. One already running is returned as it is.
        """

        now = self.clock()
        if self.scan_state is not None and not self.scan_state.done:
            return self.scan_state
        self.book.yield_radio(self.relay.base_sf)
        scan = Scan(now, now, 0.0, 0.0, frozenset(self.nodes))
        self.scan_state = scan
        expected = max(expected_nodes or 0, self.expected_nodes)
        unknown = max(0, expected - len(self.nodes))
        if self.wake is not None and now >= self.awake_until - self.scan_wake_lease / 2:
            self._wake_network(now, self.scan_wake_lease)
            scan.starts_at, scan.unknown = now + self.scan_wake_settle, unknown
            scan.ends = scan.starts_at
            return scan
        self._full_round(scan, now, unknown, first=True)
        return scan

    def _full_round(self, scan: Scan, now: float, unknown: int, first: bool = False) -> None:
        """A DISCOVER to everyone, its window sized for the known nodes plus ``unknown`` more."""

        plan = round_plan(
            [node.hops for node in self.nodes.values()],
            unknown,
            self.model,
            self.probe_window,
            self.relay.probe_backoff[1],
            self.scan_spread_factor,
            self.margin,
        )
        if not first:
            scan.rounds += 1
        scan.spread, scan.ends = plan.spread, now + plan.expected + self.margin
        scan.expected += plan.expected
        # The flood queue waits only while the scan spreads (measured: held
        # for the whole answer window, a broken session's re-probes queued
        # behind it for a minute and its commands failed).
        self._send_discover(scan, BROADCAST_ID, plan.units, plan.spreading)

    def _send_discover(self, scan: Scan, target: int, spread: int, hold: float) -> None:
        # Scans do not draw on the controller's per-node ids (that refuses
        # broadcast); relays key them apart from probes by their kind.
        self._scan_sequence = (self._scan_sequence + 1) & 0xFF
        epoch = self.controller.epoch if self.controller is not None else 0
        sequence = self._scan_sequence
        scan.ids.add((sequence, epoch))
        frame = DiscoverFrame(
            sender_id=self.controller_id,
            target_id=target,
            sequence=sequence,
            origin_id=self.controller_id,
            window=max(1, min(0xFF, round(self.probe_window / PROBE_WINDOW_UNIT))),
            spread=spread,
            epoch=epoch,
        )
        # A key no command can have (their message ids are not negative).
        self._scan_key = (target, -1 - (epoch << 8 | sequence))
        self._submit_flood(self._scan_key, frame.encode(), hold=hold)

    def _scan_tick(self, now: float) -> None:
        scan = self.scan_state
        if start_due(scan, self.scan_at_start, self.controller is not None):
            self.scan()
            return
        if rescan_due(
            scan,
            len(self.nodes),
            now,
            bool(self.sessions),
            self.expected_nodes,
            self.rescan_after,
            self._rescans_done,
            self.rescans,
        ):
            self._rescans_done += 1
            self.scan()
            return
        # A scan's answers crowd the gateway's own hop for about a minute
        # (measured on 60 nodes: a session's commands got through 66 %
        # of the time, 3× slower): the periodic one waits for no session.
        if interval_due(scan, now, bool(self.sessions), self.scan_interval):
            self.scan()
        if scan is not None and scan.done:
            return
        if scan is None or scan.done:
            return
        if scan.starts_at is not None:
            if now < scan.starts_at:
                return
            scan.starts_at = None
            self._full_round(scan, now, scan.unknown, first=True)
            return
        if self.wake is not None and now >= self.awake_until - self.scan_wake_settle:
            self._wake_network(now, self.scan_wake_lease)  # still scanning: keep everyone up
        flood = not self.scan_routed
        if scan.asking is None and not scan.followups and not scan.closing:
            if now < scan.ends:
                return
            # Silent nodes worth asking: those known before, and those another
            # answer names as its neighbour (they exist: their answer was lost
            # on the way, measured as the usual case).
            away = scan.away or frozenset(self.book.away_nodes())
            missing = missing_nodes(scan, away)
            if len(missing) > self.scan_followups and scan.rounds < self.scan_rounds:
                # Too many lost to ask one by one: the window was too small for
                # the network (a first scan cannot know its size). Everyone
                # again, in a window sized from what this round found.
                self._full_round(scan, now, len(missing))
                return
            if missing and away:
                # Nodes behind a session's path cannot hear the common channel:
                # the sessions come back while silent nodes are asked one by one.
                if scan.recall_until is None:
                    scan.recall_until, scan.away = now + self.recall_wait, away
                if self._recall(now) and now < scan.recall_until:
                    return
            scan.closing = True
            scan.followups.extend(missing[: self.scan_followups])
        if scan.asking is not None:
            if scan.asking not in scan.answered and now < scan.asking_until:
                return
            node, routed = scan.asking, scan.asking_routed
            scan.asking, scan.asking_routed = None, False
            if routed and node not in scan.answered:
                # The known path did not carry it: everyone carries the question.
                scan.followups.appendleft(node)
                flood = True
        if scan.followups:
            node = scan.followups.popleft()
            if not flood and self._ask_routed(scan, node, now):
                return
            known = self.nodes.get(node)
            hops = min(MAX_RELAYS + 1, known.hops + 4) if known is not None else MAX_RELAYS + 1
            wait = self.model.scan(hops, self.probe_window, self.relay.probe_backoff[1], 0.0) + self.margin
            scan.asking, scan.asking_until = node, now + wait
            self._send_discover(scan, node, 0, wait)
            return
        scan.finished_at = now
        from .events import ScanFinished

        nudge_all = getattr(self.controller, "nudge_all", None)
        if nudge_all is not None:
            nudge_all(fresh=False)  # rounds during a scan rarely get through: ask again now
        self._assign_radios(now)
        found = sorted(scan.answered)
        silent, away = (scan.known | scan.mentioned) - scan.answered.keys(), self.book.away_nodes() | scan.away
        self._publish(
            ScanFinished(
                found=tuple(found),
                missing=tuple(sorted(silent - away)),
                in_session=tuple(sorted(silent & away)),
                new=tuple(node for node in found if node not in scan.known),
                duration=now - scan.started,
                expected=scan.expected,
            )
        )

    def _ask_routed(self, scan: Scan, node: int, now: float) -> bool:
        """Ask silent ``node`` with a PROBE along the cheapest known path; ``False``: no path known.

        A routed PROBE costs one send per hop where a DISCOVER to one node
        is carried by every node (about N sends). The node book's path to a
        node that went silent is the one most likely broken, so it is priced
        like a detour's (``detour_margin``) and another may win; a node only
        named as a neighbour is reached through the node that named it.
        """

        if self.controller is None:
            return False
        away = scan.away
        relays = self.node_book._book_path(
            node, lambda a, b: a not in away and b not in away, MAX_RELAYS + 1,
            cost=self.relay.path_etx, detour_margin=self.detour_margin,
        )
        if relays is None:
            return False
        sequence, epoch = self._take_id(node)
        scan.probes[(sequence, epoch)] = node
        scan.routed += 1
        _, backoff, _ = self._probe_timing(False)
        wait = self.model.probe(len(relays) + 1, PROBE_WINDOW_UNIT, backoff, 0.0) + self.margin
        scan.asking, scan.asking_until, scan.asking_routed = node, now + wait, True
        frame = ProbeFrame(
            sender_id=self.controller_id,
            target_id=node,
            sequence=sequence,
            origin_id=self.controller_id,
            window=1,  # one copy comes back along one path
            epoch=epoch,
        )
        self._log("scan_probe", node)
        self._emit(add_route(frame.encode(), relays))
        return True

    def _on_path(self, frame: ProbeFrame, links: tuple[LinkQuality, ...]) -> None:
        """``MeshRelay.path_found`` of the gateway's relay: a JOIN, best path chosen."""

        if not isinstance(frame, JoinFrame):
            return
        node = frame.origin_id
        route = reply_route(hop.node_id for hop in frame.hops)
        # The JOIN measured its links from the node inward; ours run outward.
        self._found(node, route, tuple(reversed(links)), "join")
        reply = ReplyFrame(
            sender_id=self.controller_id,
            target_id=node,
            sequence=frame.sequence,
            result=ReplyResult.OK,
            epoch=frame.epoch,
        )
        self._emit(add_route(reply.encode(), route))
        if self.join_storm is not None:
            now = self.clock()
            self._join_storm.threshold = self.join_storm
            self._join_storm.window = self.join_storm_window
            triggered = self._join_storm.note(now)
            running = self.scan_state is not None and not self.scan_state.done
            if triggered and not running:
                self._join_storm.clear()
                self.scan(expected_nodes=len(self.nodes))

    def _on_discover_reply(self, frame: DiscoverReplyFrame, relays: tuple[int, ...], now: float) -> None:
        node = frame.sender_id
        self._found(node, reply_route(relays), frame.links, "scan", frame.state, frame.neighbours)
        scan = self.scan_state
        if scan is not None and (frame.sequence, frame.epoch) in scan.ids:
            scan.note_answer(
                node,
                now,
                (
                    hop.node_id
                    for hop in frame.neighbours
                    if hop.node_id not in (self.controller_id, BROADCAST_ID)
                ),
            )
            if scan.asking == node:
                self._release(self._scan_key, now)

    def _found(
        self,
        node: int,
        route: tuple[int, ...],
        links: tuple[LinkQuality, ...],
        how: str,
        state: Optional[int] = None,
        neighbours: tuple[ProbeHop, ...] = (),
    ) -> None:
        from .events import NodeFound

        new = self.node_book.found(node, route, links, how, self.clock(), state, neighbours)
        if new is None:
            # Never a node: one taken in was asked by id (a scan drew a
            # message id for 0xFF and ``allocate_id`` refused it in the main loop).
            self.stats["bad"] += 1
            return
        known = self.nodes[node]
        self._publish(
            NodeFound(
                node_id=node,
                how=how,
                route=known.route,
                links=tuple((link.rssi, link.snr_db) for link in known.links),
                state=known.state,
                neighbours=tuple((hop.node_id, hop.link.rssi) for hop in known.neighbours),
                new=new,
            )
        )

    def _publish(self, event) -> None:
        if self.controller is not None:
            self.controller.events.publish(event)

    def weak_links(self, threshold: Optional[float] = None) -> list[WeakLink]:
        return self.node_book.weak_links(
            self.sf_margin if threshold is None else threshold, self.relay.probe_weak_snr
        )

    def test_link(self, target: int, rounds: int = 20, gap: float = 0.5) -> LinkTest:
        """Start a link test to ``target``: ``rounds`` keepalive round trips on its session.

        Opens the session if needed (one at a time: another one closes).
        Commands keep priority: a round waits while one is in flight. The
        result is in ``link_tests[target]`` and, once over, a
        ``LinkTestFinished`` event.
        """

        if rounds < 1:
            raise ValueError("a link test needs at least one round")
        now = self.clock()
        self.open_session(target)
        test = LinkTest(target, rounds, now, gap, self.sf_margin, base_sf=self.relay.base_sf, progress_at=now)
        self.link_tests[target] = test
        self.stats["link_tests"] += 1
        return test

    def _test_of(self, session: Session) -> Optional[LinkTest]:
        return self.link_tests_book._test_of(session)

    def _test_due(self, session: Session, now: float) -> bool:
        return self.link_tests_book._test_due(session, now)

    def _test_round(self, session: Session, now: float) -> None:
        test = self.link_tests_book._test_of(session)
        if test is None or test.pending is not None:
            return
        expected = self._model_for(session.sf).keepalive(session.hops).expected
        started = self.link_tests_book._test_round(
            session, now, self.controller_id, self.relay.probe_weak_snr, expected, self.margin,
        )
        if started:
            session.last_used = now  # a test keeps its session from idling out

    def _test_result(self, session: Session, now: float, answered: bool) -> None:
        finished = self.link_tests_book._test_result(session, now, answered)
        if finished is not None:
            self._finish_link_test_acceptance(*finished)

    def _test_tick(self, now: float) -> None:
        for finished in self.link_tests_book._test_tick(self.sessions, now):
            self._finish_link_test_acceptance(*finished)

    def _finish_link_test_acceptance(self, test: LinkTest, accepting: bool) -> None:
        if accepting:
            from .events import AcceptanceFinished

            acceptance = self.assess(test.target, test)
            self.acceptance[test.target] = acceptance
            self._publish(AcceptanceFinished(node_id=test.target, acceptance=acceptance))

    # ---------------------------------------------------------------- health

    def _health(self, target: int) -> Health:
        return self.health_book._health(target)

    def _warn(self, target: int, cause: str, text: str) -> None:
        self.health_book._warn(target, cause, text)

    def _clear(self, target: int, cause: str) -> None:
        self.health_book._clear(target, cause)

    def _note_attempts(self, target: int, sends: int, through: bool) -> None:
        self.health_book._note_attempts(target, sends, through)

    # ------------------------------------------------------------ acceptance

    def accept(self, target: int, rounds: int = 20) -> LinkTest:
        """Deployment acceptance of ``target``: a link test, then ``assess`` on the path it ran over.

        The verdict lands in ``acceptance[target]`` and comes as an
        ``AcceptanceFinished`` event. Scan first (``scan``): the detours come
        from the node book, and without it only the probed links are known.
        """

        test = self.test_link(target, rounds)
        self._accepting[target] = test
        return test

    def assess(self, target: int, test: Optional[LinkTest] = None) -> Acceptance:
        """Judge ``target``'s open path by the acceptance rule (``ACCEPT_*``); ``test``, if given, must be good."""

        now = self.clock()
        session = self.sessions.get(target)
        if session is None or session.state != OPEN or len(session.links) != len(session.route) + 1:
            return _assess_linkqa(
                PathView(
                    target,
                    None if session is None else session.state,
                    () if session is None else session.route,
                    () if session is None else session.links,
                ),
                now=now,
                controller_id=self.controller_id,
                floor=0.0,
                link_map={},
                has_neighbour_info=False,
                test=test,
            )
        floor = self.relay.probe_weak_snr
        return _assess_linkqa(
            PathView(target, session.state, session.route, session.links),
            now=now,
            controller_id=self.controller_id,
            floor=floor,
            link_map=self.node_book._link_map(floor),
            has_neighbour_info=any(known_node.neighbours for known_node in self.nodes.values()),
            test=test,
        )

    def deliveries(self, target: int) -> list[Delivery]:
        """Recent commands to ``target`` on its session, oldest first."""

        session = self.sessions.get(target)
        return [] if session is None else list(session.deliveries)

    def _keepalive(self, session: Session, now: float) -> None:
        sequence, epoch = self._take_id(session.target)
        model = self._model_for(session.sf)
        deadline = now + model.keepalive(session.hops).worst + self.margin
        self.book.begin_keepalive(session, (sequence, epoch), deadline)
        radio, sf = self.book.keepalive_target(session)
        frame = KeepaliveFrame(
            self.controller_id,
            session.target,
            sequence,
            lease=self.lease,
            epoch=epoch,
            radio=radio_byte(self._arms[radio].channel, sf),
        )
        self._emit(add_route(frame.encode(), session.route), session.radio)
        self._test_round(session, now)

    def _emit(self, data: bytes, radio: int = 0) -> None:
        arm = self._arms[radio]
        if arm.held or self.clock() < arm.quiet_until or arm.leaving_until is not None:
            arm.held.append(data)
        else:
            arm.transport.send(data)

    def _take_id(self, target: int) -> tuple[int, int]:
        if self.controller is None:
            raise RuntimeError("attach(controller) first: probes and keepalives need message ids")
        sequence, epoch = self.controller.allocate_id(target)
        own = self._own_ids.setdefault(target, OrderedDict())
        own[(sequence, epoch)] = None
        while len(own) > OWN_IDS_KEPT:
            own.popitem(last=False)
        return sequence, epoch

    def _own_answer(self, reply: ReplyFrame) -> bool:
        """An answer to a frame the gateway sent itself: never the controller's (review A1)."""

        if (reply.sequence, reply.epoch) in self._own_ids.get(reply.sender_id, ()):
            self.stats["own_answers"] += 1
            return True
        return False

    def _submit_flood(
        self, key: tuple[int, int], plain: bytes, front: bool = False, hold: Optional[float] = None
    ) -> None:
        now = self.clock()
        self.stats["flooded"] += 1
        if not self.serialize_floods:
            self._emit(plain)
            return
        active = self._flood
        if active is not None and active.key == key:
            if now < active.until:
                self.stats["flood_retries_swallowed"] += 1
                return
            active.plain, active.until = plain, now + self.flood_hold
            self._emit(plain)
            return
        for queued in self._floods:
            if queued.key == key:
                queued.plain = plain
                return
        flood = _Flood(key, plain, hold=hold)
        if front:
            self._floods.appendleft(flood)
        else:
            self._floods.append(flood)
        self._pump(now)

    def _pump(self, now: float) -> None:
        while self._flood is None and self._floods:
            if now < self._settled_at:
                return  # the last flood's tail is still out there (``tick`` comes back)
            flood = self._floods.popleft()
            if self._stale_copy(flood.plain):
                continue
            target = flood.key[0]
            session = self.sessions.get(target)
            if session is not None and session.state == OPEN and flood.plain[0] in (FrameKind.REQUEST, FrameKind.WAKE):
                # The path came back while this waited: no flood needed.
                if session.keepalive_id is not None:
                    self.book.hold_command(session, flood.key, flood.plain)
                else:
                    self._send_command(session, flood.key, flood.plain)
                continue
            self._flood = flood
            flood.until = now + (self.flood_hold if flood.hold is None else flood.hold)
            self._emit(flood.plain)

    def _release(self, key: tuple[int, int], now: float) -> None:
        if self._flood is not None and self._flood.key == key:
            self._flood = None
            # Measured: a re-probe sent the moment the fallback's reply came
            # in died at its first ring, on that reply's last copies.
            self._settled_at = now + self.flood_settle
            self._pump(now)

    def _deliver(self, plain: bytes) -> None:
        if self._receiver is not None:
            self._receiver(plain)

    def _log(self, event: str, node: int) -> None:
        self.history.append((self.clock(), event, node))
        excess = len(self.history) - self.max_history
        if excess >= self.max_history // 10:  # trim in batches, not on every event
            del self.history[:excess]
            self.history_base += excess
