"""Managed flooding and routed sessions: multi-hop over a radio whose topology nobody knows.

Nodes sit wherever they were deployed and no path can be assumed, so every
node may relay what it hears. ``MeshRelay`` sits between ``SecureTransport``
and the radio and keeps the flood bounded without a hop limit:

* **Each transmission is relayed at most once per node.** A transmission is
  named by ``(origin, FCNT)`` from the sealed header: FCNT changes on every
  send, end-to-end retries included, and no relay touches it. Each node keeps
  one ``ReplayWindow`` per origin, so an old copy is never relayed again, no
  matter how long it circulates or how busy the network is (a FIFO cache
  would forget it under load). The windows live in RAM (``reboot``): only
  the controller's is seeded again after a power cut, from the node's
  persisted replay mark. A flood thus costs at most one transmission
  per node, plus the originator's own repeats. A relay changes nothing but
  the authenticated TX byte (``secure.reseal``): SENDER, FCNT and the plain
  frame pass on untouched, so every duplicate key survives the hop.
* **Relays that add nothing stay quiet.** A relay waits a random backoff
  first. A strong signal (a close sender) means a longer wait, since a
  distant receiver covers more new ground. For frames spreading out from
  the controller (REQUEST, WAKE), hearing ``suppress_after`` other copies
  during the wait cancels the relay: the neighbourhood is covered. Frames
  heading for the controller (REPLY, REPORT, RESULT, EVENT, HEARTBEAT) are never
  suppressed: copies from further out say nothing about the controller's
  side.
* **HELLO is one hop.** It is never relayed and never waits for an
  acknowledgement (``liveness.py``).
* **WAKE_NET reaches dormant radios** (task D2, ``wake.py``). It floods like
  a REQUEST, but every copy, the originator's and each relay's, goes out
  with a preamble of ``wake_preamble`` symbols, long enough for a radio that
  only samples the channel to notice it, and listens before it talks (long
  preambles from neighbours would otherwise overlap). The originator
  repeats it (``wake_retries``) until it hears a relay pass it on: next to
  a dormant network nobody may have heard it. ``wake_gate`` may refuse to
  relay one (a rate limit against forged wake-ups). SLEEP_NET is an
  ordinary outward flood.
* **The reply confirms delivery to the previous hop and to the controller.**
  The originator takes any rebroadcast of its frame, or the reply to its
  request, as an implicit acknowledgement, and repeats the frame verbatim
  (``hop_retries``) only when it hears neither. A reply is relayed only by
  nodes that relayed its request (``guided_replies``), so it returns along
  the backbone the request built instead of flooding everywhere, and it
  cancels relays of that request that are still waiting. Because that
  backbone is thin, every relay of a reply also waits for an implicit
  acknowledgement and repeats it once (``confirm_replies``).

Routed sessions (``protocol_spec.md`` section 11) ride on the same relays:

* **PROBE** floods from the gateway. Each relay appends itself and the link
  it heard, and seals the result as a frame of its own through ``sealer``
  (the node's ``SecureTransport.seal_frame``: one persisted counter, never a
  second one). A relay forwards each probe once, keyed by (ORIGIN, message
  id), after a wait that is *shorter* the better the path so far, and keeps
  only the best copy heard meanwhile. The target collects copies for the
  probe's WINDOW after the first and answers the best path (fewest expected
  sends, ``probe_metric``) with a routed PROBE_REPLY.
* **Routed frames** (KIND has ``ROUTED``) carry the relay list. Only listed
  relays forward, at once, and only copies from a node before them in the
  list (``L`` = SENDER, R1 .. RN, TARGET). TX tells the copies apart:
  hearing the frame from a node after us acknowledges our hop; hearing it
  from before us again means that node missed our forward, and an
  acknowledged relay answers with the copy of the furthest node it heard
  (TX = that node), which nobody further on takes for a retry. A REPLY
  acknowledges every hop of its request. Unacknowledged hops are repeated
  after about three airtimes, or near the target once its answer is overdue
  (``route_hop_timeout``), at most ``route_retries`` times; then a relay reports
  ROUTE_ERROR back along the route, and the originator is told through
  ``route_failed``. The gateway echoes the last hop of a REPLY, PROBE_REPLY,
  ROUTE_ERROR or RESULT (``last_hop_ack``), which acknowledges it like any
  hop; a TRACE_REPLY or DISCOVER_REPLY into the gateway is sent once more
  after a short gap instead.
  Relays keep no route state.
* **A session's setting** (tasks Q11, Q12): a KEEPALIVE's RADIO byte names
  the channel and SF the path moves to once the node has passed that
  keepalive's answer on; ``radio_timeout`` of silence there, or going
  dormant, brings it back to the common setting.
* **End-to-end layer** (task Q14): REQUEST, REPORT, RESULT, EVENT and the
  REPLY to a REQUEST or RESULT carry an inner layer under the node's application key (KIND has ``APP``).
  Relays read only the clear head (KIND, SENDER, TARGET, SEQ, EPOCH) and the
  route trailer; compare KINDs through ``_kind`` (it strips ROUTED and APP),
  never ``plain[0] ==``.

The firmware-facing summary of these rules is ``protocol_spec.md``.

The relay key must be the per-transmission FCNT, never the message id
(EPOCH | SEQUENCE): an end-to-end retry keeps its message id, and keying on
it would stop every retry at the first relay that saw the original.

Only sealed frames are relayed (the protocol is encrypted-only; plaintext is
an engineering fallback for single-hop links). The MIC is checked before
anything is recorded, so a forged frame cannot push an origin's window ahead
and silence it. Timing (``backoff``, ``hop_timeout``, the route timeouts)
depends on airtime and belongs next to the radio; the defaults suit the
simulator only.
"""

from __future__ import annotations

import math
import random
import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass
from typing import Callable, Optional, Protocol

from .airtime import Airtime, airtime_for
from .energy import symbol_time
from .frames import (
    BROADCAST_ID,
    APP,
    ROUTED,
    TRACE_DONE,
    TRACE_GAVE_UP,
    TRACE_OTHER,
    TRACE_UNSEEN,
    PATH_KINDS,
    PROBE_WAKE,
    DiscoverFrame,
    FrameKind,
    JoinFrame,
    LinkQuality,
    ProbeFrame,
    ProbeReplyFrame,
    RouteErrorFrame,
    TraceFrame,
    TraceRecord,
    TraceReplyFrame,
    add_route,
    decode_routed,
    reply_route,
    radio_of,
    split_route,
)
from .secure import KEY_LEN, MIC_LEN, SECURE, ReplayWindow, open_sealed, seal, transmitter
from .transport import Receiver

#: Called once per frame heard: ``(frame, RSSI in dBm or None)``.
RadioReceiver = Callable[[bytes, Optional[float]], None]

#: ``(asker, answerer, sequence, epoch)``: the same for a request (or report) and its reply.
RequestId = tuple[int, int, int, int]

#: Called with every authenticated first copy, whoever it is for: ``(plain frame, RSSI)``.
Observer = Callable[[bytes, Optional[float]], None]

#: Seals a plain frame under this node's own persisted FCNT (``SecureTransport.seal_frame``).
Sealer = Callable[[bytes], bytes]

#: A routed frame this node originated found no next hop: ``(plain frame, next hop)``.
RouteFailed = Callable[[bytes, int], None]

#: A JOIN or DISCOVER this node collected for its window: ``(frame, links)``,
#: the best path heard and one link per hop of it from the origin's side.
PathFound = Callable[[ProbeFrame, "tuple[LinkQuality, ...]"], None]


class RadioPort(Protocol):
    """A broadcast radio: what one node sends, every node in range may hear.

    A radio may also offer ``busy_until() -> float`` (channel activity
    detection: until when it is receiving or sending); every frame then
    listens before it talks.
    """

    def send(self, data: bytes) -> None: ...

    # A radio that can reach dormant radios takes ``send(data, preamble=symbols)``.

    def set_receiver(self, callback: RadioReceiver) -> None: ...


#: Frames answered by a REPLY (a TRACE by its TRACE_REPLY): (sender, target, sequence, epoch) names them.
_ANSWERED = (
    FrameKind.REQUEST,
    FrameKind.WAKE,
    FrameKind.REPORT,
    FrameKind.KEEPALIVE,
    FrameKind.TRACE,
    FrameKind.RESULT,
    FrameKind.EVENT,
)
#: Routed kinds heading for the gateway, which never relays: their last hop
#: cannot be acknowledged.
_INBOUND = (
    FrameKind.REPLY,
    FrameKind.PROBE_REPLY,
    FrameKind.ROUTE_ERROR,
    FrameKind.TRACE_REPLY,
    FrameKind.DISCOVER_REPLY,
    FrameKind.RESULT,
)
#: Inbound kinds the gateway echoes (``MeshRelay.last_hop_ack``), so their last
#: hop is acknowledged like any other. Still sent twice, blind: a TRACE_REPLY
#: (diagnostics) and a DISCOVER_REPLY (a scan's answers converge on the
#: gateway, which hears none of them while it echoes; measured: a ladder
#: scan lost an answer; the scan asks silent nodes again anyway).
_ECHOED = (FrameKind.REPLY, FrameKind.PROBE_REPLY, FrameKind.ROUTE_ERROR, FrameKind.RESULT)
#: How each flooded path frame decodes.
_PATH_FRAMES = {FrameKind.PROBE: ProbeFrame, FrameKind.JOIN: JoinFrame, FrameKind.DISCOVER: DiscoverFrame}
#: Floods spreading out from the controller: covered neighbourhoods suppress them.
_SPREADING = (FrameKind.REQUEST, FrameKind.WAKE, FrameKind.WAKE_NET, FrameKind.SLEEP_NET)
#: Kinds a gateway sends out on a route.
_OUTBOUND = (FrameKind.REQUEST, FrameKind.WAKE, FrameKind.KEEPALIVE)
#: Bytes one node adds to a TRACE_REPLY: its record, less its own place in the route.
_TRACE_GROWTH = 4 - 1


def _trace_state(record: "Optional[HopRecord]") -> int:
    if record is None:
        return TRACE_UNSEEN
    if record.state in ("acked", "answered", "delivered", "sent"):
        return TRACE_DONE
    if record.state == "gave_up":
        return TRACE_GAVE_UP
    return TRACE_OTHER


def _dbm(value: Optional[float]) -> Optional[int]:
    return None if value is None else min(-1, max(-255, round(value)))


def node_report(
    log: "dict[tuple[int, int, int, int, int], HopRecord]",
    gateway: int,
    target: int,
    sequence: int,
    epoch: int,
) -> TraceRecord:
    """This node's ``TraceRecord`` for one message, from its ``route_log``.

    The one place a route log becomes a trace record: relays use it to answer
    a TRACE, and ``pathview.records_view`` uses it on the simulator's ground
    truth, so both views can only differ by what was lost on the air.
    """

    out = next(
        (
            record
            for kind in _OUTBOUND
            if (record := log.get((kind, gateway, target, sequence, epoch))) is not None
        ),
        None,
    )
    back = log.get((FrameKind.REPLY, target, gateway, sequence, epoch))
    snr = None
    if out is not None and out.prev_snr is not None:
        snr = min(127, max(-127, round(out.prev_snr * 4)))
    return TraceRecord(
        rssi_out=_dbm(out.prev_rssi) if out is not None else None,
        snr_out=snr,
        rssi_back=_dbm(back.prev_rssi) if back is not None else None,
        out_repeats=min(3, out.repeats) if out is not None else 0,
        out_state=_trace_state(out),
        back_repeats=min(3, back.repeats) if back is not None else 0,
        back_state=_trace_state(back),
    )


def _kind(plain: bytes) -> int:
    """KIND without the ROUTED and APP flags (a relay treats end-to-end sealed frames alike, task Q14)."""

    return plain[0] & ~(ROUTED | APP) & 0xFF


def _is_wake_preamble(preamble: int) -> bool:
    """Whether ``preamble`` is the long WAKE preamble rather than the standard 8 symbols."""

    return preamble != 8


def _probe_key(frame: ProbeFrame) -> tuple[int, ...]:
    # (ORIGIN, message id) names a probe; a JOIN or DISCOVER also by its
    # kind, since the gateway's scans do not draw on its probes' id space.
    if frame.kind == FrameKind.PROBE:
        return frame.origin_id, frame.message_id
    return frame.origin_id, frame.message_id, frame.kind


def _is_wake_probe(plain: bytes) -> bool:
    """A wake probe, or (Q22) a wake JOIN: both go on with the long preamble."""

    return _kind(plain) in (FrameKind.PROBE, FrameKind.JOIN) and len(plain) > 6 and bool(plain[6] & PROBE_WAKE)


def _request_id(plain: bytes) -> Optional[RequestId]:
    if len(plain) < 5:
        return None
    kind = _kind(plain)
    if kind in _ANSWERED:
        return plain[1], plain[2], plain[3], plain[4]
    if kind == FrameKind.REPLY:
        return plain[2], plain[1], plain[3], plain[4]
    return None


def _route_of(plain: bytes) -> Optional[tuple[int, ...]]:
    """``(SENDER, R1 .. RN, TARGET)`` of a routed plain frame, or ``None`` if malformed."""

    try:
        base, relays = split_route(plain)
    except ValueError:
        return None
    return (base[1], *relays, base[2])


def route_hop_timeout(
    airtime: float, jitter: float, slack: float, answer: float, remaining: int, answered: bool
) -> float:
    """How long a routed hop waits for its acknowledgement before repeating.

    ``remaining`` is the number of hops still to go (1 = into the target);
    ``answered`` says the target will answer (REQUEST, WAKE, KEEPALIVE).

    The next hop's copy is back after two airtimes, but the hop after that
    transmits during the third: a repeat any earlier lands on top of it at
    the node in between, which is hidden from us (measured: on a line every
    lost frame then took the next hop's acknowledgement with it). Close to
    the target the answer comes back while that third airtime runs: three
    hops or fewer from it, wait for the answer itself (it acknowledges every
    hop of its request) rather than repeat into it.
    """

    if answered and remaining <= 3:
        return 2 * remaining * (airtime + jitter) + answer + slack
    return 3 * airtime + 2 * jitter + slack


def _link_etx(
    snr_db: float, floor_db: float, sigma: float, safety: float, break_cost: float = 0.0, attempts: int = 3
) -> float:
    """Expected sends for one hop and its acknowledgement back (symmetric link).

    The chance one frame gets through is modelled as normal in the SNR margin
    over the demodulation floor: ``sigma`` covers fading and the error of one
    measured copy, ``safety`` how far a link may drift below what the probe
    saw (estimates, task B1). ``break_cost`` adds that many sends times the
    chance the hop fails all its ``attempts`` (A5: a hop that runs out of
    repeats breaks the session, which costs a re-probe, not one more send).
    """

    margin = snr_db - floor_db - safety
    through = max(0.01, 0.5 * (1.0 + math.erf(margin / (sigma * math.sqrt(2.0)))))
    both = through * through
    return 1.0 / both + break_cost * (1.0 - both) ** attempts


def _score(links: tuple[LinkQuality, ...], good: int) -> tuple[int, int, int]:
    # Weakest link first, but only up to ``good`` (quarter dB): beyond that a
    # link is as good as it needs to be, and fewer hops win (measured: an
    # uncapped weakest link picked 14 short hops where 6 would do). The sum
    # breaks remaining ties.
    weakest = min(link.snr for link in links)
    return min(weakest, good), -len(links), sum(link.snr for link in links)


@dataclass
class _Flight:
    """A frame this node still has to (re)transmit."""

    raw: bytes
    due: float
    #: Set for frames a REPLY answers, so the reply can find them.
    request: Optional[RequestId] = None
    #: Hearing ``suppress_after`` copies before sending may cancel it.
    suppressible: bool = True
    #: After sending, wait for an implicit acknowledgement and repeat the
    #: frame (``hop_retries``) when none arrives.
    confirm: bool = False
    heard: int = 0
    sent: int = 0
    #: Routed frames: the whole path ``L`` and our place in it.
    route: Optional[tuple[int, ...]] = None
    index: int = 0
    timeout: Optional[float] = None
    retries: Optional[int] = None
    #: Send twice, a short gap apart, without waiting for anyone.
    blind: bool = False
    #: A one-off copy answering a retry from behind (counted apart).
    answer: bool = False
    #: When it last went out; routed hops expect their acknowledgement from then.
    sent_at: float = 0.0
    #: Preamble symbols it goes out with (8: the standard one).
    preamble: int = 8
    #: Called when a copy acknowledges it (the originator's WAKE_NET).
    on_ack: Optional[Callable[[], None]] = None
    #: When this node took it up.
    created: float = 0.0
    #: Routed frames: the ``route_log`` entry this flight reports to.
    log: Optional[tuple[int, int, int, int, int]] = None
    #: The target's reply: sent again only when the last relay repeats the
    #: request (``demanded``), never on a timer of its own, until ``linger``;
    #: each repeat heard moves ``linger`` to at least ``linger_span`` past it.
    on_demand: bool = False
    demanded: bool = False
    linger: float = 0.0
    linger_span: float = 0.0


@dataclass
class HopRecord:
    """What this node saw of one routed message: kept locally, nothing goes on air.

    The raw material for path diagnostics: the gateway sees its own records
    through ``route_event``; a relay's reach it in its TRACE_REPLY record
    (``node_report``). ``state``: ``pending`` (sent, no acknowledgement
    yet), ``acked`` (a copy from further on heard), ``answered`` (the reply
    came back through us), ``delivered`` (we are the target and got it),
    ``gave_up`` (repeats ran out), ``skipped`` (a node
    after us had it first), ``sent`` (sent without waiting: the last hop
    into the gateway, or an answer to a retry).
    """

    kind: int
    sender: int
    target: int
    sequence: int
    epoch: int
    route: tuple[int, ...]
    index: int
    #: RSSI / SNR (dB) of the first copy we received (maybe from a node further
    #: back, past a skipped hop); ``None`` for our own frame.
    heard_rssi: Optional[float] = None
    heard_snr: Optional[float] = None
    #: RSSI / SNR of the copy from the node just before us on the route: this hop's link.
    prev_rssi: Optional[float] = None
    prev_snr: Optional[float] = None
    sends: int = 0
    state: str = "pending"
    #: The copy that acknowledged us: who sent it, and how strong it was.
    ack_from: Optional[int] = None
    ack_rssi: Optional[float] = None
    #: End-to-end attempts of this message seen here (each with a fresh FCNT).
    attempts: int = 1
    #: The last hop into the gateway, sent twice without an acknowledgement.
    blind_last: bool = False

    @property
    def message_id(self) -> int:
        return self.epoch << 8 | self.sequence

    @property
    def repeats(self) -> int:
        """Sends beyond the first that loss made necessary.

        A last hop into the gateway that goes twice by design (``blind_last``)
        does not count its second send.
        """

        planned = 2 if self.blind_last else 1
        return max(0, self.sends - planned)


#: ``(record, event)`` with event ``seen``, ``sent``, ``acked``, ``answered``, ``delivered``, ``gave_up`` or ``skipped``.
RouteEvent = Callable[[HopRecord, str], None]


def _log_key(plain: bytes) -> tuple[int, int, int, int, int]:
    return _kind(plain), plain[1], plain[2], plain[3], plain[4]


@dataclass
class _Done:
    """A routed frame we passed on and saw acknowledged."""

    #: Index in the route of the furthest node known to hold the frame.
    furthest: int
    answered: bool = False


@dataclass
class _TraceOut:
    """A TRACE_REPLY this node appended to and has to pass on toward the gateway.

    Confirmed like a probe: by the next node's copy, which holds more records.
    Into the gateway, which appends nothing, it is simply sent twice.
    """

    plain: bytes
    records: int
    due: float
    blind: bool
    expires: float
    raw: Optional[bytes] = None
    sends: int = 0
    acked: bool = False
    #: The node before us sent its copy again (it missed ours): ours once more,
    #: as it was. Kept until ``expires`` so a repeat never makes a second answer.
    echo: bool = False


@dataclass
class _Probe:
    """One probe this node is waiting to pass on (or, as its target, to answer)."""

    frame: ProbeFrame
    links: tuple[LinkQuality, ...]
    due: float
    expires: float
    target: bool
    sent: bool = False
    #: A relay's probe counts as carried on once a copy naming us is heard;
    #: until then it is sent again (``probe_retries``).
    acked: bool = False
    resends: int = 0
    #: A routed probe (task C5): the relays it still has to pass, after us.
    route: Optional[tuple[int, ...]] = None


class MeshRelay:
    """Relay sealed frames by managed flooding; a ``Transport`` to the layer above.

    ``forward=False`` makes a pure endpoint (the controller's gateway): it
    still filters duplicates and tracks implicit acknowledgements, but never
    relays. Only frames addressed to ``node_id`` (or broadcast) go up, and
    only their first copy. ``stats`` counts what happened to every frame.

    Routed sessions need ``sealer`` (frames the relay originates: re-sealed
    probes, probe replies, route errors) and, at an originator that wants to
    hear about it, ``route_failed``.
    """

    def __init__(
        self,
        radio: RadioPort,
        key: bytes,
        node_id: int,
        *,
        forward: bool = True,
        backoff: tuple[float, float] = (0.05, 1.0),
        suppress_after: Optional[int] = 3,
        guided_replies: bool = True,
        confirm_replies: bool = True,
        hop_retries: int = 1,
        hop_timeout: float = 1.5,
        rssi_range: tuple[float, float] = (-120.0, -40.0),
        rssi_weight: float = 0.7,
        max_pending: int = 8,
        max_paths: int = 8,
        airtime: Airtime = airtime_for(7),
        route_jitter: float = 0.01,
        route_slack: float = 0.02,
        route_answer: float = 0.02,
        route_retries: int = 3,
        last_hop_ack: bool = True,
        probe_backoff: tuple[float, float] = (0.02, 0.6),
        probe_retries: int = 1,
        probe_weak_snr: float = -7.5,
        probe_good_snr: float = 10.0,
        probe_metric: str = "etx",
        etx_sigma: float = 3.0,
        etx_safety: float = 3.0,
        etx_break: float = 200.0,
        probe_delay: str = "cost",
        probe_cost_span: float = 4.0,
        link_memory: int = 16,
        link_weight: float = 0.25,
        noise_floor: float = -117.0,
        wake_preamble: int = 8,
        wake_retries: int = 3,
        wake_copies: int = 3,
        wake_backoff: Optional[tuple[float, float]] = None,
        wake_probe_retries: int = 0,
        join_wakes: bool = True,
        wake_expect: float = -121.5,
        base_sf: int = 7,
        radio_timeout: float = 6.0,
        symbol: float = symbol_time(7),
        max_probes: int = 4,
        probe_ttl: float = 30.0,
        max_done: int = 8,
        max_route_log: int = 8,
        sealer: Optional[Sealer] = None,
        route_failed: Optional[RouteFailed] = None,
        route_event: Optional[RouteEvent] = None,
        clock: Callable[[], float] = time.monotonic,
        rng: Optional[random.Random] = None,
        on_reject: Optional[Callable[[str, bytes], None]] = None,
    ) -> None:
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESCCM
        except ImportError as exc:  # pragma: no cover - depends on the environment
            raise ImportError("MeshRelay needs cryptography: pip install cryptography") from exc
        if len(key) != KEY_LEN:
            raise ValueError(f"key must be {KEY_LEN} bytes")
        if suppress_after is not None and suppress_after < 1:
            raise ValueError("suppress_after must be at least 1 (or None)")
        self._aead = AESCCM(bytes(key), tag_length=MIC_LEN)
        self.radio = radio
        self.node_id = node_id
        self.forward = forward
        self.backoff = backoff
        self.suppress_after = suppress_after
        self.guided_replies = guided_replies
        self.confirm_replies = confirm_replies
        self.hop_retries = hop_retries
        self.hop_timeout = hop_timeout
        self.rssi_range = rssi_range
        self.rssi_weight = rssi_weight
        self.airtime = airtime
        self.route_jitter = route_jitter
        self.route_slack = route_slack
        self.route_answer = route_answer
        self.route_retries = route_retries
        #: The gateway echoes each inbound routed frame it gets (TX = itself),
        #: the acknowledgement every other hop has from the next one's copy;
        #: the last relay waits for it and repeats like on any hop. Off: the
        #: last hop goes twice, blind. Measured (30 % loss, 12 seeds): blind,
        #: 5–6 % of all answers died on that hop, about half the answers lost
        #: on planned grid 40, where no request was lost on the way out.
        self.last_hop_ack = last_hop_ack
        #: Called whenever a frame comes in or one of ours is handed down: what
        #: ``next_due`` said may no longer hold (``MeshNetwork`` skips idle ticks).
        self.activity: Optional[Callable[[], None]] = None
        self.probe_backoff = probe_backoff
        self.probe_retries = probe_retries
        #: SNR (dB) of a barely usable link (SF7 demodulation floor): the longest probe wait.
        self.probe_weak_snr = probe_weak_snr
        #: SNR (dB) from which a link counts as good enough: then fewer hops win.
        self.probe_good_snr = probe_good_snr
        #: How probe paths compare: ``"etx"`` (default, task B1) the lowest sum
        #: of expected sends per hop (``_link_etx``, floor ``probe_weak_snr``),
        #: so several mediocre links add up and strong paths go by hop count;
        #: ``"widest"`` the old rule, best weakest link up to ``probe_good_snr``.
        if probe_metric not in ("etx", "widest"):
            raise ValueError(f"probe_metric must be 'etx' or 'widest', got {probe_metric!r}")
        self.probe_metric = probe_metric
        self.etx_sigma = etx_sigma
        self.etx_safety = etx_safety
        self.etx_break = etx_break
        if probe_delay not in ("quality", "cost"):
            raise ValueError(f"probe_delay must be 'quality' or 'cost', got {probe_delay!r}")
        self.probe_delay = probe_delay
        self.probe_cost_span = probe_cost_span
        #: A probe reports the link it came in on as this node's smoothed RSSI
        #: of that neighbour (EWMA, ``link_weight`` for the new copy) over
        #: every unrouted frame heard from it, not the one copy: with fading,
        #: one copy is ±2 dB off (task B1). ``link_memory`` 0 turns it off.
        self.link_memory = link_memory
        self.link_weight = link_weight
        # [MEM] smoothed RSSI per neighbour | 端: device | 上限: link_memory(默认 16) | 每项: 1 B id + 2 B RSSI
        self._links: OrderedDict[int, float] = OrderedDict()
        #: Sim stand-in for the radio's SNR: RSSI minus this floor (dBm).
        self.noise_floor = noise_floor
        #: Preamble symbols of every WAKE_NET copy (``energy.preamble_for``);
        #: ``symbol`` is one symbol in seconds at the SF ``airtime`` is for.
        self.wake_preamble = wake_preamble
        self.wake_retries = wake_retries
        #: A relay sends a WAKE_NET again (a random backoff later, at most
        #: ``wake_copies`` copies in all) while a neighbour it knows (smoothed
        #: RSSI at least ``wake_expect``) has not been heard since it woke:
        #: that one may still be dormant (a collision or a weak link).
        self.wake_copies = wake_copies
        self.wake_expect = wake_expect
        #: Task Q11 / Q12: the channel and SF this node is on (the common
        #: setting is channel 0 at ``base_sf``). A session's KEEPALIVE names
        #: another; the node moves there once it has passed that keepalive's
        #: answer on (``_radio_flights``), and comes back when it has heard
        #: nothing there for ``radio_timeout`` seconds, or goes dormant.
        self.base_sf = base_sf
        self.radio_timeout = radio_timeout
        self.radio_setting = (0, base_sf)
        self._base_airtime = airtime
        # [MEM] session radio settings waiting for the answer to pass | 端: device | 上限: 2 | 每项: 请求 ID 4 B + 1 B
        self._radio_pending: dict[RequestId, tuple[int, int]] = {}
        self._radio_flights: dict[tuple[int, int], tuple[int, int]] = {}
        self._heard_any = -math.inf
        #: Backoff of a WAKE_NET relay (``None``: ``backoff``).
        self.wake_backoff = wake_backoff
        #: ``probe_retries`` for a wake probe.
        self.wake_probe_retries = wake_probe_retries
        #: Q22: a node that boots in a sleeping network (``WakeAgent.boot``
        #: sets ``boot_until``) sends its JOINs until then as wake JOINs
        #: (``PROBE_WAKE`` in the window byte, long preamble), relayed and
        #: answered like a wake probe: it is heard at once instead of at the
        #: next query round. Only boot JOINs: every JOIN long, or every JOIN a
        #: dormant relay caught passed on long, jammed the air (area power cut:
        #: 12 of 40 never joined, 63 min to an outcome; no power cut at all:
        #: 455 long JOINs in 3 h from retries a CAD look caught by chance).
        self.join_wakes = join_wakes
        self.boot_until = -math.inf
        # [MEM] when each neighbour in the link table was last heard | 端: device | 上限: link_memory(默认 16) | 每项: 4 B 时间
        self._heard_at: dict[int, float] = {}
        #: When this node last heard a WAKE_NET copy (repeats wait for a lull).
        self._wake_heard_last = -math.inf
        self.symbol = symbol
        #: Asked before relaying a fresh WAKE_NET (plain frame); ``False`` drops it.
        self.wake_gate: Optional[Callable[[bytes], bool]] = None
        #: Told when a WAKE_NET this node originated was passed on by someone.
        self.wake_heard: Optional[Callable[[], None]] = None
        #: Dormant (set by ``wake.WakeAgent``): a frame a CAD look happened to
        #: catch is dropped unless it is a WAKE_NET, and nothing is recorded.
        self.asleep = False
        self.max_probes = max_probes
        self.probe_ttl = probe_ttl
        self.sealer = sealer
        self.route_failed = route_failed
        #: Told of every change to a ``route_log`` entry (called with the lock held: record only).
        self.route_event = route_event
        self.max_route_log = max_route_log
        # [MEM] routed messages seen lately (diagnostics, nothing on air) | 端: device(中继/目标)、网关 | 上限: max_route_log(默认 8) | 每项: 键 5 B + 位置 1 B + RSSI/SNR 2 B + 次数 1 B + 状态 1 B + 确认者与其 RSSI 2 B ≈ 12 B（路由取自在途帧，不另存）
        self.route_log: OrderedDict[tuple[int, int, int, int, int], HopRecord] = OrderedDict()
        #: See ``Observer``; set by a layer that watches the neighbourhood.
        self.observer: Optional[Observer] = None
        #: More observers, for layers beside the one in ``observer``.
        self.observers: list[Observer] = []
        #: Told of each JOIN / DISCOVER collected here (``PathFound``); a PROBE
        #: is still answered by this layer itself.
        self.path_found: Optional[PathFound] = None
        self.max_pending = max_pending
        self.clock = clock
        self._rng = rng or random.Random()
        self._on_reject = on_reject
        self._receiver: Optional[Receiver] = None
        self._lock = threading.Lock()
        # [MEM] per-origin relay windows, RAM only (``reboot``) | 端: device(中继) | 上限: 256 个源 | 每项: 9 B
        self._windows: dict[int, ReplayWindow] = {}
        # [MEM] frames waiting to be relayed or acknowledged | 端: device(中继) | 上限: max_pending(默认 8) | 每项: 1 个安全帧(≤75 B：路由命令帧 ≤38 B，PROBE_REPLY ≤65 B，DISCOVER_REPLY ≤75 B) + 计时 + 计数
        self._flights: dict[tuple[int, int], _Flight] = {}
        # [MEM] requests this node relayed (reply backbone) | 端: device(中继) | 上限: max_paths(默认 8) | 每项: 4 B
        self._paths: deque[RequestId] = deque(maxlen=max_paths)
        # [MEM] acknowledged routed frames (to answer a retry) | 端: device(中继) | 上限: max_done(默认 8) | 每项: (源 1 B, FCNT 4 B) + 1 B 位置 + 1 位
        self._done: OrderedDict[tuple[int, int], _Done] = OrderedDict()
        self._max_done = max_done
        # [MEM] probes waiting / recently passed on | 端: device(中继/目标) | 上限: max_probes(默认 4) | 每项: (ORIGIN, 消息 ID) 3 B + ≤55 B 候选 + 计时
        self._probes: dict[tuple[int, int], _Probe] = {}
        # [MEM] traces passing through (trace id -> traced message) | 端: device(中继/目标) | 上限: 2 | 每项: 网关 1 B + 追踪 ID 2 B + 目标 1 B + 消息 ID 2 B
        self._traces: OrderedDict[tuple[int, int], tuple[int, int, int]] = OrderedDict()
        # [MEM] trace replies to pass on | 端: device(中继/目标) | 上限: 2 | 每项: ≤ 83 B 安全帧 + 计时 + 次数
        self._trace_out: dict[tuple[int, int], _TraceOut] = {}
        # [MEM] answers held back (request heard past the last relay) | 端: device(目标) | 上限: 2 | 每项: 请求 ID 4 B + 时间 4 B
        self._answer_hold: OrderedDict[RequestId, float] = OrderedDict()
        #: Echoes of inbound frames to send once the lock is released (``_echo``).
        self._echoes: list[bytes] = []
        self.stats = {
            "originated": 0,
            "repeated": 0,
            "relayed": 0,
            "suppressed": 0,
            "answered": 0,
            "duplicates": 0,
            "not_on_path": 0,
            "overflow": 0,
            "auth_failed": 0,
            "plain_dropped": 0,
            "routed": 0,
            "not_on_route": 0,
            "bad_route": 0,
            "observer_errors": 0,
            "hop_acked": 0,
            "overtaken": 0,
            "retry_answered": 0,
            "echoed": 0,
            "route_errors": 0,
            "route_dropped": 0,
            "probes_relayed": 0,
            "probes_answered": 0,
            "probes_ignored": 0,
            "traces_answered": 0,
            "traces_relayed": 0,
            "unsealed": 0,
            "wakes_relayed": 0,
            "wakes_refused": 0,
            "wake_repeats": 0,
            "wakes_done": 0,
            "asleep_dropped": 0,
            "radio_switches": 0,
            "radio_reverted": 0,
        }
        radio.set_receiver(self._on_radio)

    # ----------------------------------------------------- Transport (upward)

    def set_receiver(self, callback: Receiver) -> None:
        self._receiver = callback

    def send(self, data: bytes) -> None:
        """Broadcast a frame sealed by the layer above."""

        self._originate(bytes(data))

    def _originate(self, raw: bytes) -> None:
        if self.activity is not None:
            self.activity()
        plain, fcnt = open_sealed(self._aead, raw)  # our own frame: never fails
        now = self.clock()
        send_now = True
        preamble = 8
        with self._lock:
            # Our own frame coming back is a copy, and an acknowledgement.
            self._window(plain[1]).accept(fcnt)
            self.stats["originated"] += 1
            room = len(self._flights) < self.max_pending
            if plain[0] == FrameKind.PROBE | ROUTED and plain[1] == plain[5] == self.node_id:
                # Our own probe along a route (task C5): confirmed by the
                # first relay's copy, like a flooded one; repeated on its route.
                base, relays = split_route(plain)
                frame = ProbeFrame.from_bytes(base)
                if _probe_key(frame) not in self._probes:  # a resend keeps its count (2026-10-27)
                    probe = _Probe(frame, (), now, now + self.probe_ttl, False, sent=True, route=relays)
                    probe.due = now + self._probe_ack_wait(probe)
                    self._keep_probe(_probe_key(frame), probe)
            elif plain[0] == FrameKind.PROBE | ROUTED:
                # A relay's probe along a route, sealed as its own: the next
                # relay seals its copy as a new frame too, so only the probe
                # state (acked by a copy naming us) confirms it. A routed
                # flight here repeated it three times and reported the route
                # broken (2026-10-27, relay vectors).
                pass
            elif plain[0] & ROUTED:
                route = _route_of(plain)
                if _kind(plain) == FrameKind.TRACE:
                    self._note_trace(plain)  # so its answer acknowledges our hop
                if route is not None and self.node_id in route and room:
                    index = route.index(self.node_id)
                    hold = self._answer_hold.pop(_request_id(plain), None)
                    busy = self._channel_busy(now)
                    if busy is not None:
                        hold = max(hold or 0.0, busy)
                    if index < len(route) - 1:
                        if hold is not None and hold > now:
                            # The request reached us past the last relay,
                            # which is about to pass it on: answering now
                            # would collide with that copy.
                            send_now = False
                            self._flights[(plain[1], fcnt)] = self._routed_flight(
                                raw, plain, route, index, now, sent=0, wait=hold - now
                            )
                        else:
                            flight = self._routed_flight(raw, plain, route, index, now, sent=1)
                            self._flights[(plain[1], fcnt)] = flight
                            preamble = flight.preamble
                        self._follow_answer((plain[1], fcnt), plain)
            elif plain[0] in PATH_KINDS and plain[1] == plain[5] == self.node_id:
                # Our own new probe (or JOIN, DISCOVER): confirmed like a
                # relay's, by any copy passed on (lost on its first hop,
                # nobody would ever see it).
                frame = _PATH_FRAMES[plain[0]].from_bytes(plain)
                key = _probe_key(frame)
                if key not in self._probes:
                    # A resend comes through here too: replacing the state reset
                    # its count, and it went again every second until probe_ttl
                    # (2026-10-27, relay vectors).
                    probe = _Probe(frame, (), now, now + self.probe_ttl, False, sent=True)
                    probe.due = now + self._probe_ack_wait(probe)
                    self._keep_probe(key, probe)
            elif plain[0] == FrameKind.WAKE_NET:
                if room:
                    self._flights[(plain[1], fcnt)] = _Flight(
                        raw,
                        now + self._wake_ack_wait(len(raw)),
                        confirm=True,
                        sent=1,
                        retries=self.wake_retries,
                        timeout=self._wake_ack_wait(len(raw)),
                        preamble=self.wake_preamble,
                        on_ack=self.wake_heard,
                    )
                preamble = self.wake_preamble
            # Nobody relays a HELLO, and every relay re-seals a PROBE as its
            # own frame: waiting for a copy of either would only send it twice.
            elif plain[0] != FrameKind.HELLO and plain[0] not in PATH_KINDS:
                if self.hop_retries > 0 and room:
                    self._flights[(plain[1], fcnt)] = _Flight(
                        raw,
                        now + self.hop_timeout,
                        request=self._request_of(plain),
                        confirm=True,
                        sent=1,
                    )
        if _is_wake_probe(plain):
            preamble = self.wake_preamble
        if send_now:
            self._put(raw, preamble)

    def set_radio(self, channel: int, sf: int) -> None:
        """Move to ``channel`` at ``sf`` (the radio follows if it can); timing follows the SF's airtime."""

        if (channel, sf) == self.radio_setting:
            return
        self.radio_setting = (channel, sf)
        self.stats["radio_switches"] += 1
        self._heard_any = max(self._heard_any, self.clock())  # silence counts from the move
        self.airtime = self._base_airtime if sf == self.base_sf else airtime_for(sf)
        self.symbol = symbol_time(sf)
        setter = getattr(self.radio, "set_radio", None)
        if setter is not None:
            setter(channel, sf)

    def _put(self, raw: bytes, preamble: int = 8) -> None:
        """Hand one copy to the radio, with a long preamble when asked for one."""

        if _is_wake_preamble(preamble):
            self.radio.send(raw, preamble=preamble)
        else:
            self.radio.send(raw)

    @property
    def wake_lead(self) -> float:
        """Seconds a wake preamble adds to a frame's airtime."""

        return (self.wake_preamble - 8) * self.symbol

    def _wake_ack_wait(self, length: int) -> float:
        # The first relay backs off at most ``backoff[1]``, then sends a
        # long preamble and the frame.
        lead = self.wake_lead
        return (self.wake_backoff or self.backoff)[1] + lead + 2 * self.airtime(length) + self.route_slack

    # ---------------------------------------------------------------- driving

    def tick(self, now: Optional[float] = None) -> None:
        """Process flights, then traces, then probes under one lock; RNG and send order matter."""

        now = self.clock() if now is None else now
        sends, failed, traces, probes = [], [], [], []
        busy = self._channel_busy(now)
        with self._lock:
            self._tick_flights(now, busy, sends, failed)
            self._tick_traces(now, busy, traces)
            self._tick_probes(now, busy, probes)
        with self._lock:
            moves = [(ident, radio) for ident, radio in self._radio_flights.items() if ident not in self._flights]
            for ident, _ in moves:
                del self._radio_flights[ident]
            stuck = (
                self.forward
                and self.radio_setting != (0, self.base_sf)
                and now - self._heard_any >= self.radio_timeout
            )
        for raw, preamble in sends:
            self._put(raw, preamble)
        for _, radio in moves:
            self.set_radio(*radio)  # the answer is on its way: follow the session
        if stuck:
            self.stats["radio_reverted"] += 1
            self.set_radio(0, self.base_sf)  # nothing heard on the session's setting: back home
        for flight in failed:
            self._route_broken(flight)
        for probe in probes:
            self._pass_probe(probe)
        for out in traces:
            self._pass_trace(out)

    def _tick_flights(self, now: float, busy: Optional[float], sends: list[tuple[bytes, int]], failed: list[_Flight]) -> None:
        """Process flights (shared lock held)."""

        for ident, flight in list(self._flights.items()):
            if now < flight.due:
                continue
            if busy is not None:
                # Listen before talk: a neighbour is on the air. Sending
                # now would lose both frames, ours and the one it may be
                # acknowledging us with (measured: adjacent relays'
                # repeats ran about one airtime apart and collided). Plain
                # floods too (2026-10-27, after Meshtastic: about 10 %
                # fewer sends and 30 % fewer collisions with a 10 ms
                # detection delay; delivery unchanged).
                if _is_wake_preamble(flight.preamble):
                    # A long preamble: neighbours that waited out the same
                    # copy would otherwise all start within the jitter,
                    # and those that cannot hear each other collide at
                    # every dormant node between them (measured).
                    flight.due = busy + self._backoff(None, self.wake_backoff)
                else:
                    flight.due = busy + self._rng.uniform(0.0, self.route_jitter)
                continue
            if flight.blind:
                # The last hop into the gateway (never acknowledged) goes
                # twice, a short gap apart (A5: once only was worse); a
                # flight made with both sends behind it goes no more.
                if flight.sent >= 2:
                    del self._flights[ident]
                    continue
                self.stats["relayed" if flight.sent == 0 else "repeated"] += 1
                if flight.sent == 0 and flight.route is not None and flight.index:
                    self.stats["routed"] += 1
                flight.sent += 1
                if flight.sent >= 2:
                    del self._flights[ident]
                else:
                    flight.due = now + self._repeat_gap(len(flight.raw))
                self._log(flight.log, "sent", waits=False)
                sends.append((flight.raw, flight.preamble))
                continue
            retries = self.hop_retries if flight.retries is None else flight.retries
            if _is_wake_preamble(flight.preamble) and not flight.confirm and flight.sent and flight.route is None:
                quiet = self._wake_heard_last + self._wake_gap(len(flight.raw))
                if now < quiet:
                    # Wake copies are still going round here: those
                    # unheard may just be queued behind them. Repeating now fed the
                    # congestion it waited on (measured: 110 long copies
                    # for one wake, 35 s on air).
                    flight.due = quiet
                    continue
                if not self._wake_needed(flight):
                    del self._flights[ident]  # every neighbour we know is awake
                    self.stats["wakes_done"] += 1
                    continue
            if flight.answer:
                self.stats["retry_answered"] += 1
                del self._flights[ident]
                self._log(flight.log, "sent", waits=False)
                sends.append((flight.raw, flight.preamble))
                continue
            if flight.on_demand:
                if flight.sent and not flight.demanded:
                    del self._flights[ident]  # nobody asked again: it got there
                    continue
                if flight.sent:
                    self.stats["repeated"] += 1
                flight.sent += 1
                flight.demanded = False
                flight.due = flight.linger
                if flight.sent > retries + 1:
                    del self._flights[ident]
                self._log(flight.log, "sent", waits=False)
                sends.append((flight.raw, flight.preamble))
                continue
            if flight.sent == 0 and flight.route is not None and flight.index == 0:
                pass  # our own answer, held back: not a relay
            elif flight.sent == 0:
                self.stats["relayed"] += 1
                if _is_wake_preamble(flight.preamble):
                    self.stats["wakes_relayed"] += 1
                if flight.route is not None:
                    self.stats["routed"] += 1
                elif flight.request is not None:
                    self._paths.append(flight.request)
            elif flight.sent > retries:
                # Nobody carried it on; the controller's end-to-end retry
                # (with a fresh FCNT, hence a new flood) takes over.
                del self._flights[ident]
                if flight.route is not None:
                    failed.append(flight)
                continue
            else:
                self.stats["repeated"] += 1
                if _is_wake_preamble(flight.preamble):
                    self.stats["wake_repeats"] += 1
            flight.sent += 1
            flight.sent_at = now
            if flight.confirm:
                if flight.timeout is None:
                    flight.due = now + self.hop_timeout
                else:
                    # Routed: up to one more airtime at random, so two
                    # senders around one receiver (measured: the gateway
                    # and a relay reporting back) cannot repeat in step.
                    spread = self.airtime(len(flight.raw))
                    flight.due = now + flight.timeout + self._rng.uniform(0.0, spread)
            elif _is_wake_preamble(flight.preamble) and flight.sent < self.wake_copies:
                flight.due = now + self._wake_gap(len(flight.raw))
            else:
                del self._flights[ident]
            if flight.route is not None:
                self._log(flight.log, "sent", waits=flight.confirm)
            sends.append((flight.raw, flight.preamble))

    def _tick_traces(self, now: float, busy: Optional[float], traces: list[_TraceOut]) -> None:
        """Process traces (shared lock held)."""

        for key, out in list(self._trace_out.items()):
            if now >= out.expires:
                del self._trace_out[key]
                continue
            if now < out.due or (out.acked and not out.echo):
                continue
            if busy is not None:
                out.due = busy + self._rng.uniform(0.0, self.route_jitter)  # listen before talk
                continue
            if out.echo:
                out.echo = False
                traces.append(out)
                continue
            if out.sends > (1 if out.blind else self.route_retries):
                out.acked = True  # give up; kept so a late repeat makes no second answer
                self.stats["route_dropped"] += 1
                continue
            out.sends += 1
            length = len(out.plain) + 9
            if out.blind:
                out.due = now + self._repeat_gap(length)
            else:
                timeout = self._route_timeout(length + _TRACE_GROWTH, 0, False)
                out.due = now + timeout + self._rng.uniform(0.0, self.airtime(length))
            traces.append(out)

    def _tick_probes(self, now: float, busy: Optional[float], probes: list[_Probe]) -> None:
        """Process probes (shared lock held)."""

        for key, probe in list(self._probes.items()):
            if busy is not None and now >= probe.due and (not probe.sent or self._probe_awaiting(probe)):
                if probe.frame.wake:  # a long preamble: see the WAKE_NET flights above
                    probe.due = busy + self._backoff(None, self.wake_backoff)
                else:
                    probe.due = busy + self._rng.uniform(0.0, self.route_jitter)  # listen before talk
                continue
            if probe.sent:
                if now >= probe.expires:
                    del self._probes[key]
                elif self._probe_unconfirmed(probe, now):
                    # Nobody further out passed it on (a collision at
                    # the edge kills a probe): once more, as a new frame.
                    probe.resends += 1
                    probe.due = now + self._probe_ack_wait(probe)
                    probes.append(probe)
            elif now >= probe.due:
                probe.sent = True
                probe.due = now + self._probe_ack_wait(probe)
                probes.append(probe)

    def next_due(self) -> float:
        """The earliest time ``tick`` can do anything; until then it is a no-op.

        Never later than the truth (earlier only costs an idle tick). A frame
        heard or handed down changes it: see ``activity``.
        """

        due = math.inf
        with self._lock:
            for flight in self._flights.values():
                due = min(due, flight.due)
            for out in self._trace_out.values():
                due = min(due, out.expires)
                if not (out.acked and not out.echo):
                    due = min(due, out.due)
            for probe in self._probes.values():
                if not probe.sent:
                    due = min(due, probe.due)
                else:
                    due = min(due, probe.expires)
                    if self._probe_awaiting(probe):
                        due = min(due, probe.due)
            # A move waits for its flight to go (its due is counted above);
            # once it has gone, the next tick moves.
            if any(ident not in self._flights for ident in self._radio_flights):
                return -math.inf
            if self.forward and self.radio_setting != (0, self.base_sf):
                due = min(due, self._heard_any + self.radio_timeout)
        return due

    def pending(self) -> int:
        """Frames waiting to be relayed or acknowledged, probes included."""

        with self._lock:
            waiting = sum(
                not probe.sent or self._probe_awaiting(probe) for probe in self._probes.values()
            )
            waiting += sum(not out.acked or out.echo for out in self._trace_out.values())
            return len(self._flights) + waiting

    def _channel_busy(self, now: float) -> Optional[float]:
        """Until when the radio senses activity, or ``None`` when the channel is clear."""

        sense = getattr(self.radio, "busy_until", None)
        if sense is None:
            return None
        until = sense()
        return until if until > now else None

    def _probe_awaiting(self, probe: _Probe) -> bool:
        # A wake probe floods the whole network with long preambles: a resend
        # adds as much again, which buried the target's answer (measured).
        if probe.route is not None:
            retries = self.route_retries  # one path, no other copy to make up for it
        else:
            retries = self.wake_probe_retries if probe.frame.wake else self.probe_retries
        return not probe.target and not probe.acked and probe.resends < retries

    def _note_link(self, tx: int, rssi: Optional[float]) -> Optional[float]:
        """Fold an unrouted copy from ``tx`` into its smoothed RSSI; return the estimate (lock held).

        Only unrouted frames: there TX is always who sent this copy (a routed
        answer copy carries the TX of the furthest node, not the sender's).
        """

        if rssi is None or self.link_memory <= 0:
            return rssi
        level = self._links.pop(tx, None)
        level = rssi if level is None else (1.0 - self.link_weight) * level + self.link_weight * rssi
        self._links[tx] = level
        while len(self._links) > self.link_memory:
            self._links.popitem(last=False)  # heard least recently
        return level

    def _wake_gap(self, length: int) -> float:
        # Long enough for the neighbours this copy woke to pass it on first.
        lead = self.wake_lead
        window = self.wake_backoff or self.backoff
        return window[1] + lead + self.airtime(length) + self._backoff(None, window)

    def _wake_needed(self, flight: _Flight) -> bool:
        """Is a neighbour this relay knows (at least ``wake_expect``) unheard since it woke (lock held)?

        Then it may still be dormant, and a repeat is worth its long preamble.
        (Skipping the first copy the same way was measured to save nothing:
        a node wakes after most neighbours have already passed the wake on.)
        """

        since = flight.created
        return any(
            level >= self.wake_expect and self._heard_at.get(node, -math.inf) < since
            for node, level in self._links.items()
        )

    def _probe_unconfirmed(self, probe: _Probe, now: float) -> bool:
        return self._probe_awaiting(probe) and now >= probe.due

    def _probe_ack_wait(self, probe: _Probe) -> float:
        # The next relay waits up to the longest probe backoff, then sends.
        # The repeat itself goes at a fresh random point: two relays whose
        # copies collided once would collide again a fixed time later.
        length = len(probe.frame.encode()) + 9 + 3
        high = self.probe_backoff[1]
        lead = self.wake_lead if probe.frame.wake else 0.0
        return high + 2 * (self.airtime(length) + lead) + self.route_slack + self._rng.uniform(0.0, high)

    # -------------------------------------------------------------- receiving

    def _on_radio(self, raw: bytes, rssi: Optional[float] = None) -> None:
        if self.activity is not None:
            self.activity()
        if not raw:
            return
        if not raw[0] & SECURE:
            self._reject("plain_dropped", "mesh relays sealed frames only", raw)
            return
        try:
            plain, fcnt = open_sealed(self._aead, raw)
        except ValueError as exc:
            self._reject("auth_failed", str(exc), raw)
            return
        except Exception:  # noqa: BLE001 - cryptography.exceptions.InvalidTag
            self._reject("auth_failed", "message authentication failed", raw)
            return

        if self.asleep and not self._wakes_us(plain):
            self.stats["asleep_dropped"] += 1
            return
        origin, target = plain[1], plain[2]
        ident = (origin, fcnt)
        tx = transmitter(raw)
        routed = bool(plain[0] & ROUTED)
        with self._lock:
            self._echoes = []
            self._heard_at[tx] = self._heard_any = self.clock()
            if plain[0] == FrameKind.WAKE_NET:
                self._wake_heard_last = self._heard_at[tx]
            if len(self._heard_at) > 2 * max(1, self.link_memory):
                for node in [n for n in self._heard_at if n not in self._links and n != tx]:
                    del self._heard_at[node]
            window = self._window(origin)
            fresh = window.check(fcnt)
            if not fresh:
                if routed:
                    self._on_routed_copy(ident, plain, fcnt, tx, rssi)
                else:
                    self._on_copy(ident)
            else:
                window.accept(fcnt)
                # TX is the sender on unrouted frames and on routed probes
                # (sealed again at every hop); not on other routed ones.
                routed_probe = plain[0] == FrameKind.PROBE | ROUTED
                heard = rssi if routed and not routed_probe else self._note_link(tx, rssi)
                if _kind(plain) == FrameKind.REPLY:
                    self._on_reply(_request_id(plain), tx, rssi)
                if routed and _kind(plain) == FrameKind.TRACE_REPLY:
                    self._on_trace_reply(plain)
                elif routed_probe:
                    self._on_routed_probe(plain, heard)
                elif routed:
                    self._route_relay(ident, plain, fcnt, tx, rssi)
                elif plain[0] in PATH_KINDS:
                    self._on_probe(plain, heard)
                elif self._should_relay(plain, origin, target):
                    self._schedule(ident, plain, rssi)
            echoes, self._echoes = self._echoes, []
        for echo in echoes:
            self._put(echo, 8)
        if not fresh:
            return
        for observer in (self.observer, *self.observers):
            if observer is not None:
                # One failing observer must not keep the frame from the others or from above (review B4).
                try:
                    observer(plain, rssi)
                except Exception:  # noqa: BLE001 - counted; the frame goes on
                    self.stats["observer_errors"] += 1
        if plain[0] in PATH_KINDS or plain[0] in (FrameKind.TRACE | ROUTED, FrameKind.PROBE | ROUTED):
            return  # consumed here; the target answers from this layer
        if target in (self.node_id, BROADCAST_ID) and self._receiver is not None:
            self._receiver(raw)

    def _wakes_us(self, plain: bytes) -> bool:
        """May this frame wake a dormant node: a WAKE_NET, a wake probe, or routed through or to it?"""

        if plain[0] == FrameKind.WAKE_NET or _is_wake_probe(plain):
            return True
        if not plain[0] & ROUTED:
            return False
        route = _route_of(plain)
        return route is not None and self.node_id in route[1:]

    def joins_wake(self) -> bool:
        """Q22: our JOINs now are wake JOINs (booting in a sleeping network)."""

        return self.join_wakes and _is_wake_preamble(self.wake_preamble) and self.clock() < self.boot_until

    # ------------------------------------------------------- rules (lock held)

    def mark(self, origin: int) -> Optional[int]:
        """Highest FCNT heard from ``origin`` (relayed or not), or ``None``."""

        with self._lock:
            window = self._windows.get(origin)
            return None if window is None else window.last

    def reboot(self, marks: Optional[dict[int, int]] = None) -> None:
        """Power back on: the relay windows were in RAM and start empty.

        ``marks`` (origin -> FCNT) seeds windows from what the node did
        persist, the controller's mark (spec section 7), so the
        controller's old floods are not relayed again. Any other origin's
        frame from before the reboot may be relayed once more.
        """

        with self._lock:
            self._windows = {origin: ReplayWindow.restored(last) for origin, last in (marks or {}).items()}

    def _window(self, origin: int) -> ReplayWindow:
        window = self._windows.get(origin)
        if window is None:
            window = self._windows[origin] = ReplayWindow()
        return window

    @staticmethod
    def _request_of(plain: bytes) -> Optional[RequestId]:
        return _request_id(plain) if _kind(plain) != FrameKind.REPLY else None

    def _on_copy(self, ident: tuple[int, int]) -> None:
        self.stats["duplicates"] += 1
        flight = self._flights.get(ident)
        if flight is None:
            return
        if flight.sent and _is_wake_preamble(flight.preamble) and not flight.confirm:
            return  # a relay's further wake copies go out blind
        if flight.sent:
            del self._flights[ident]  # someone carried it on: implicit ack
            if flight.on_ack is not None:
                flight.on_ack()
            return
        flight.heard += 1
        limit = self.suppress_after
        if flight.suppressible and limit is not None and flight.heard >= limit:
            del self._flights[ident]
            self.stats["suppressed"] += 1

    def _on_reply(
        self, request: Optional[RequestId], tx: Optional[int] = None, rssi: Optional[float] = None
    ) -> None:
        # The target answered: relays of the request still waiting are
        # pointless, and an own request is acknowledged end to end. On a
        # route the reply acknowledges every hop of its request.
        for ident, flight in list(self._flights.items()):
            if request is not None and flight.request == request:
                del self._flights[ident]
                if flight.route is not None:
                    self._note_done(ident, len(flight.route) - 1)
                    self._log(flight.log, "answered", tx, rssi)
                if not flight.sent:
                    self.stats["answered"] += 1

    def _should_relay(self, plain: bytes, origin: int, target: int) -> bool:
        if not self.forward or self.node_id in (origin, target):
            return False
        if plain[0] == FrameKind.HELLO:
            return False  # one hop by definition
        if _kind(plain) == FrameKind.REPLY and self.guided_replies:
            if _request_id(plain) not in self._paths:
                self.stats["not_on_path"] += 1
                return False
        return True

    def _schedule(self, ident: tuple[int, int], plain: bytes, rssi: Optional[float]) -> None:
        if len(self._flights) >= self.max_pending:
            self.stats["overflow"] += 1
            return
        wake = plain[0] == FrameKind.WAKE_NET
        if wake and self.wake_gate is not None and not self.wake_gate(plain):
            self.stats["wakes_refused"] += 1
            return
        due = self.clock() + self._backoff(rssi, self.wake_backoff if wake else None)
        # Only frames spreading out from the controller (REQUEST, WAKE) are
        # suppressed: their target may be anywhere, so a covered
        # neighbourhood is all that matters. Everything else heads for one
        # place, the controller; copies heard from relays further out say
        # nothing about the controller's side, and counting them silences
        # the relays nearest to it (measured: replies and reports died in
        # the last hops).
        outward = _kind(plain) in _SPREADING
        self._flights[ident] = _Flight(
            seal(self._aead, plain, ident[1], self.node_id),  # same frame, TX = us
            due,
            request=self._request_of(plain),
            suppressible=outward,
            confirm=_kind(plain) == FrameKind.REPLY and self.confirm_replies,
            preamble=self.wake_preamble if wake else 8,
            retries=self.wake_copies - 1 if wake else None,
            created=self.clock(),
        )

    def _backoff(self, rssi: Optional[float], window: Optional[tuple[float, float]] = None) -> float:
        low, high = window or self.backoff
        if rssi is None:
            share = self._rng.random()
        else:
            weak, strong = self.rssi_range
            closeness = min(1.0, max(0.0, (rssi - weak) / (strong - weak)))
            # Part position, part chance, so equally placed relays still
            # spread out instead of colliding.
            weight = self.rssi_weight
            share = weight * closeness + (1.0 - weight) * self._rng.random()
        return low + (high - low) * share

    # ------------------------------------------------ routed frames (lock held)

    def _route_timeout(self, length: int, remaining: int, answered: bool) -> float:
        return route_hop_timeout(
            self.airtime(length),
            self.route_jitter,
            self.route_slack,
            self.route_answer,
            remaining,
            answered,
        )

    def _repeat_gap(self, length: int) -> float:
        # Right behind the first copy: the gateway holds its next frame on
        # this route for one airtime plus jitter, so the repeat never falls
        # on top of it.
        return self.airtime(length) + self._rng.uniform(0.0, self.route_jitter)

    def _route_wait(self, length: int, index: int, heard_from: int) -> float:
        """How long to wait before passing on a copy heard from ``heard_from``.

        From the node just before us: a jitter. From further back (a skipped
        hop): one more slot for every relay in between, so the relay whose
        turn it is goes first and its copy cancels or re-times ours instead
        of colliding with it; we take over only if it stays silent.
        """

        slot = self.airtime(length) + self.route_jitter + self.route_slack
        skipped = max(0, index - heard_from - 1)
        return self._rng.uniform(0.0, self.route_jitter) + skipped * slot

    def _routed_flight(
        self,
        raw: bytes,
        plain: bytes,
        route: tuple[int, ...],
        index: int,
        now: float,
        sent: int,
        wait: float = 0.0,
        rssi: Optional[float] = None,
        tx: Optional[int] = None,
    ) -> _Flight:
        flight = self._make_routed_flight(raw, plain, route, index, now, sent, wait)
        flight.log = self._log_seen(plain, route, index, rssi, tx)
        if sent:
            self._log(flight.log, "sent", waits=flight.confirm)
        return flight

    def _make_routed_flight(
        self,
        raw: bytes,
        plain: bytes,
        route: tuple[int, ...],
        index: int,
        now: float,
        sent: int,
        wait: float = 0.0,
    ) -> _Flight:
        inbound = _kind(plain) in _INBOUND
        last_hop = index == len(route) - 2
        # The answer to a wake probe: the relays on its way back may have
        # gone dormant again meanwhile, so every hop gets the long preamble.
        preamble = self.wake_preamble if self._answers_wake_probe(plain) else 8
        lead = (preamble - 8) * self.symbol
        if inbound and last_hop and not (self.last_hop_ack and _kind(plain) in _ECHOED):
            # Into the gateway, which relays nothing: no acknowledgement
            # will come, so send it twice instead of waiting for one.
            due = now + (self._repeat_gap(len(raw)) + lead if sent else wait)
            return _Flight(
                raw, due, route=route, index=index, blind=True, sent=sent, suppressible=False, preamble=preamble
            )
        remaining = len(route) - 1 - index
        length = len(raw)
        if _kind(plain) == FrameKind.TRACE:
            length += 4 * remaining  # its answer comes back longer, a record per node
        timeout = self._route_timeout(length, remaining, answered=not inbound) + lead
        if index == 0 and _kind(plain) == FrameKind.REPLY and self.forward:
            # The target's answer (a gateway's own REPLY, acknowledging a
            # JOIN, is an ordinary routed frame: repeated until a hop takes it). The last relay repeats its request until it
            # hears this; both repeating on timers of their own would make
            # them collide in step (measured). Answer each repeat instead,
            # for as long as the relay can still repeat.
            span = (self.route_retries + 1) * (timeout + self.route_answer + self.route_jitter)
            linger = now + span
            return _Flight(
                raw,
                now + wait if not sent else linger,
                suppressible=False,
                sent=sent,
                route=route,
                index=index,
                retries=self.route_retries,
                on_demand=True,
                linger=linger,
                linger_span=span,
            )
        due = now + (timeout if sent else wait)
        return _Flight(
            raw,
            due,
            request=None if inbound else _request_id(plain),
            suppressible=False,
            confirm=True,
            sent=sent,
            route=route,
            index=index,
            timeout=timeout,
            retries=self.route_retries,
            sent_at=now,
            preamble=preamble,
        )

    def _follow_answer(self, ident: tuple[int, int], plain: bytes) -> None:
        """A REPLY to a keepalive that named a new radio setting: move once it is passed on (lock held)."""

        if _kind(plain) == FrameKind.REPLY:
            radio = self._radio_pending.pop(_request_id(plain), None)
            if radio is not None:
                self._radio_flights[ident] = radio

    def _answers_wake_probe(self, plain: bytes) -> bool:
        """Is ``plain`` a PROBE_REPLY to a wake probe (or, Q22, the REPLY to a JOIN) this node passed on or answered (lock held)?"""

        if not _is_wake_preamble(self.wake_preamble):
            return False
        if _kind(plain) == FrameKind.REPLY:
            join = self._probes.get((plain[2], plain[4] << 8 | plain[3], FrameKind.JOIN))
            return join is not None and join.frame.wake
        if _kind(plain) != FrameKind.PROBE_REPLY:
            return False
        probe = self._probes.get((plain[2], plain[4] << 8 | plain[3]))
        return probe is not None and probe.frame.wake

    def _route_relay(
        self, ident: tuple[int, int], plain: bytes, fcnt: int, tx: int, rssi: Optional[float]
    ) -> None:
        route = _route_of(plain)
        if route is None:
            self.stats["bad_route"] += 1
            return
        # A good MIC does not make a frame well formed (a node holding the
        # network key can send anything): each field read is checked (review B4).
        if _kind(plain) == FrameKind.TRACE and not self._note_trace(plain):
            return
        if _kind(plain) == FrameKind.KEEPALIVE and self.node_id in route[1:]:
            base, _ = split_route(plain)
            try:
                if len(base) != 7:
                    raise ValueError("keepalive length")
                radio = radio_of(base[6])
            except ValueError:
                self.stats["bad_route"] += 1
                return
            if radio != self.radio_setting:
                self._radio_pending[_request_id(plain)] = radio
                while len(self._radio_pending) > 2:
                    self._radio_pending.pop(next(iter(self._radio_pending)))
        if self.node_id == route[-1]:
            # We are the target: it goes up. Heard past the last relay, our
            # answer waits for that relay's copy (or its slot) first.
            self._log(self._log_seen(plain, route, len(route) - 1, rssi, tx), "delivered", tx, rssi)
            if self.last_hop_ack and _kind(plain) in _ECHOED:
                self._echo(ident, plain, fcnt, route)
            if _kind(plain) == FrameKind.TRACE:
                self._answer_trace(plain, route)
                return
            if tx in route and _kind(plain) not in _INBOUND:
                skipped = len(route) - 2 - route.index(tx)
                if skipped > 0:
                    slot = self.airtime(len(plain) + 9) + self.route_jitter + self.route_slack
                    self._answer_hold[_request_id(plain)] = self.clock() + skipped * slot
                    while len(self._answer_hold) > 2:
                        self._answer_hold.popitem(last=False)
            return
        if not self.forward or self.node_id not in route[1:-1]:
            self.stats["not_on_route"] += 1
            return
        index = route.index(self.node_id)
        if tx not in route or tx == self.node_id:
            return
        heard_from = route.index(tx)
        if heard_from > index:
            # Someone after us already has it (a skipped hop): nothing to add.
            self._note_done(ident, heard_from)
            self.stats["overtaken"] += 1
            self._log(self._log_seen(plain, route, index, rssi, tx), "skipped", tx, rssi)
            return
        if len(self._flights) >= self.max_pending:
            self.stats["overflow"] += 1
            return
        relayed = seal(self._aead, plain, fcnt, self.node_id)
        wait = self._route_wait(len(relayed), index, heard_from)
        self._flights[ident] = self._routed_flight(
            relayed, plain, route, index, self.clock(), sent=0, wait=wait, rssi=rssi, tx=tx
        )
        self._follow_answer(ident, plain)

    def _on_routed_copy(
        self, ident: tuple[int, int], plain: bytes, fcnt: int, tx: int, rssi: Optional[float]
    ) -> None:
        """A routed frame heard again."""

        self.stats["duplicates"] += 1
        route = _route_of(plain)
        if route is None or self.node_id not in route or tx not in route:
            return
        index, heard_from = route.index(self.node_id), route.index(tx)
        record = self.route_log.get(_log_key(plain))
        if record is not None:
            self._log_prev(record, tx, rssi)  # the hop's own link, if this copy is from it
        flight = self._flights.get(ident)
        if heard_from > index:
            self._note_done(ident, heard_from)
            if flight is not None:
                del self._flights[ident]
                self.stats["hop_acked" if flight.sent else "overtaken"] += 1
                self._log(flight.log, "acked" if flight.sent else "skipped", tx, rssi)
            return
        if index == len(route) - 1 and self.last_hop_ack and _kind(plain) in _ECHOED:
            # The gateway: the node before us repeated, so it missed our
            # echo. Echo again (each repeat gets one, like any hop's answer).
            if heard_from < index:
                self._echo(ident, plain, fcnt, route)
            return
        if index == len(route) - 1:
            # The target. The last relay's copy releases a held answer; the
            # last relay sending it again means it missed our answer, which
            # goes again at once (a different frame: nobody takes it for a
            # retry of theirs).
            if heard_from == index - 1:
                request = _request_id(plain)
                release = self.clock() + self._rng.uniform(0.0, self.route_jitter)
                if request in self._answer_hold:
                    self._answer_hold[request] = release
                for flight in self._flights.values():
                    if flight.index == 0 and flight.route is not None and (flight.sent == 0 or flight.on_demand):
                        if _request_id(open_sealed(self._aead, flight.raw)[0]) == request:
                            flight.due = min(flight.due, release)
                            flight.demanded = flight.sent > 0
                            if flight.on_demand:
                                # The relay is still repeating: answer it for as
                                # long as it can (measured: a linger counted from
                                # when the answer was made ran out before the
                                # relay's repeats came, and a good hop broke).
                                flight.linger = max(flight.linger, release + flight.linger_span)
            return
        if heard_from == index or not self.forward:
            return  # our own copy echoed
        now = self.clock()
        wait = self._route_wait(len(plain) + 9, index, heard_from)
        if flight is not None:
            if not flight.sent:
                # A closer node's copy: our turn may come sooner than planned.
                flight.due = min(flight.due, now + wait)
            elif flight.confirm and self._ack_overdue(flight, route, now):
                # We missed our own acknowledgement, so we repeat anyway: do
                # it now, right behind the node that just repeated, which
                # listens now and takes ours for its acknowledgement. Left to
                # the two timers, the repeats fall on top of each other
                # (measured: they run about one airtime apart).
                flight.due = min(flight.due, now + self._rng.uniform(0.0, self.route_jitter))
            # Still inside the normal wait: our timer repeats the frame if it
            # must. Answering now as well would look, to the node after us,
            # like our own retry, and each node would set off the next
            # (measured: repeats cascading down the route).
            return
        done = self._done.get(ident)
        if done is None or done.answered or len(self._flights) >= self.max_pending:
            return
        if done.furthest >= len(route):
            # Another copy of the same (SENDER, FCNT) with a shorter route: a node
            # that sealed two frames under one counter. Dropped.
            self.stats["bad_route"] += 1
            return
        done.answered = True
        # The copy of the furthest node we know has it: an acknowledgement for
        # the node behind us that nobody further on mistakes for a retry. A
        # copy from anyone after us, heard while we wait, cancels ours.
        self._flights[ident] = _Flight(
            seal(self._aead, plain, fcnt, route[done.furthest]),
            now + wait,
            suppressible=False,
            route=route,
            index=index,
            answer=True,
            log=_log_key(plain),
        )

    def _echo(self, ident: tuple[int, int], plain: bytes, fcnt: int, route: tuple[int, ...]) -> None:
        """We are the gateway: the inbound frame goes back once (TX = us), its last hop's acknowledgement.

        Sent at once, before the frame goes up (``_on_radio``): what it
        carries may move the gateway to another radio setting (a session's
        answer, its closing keepalive's), and the last relay is still on
        this one until it hears the echo (measured: an echo queued behind
        the move went out on SF7 to a path at SF9, which repeated its
        answer until it gave up and came home late).
        """

        self.stats["echoed"] += 1
        self._echoes.append(seal(self._aead, plain, fcnt, self.node_id))

    def _ack_overdue(self, flight: _Flight, route: tuple[int, ...], now: float) -> bool:
        remaining = len(route) - 1 - flight.index
        if remaining <= 3 and flight.request is not None:
            return False  # near the target the answer is the acknowledgement
        air = self.airtime(len(flight.raw))
        return now >= flight.sent_at + 2 * air + 2 * self.route_jitter + self.route_slack

    # ------------------------------------------------ traces (lock held)

    def _note_trace(self, plain: bytes) -> bool:
        """Remember a TRACE passing by; ``False`` (``bad_route``) for a malformed one."""

        base, _ = split_route(plain)
        try:
            frame = TraceFrame.from_bytes(base)
        except ValueError:
            self.stats["bad_route"] += 1
            return False
        key = (frame.sender_id, frame.message_id)
        self._traces[key] = (frame.query_sequence, frame.query_epoch, frame.target_id)
        self._traces.move_to_end(key)
        while len(self._traces) > 2:
            self._traces.popitem(last=False)
        return True

    def _record_for(self, key: tuple[int, int]) -> TraceRecord:
        query = self._traces.get(key)
        if query is None:
            return TraceRecord()  # the TRACE never came past us: nothing to say
        sequence, epoch, target = query
        return node_report(self.route_log, key[0], target, sequence, epoch)

    def _queue_trace(self, key: tuple[int, int], reply: TraceReplyFrame, rest: tuple[int, ...]) -> None:
        now = self.clock()
        self._trace_out[key] = _TraceOut(
            add_route(reply.encode(), rest),
            len(reply.records),
            now + self._rng.uniform(0.0, self.route_jitter),
            blind=not rest,
            expires=now + self.probe_ttl,
        )
        while len(self._trace_out) > 2:
            del self._trace_out[min(self._trace_out, key=lambda k: self._trace_out[k].expires)]

    def _answer_trace(self, plain: bytes, route: tuple[int, ...]) -> None:
        """We are the trace's target: start the answer with our own record."""

        base, relays = split_route(plain)
        try:
            frame = TraceFrame.from_bytes(base)
        except ValueError:
            self.stats["bad_route"] += 1
            return
        key = (frame.sender_id, frame.message_id)
        if key in self._trace_out:
            return  # a repeat of the trace: our answer is on its way
        reply = TraceReplyFrame(
            self.node_id, frame.sender_id, frame.sequence, (self._record_for(key),), frame.epoch
        )
        self.stats["traces_answered"] += 1
        self._queue_trace(key, reply, reply_route(relays))

    def _on_trace_reply(self, plain: bytes) -> None:
        try:
            frame, rest = decode_routed(plain)
        except ValueError:
            self.stats["bad_route"] += 1
            return
        key = (frame.target_id, frame.message_id)
        mine = self._trace_out.get(key)
        if mine is not None and len(frame.records) > mine.records:
            mine.acked = True  # the next node has it, with its record added
        elif mine is not None and len(frame.records) < mine.records and mine.acked and mine.raw is not None:
            mine.echo = True
            mine.due = self.clock() + self._rng.uniform(0.0, self.route_jitter)
        query = self._traces.get(key)
        if query is not None:
            # The trace's answer: every hop of the TRACE is done.
            self._on_reply((frame.target_id, query[2], frame.sequence, frame.epoch))
        if not self.forward or not rest or rest[0] != self.node_id or key in self._trace_out:
            return
        reply = TraceReplyFrame(
            self.node_id,
            frame.target_id,
            frame.sequence,
            frame.records + (self._record_for(key),),
            frame.epoch,
        )
        self.stats["traces_relayed"] += 1
        self._queue_trace(key, reply, rest[1:])

    # ------------------------------------------- route log (lock held)

    def _log_seen(
        self,
        plain: bytes,
        route: tuple[int, ...],
        index: int,
        rssi: Optional[float],
        tx: Optional[int] = None,
    ) -> tuple[int, int, int, int, int]:
        key = _log_key(plain)
        snr = None if rssi is None else self._link(rssi).snr_db
        record = self.route_log.get(key)
        if record is None:
            kind, sender, target, sequence, epoch = key
            record = HopRecord(kind, sender, target, sequence, epoch, route, index, rssi, snr)
            self.route_log[key] = record
            while len(self.route_log) > self.max_route_log:
                self.route_log.popitem(last=False)
        else:
            # The same message again (an end-to-end retry, a fresh FCNT).
            record.attempts += 1
            record.route, record.index, record.heard_rssi, record.heard_snr = route, index, rssi, snr
            record.prev_rssi = record.prev_snr = None
            record.sends, record.state, record.ack_from, record.ack_rssi = 0, "pending", None, None
            self.route_log.move_to_end(key)
        record.blind_last = (
            record.kind in _INBOUND and index == len(route) - 2 and not (self.last_hop_ack and record.kind in _ECHOED)
        )
        self._log_prev(record, tx, rssi)
        self._route_note(record, "seen")
        return key

    def _log_prev(self, record: HopRecord, tx: Optional[int], rssi: Optional[float]) -> None:
        if rssi is None or record.prev_rssi is not None or record.index == 0:
            return
        if tx == record.route[record.index - 1]:
            record.prev_rssi, record.prev_snr = rssi, self._link(rssi).snr_db

    def _log(
        self,
        key: Optional[tuple[int, int, int, int, int]],
        event: str,
        ack_from: Optional[int] = None,
        ack_rssi: Optional[float] = None,
        waits: bool = True,
    ) -> None:
        record = None if key is None else self.route_log.get(key)
        if record is None:
            return
        if event == "sent":
            record.sends += 1
            if not waits and record.state == "pending":
                record.state = "sent"
        elif record.state in ("pending", "sent"):
            record.state = event
            if ack_from is not None:
                record.ack_from, record.ack_rssi = ack_from, ack_rssi
        else:
            return
        self._route_note(record, event)

    def _route_note(self, record: HopRecord, event: str) -> None:
        if self.route_event is not None:
            self.route_event(record, event)

    def _note_done(self, ident: tuple[int, int], furthest: int) -> None:
        done = self._done.get(ident)
        if done is None:
            self._done[ident] = _Done(furthest)
            while len(self._done) > self._max_done:
                self._done.popitem(last=False)
        else:
            done.furthest = max(done.furthest, furthest)

    def _route_broken(self, flight: _Flight) -> None:
        """Called outside the lock: a routed hop stayed unacknowledged."""

        plain, _ = open_sealed(self._aead, flight.raw)
        route = flight.route
        broken = route[flight.index + 1]
        with self._lock:
            self._log(flight.log, "gave_up")
        if flight.index == 0:
            self.stats["route_dropped"] += 1
            if self.route_failed is not None:
                self.route_failed(plain, broken)
            return
        if _kind(plain) in _INBOUND:
            self.stats["route_dropped"] += 1  # the gateway's end-to-end timer covers it
            return
        if self.sealer is None:
            self.stats["unsealed"] += 1
            return
        error = RouteErrorFrame(
            sender_id=self.node_id,
            target_id=route[0],
            sequence=plain[3],
            broken=broken,
            epoch=plain[4],
        )
        back = reply_route(route[1 : flight.index])
        self.stats["route_errors"] += 1
        self._originate(self.sealer(add_route(error.encode(), back)))

    # ---------------------------------------------------------- probes (lock held)

    def _link(self, rssi: Optional[float]) -> LinkQuality:
        if rssi is None:
            rssi = self.rssi_range[0]
        return LinkQuality.measured(rssi, rssi - self.noise_floor)

    def _probe_delay(self, links: tuple[LinkQuality, ...]) -> float:
        low, high = self.probe_backoff
        if self.probe_delay == "cost":
            # A5: wait by what the hop just crossed costs (ETX above 1, up to
            # ``probe_cost_span``), so copies reach every relay in the order of
            # their path's cost and each relay passes on the cheapest one. By
            # the old rule (worst link so far) a copy that skipped hops over a
            # weak link arrived first and every relay committed to it
            # (measured on a planned 17-hop line: 6–9 hop routes over
            # 0–3 dB links, broken again and again).
            last = links[-1]
            cost = self._hop_etx(last.snr_db)
            share = min(1.0, max(0.0, (cost - 1.0) / self.probe_cost_span))
            return low + (high - low) * (0.8 * share + 0.2 * self._rng.random())
        weak = self.probe_weak_snr
        strong = self.probe_good_snr  # good enough is as fast as it gets
        worst = min(link.snr_db for link in links)
        quality = min(1.0, max(0.0, (worst - weak) / (strong - weak)))
        # Good paths go first, so they reach the target first; chance keeps
        # equally good relays from colliding (with only 20 % of the window
        # random, two relays at the same depth collided about half the time).
        return low + (high - low) * (0.6 * (1.0 - quality) + 0.4 * self._rng.random())

    def _on_probe(self, plain: bytes, rssi: Optional[float]) -> None:
        try:
            frame = _PATH_FRAMES[plain[0]].from_bytes(plain)
        except ValueError:
            self.stats["probes_ignored"] += 1
            return
        key = _probe_key(frame)
        state = self._probes.get(key)
        if any(hop.node_id == self.node_id for hop in frame.hops) or self.node_id == frame.origin_id:
            if state is not None:
                state.acked = True  # someone further out carries our copy
            return
        if frame.wake and state is None and self.wake_gate is not None:
            if not self.wake_gate(plain):
                self.stats["wakes_refused"] += 1
                return
        now = self.clock()
        link = self._link(rssi)
        # A DISCOVER to everyone: every node is a target (it keeps the best
        # path to answer along) and a relay (it passes the scan on) at once.
        everyone = frame.kind == FrameKind.DISCOVER and frame.target_id == BROADCAST_ID
        if self.node_id == frame.target_id or everyone:
            links = tuple(hop.link for hop in frame.hops) + (link,)
            ckey = (*key, 1) if everyone else key
            collect = self._probes.get(ckey)
            if collect is None:
                self._keep_probe(ckey, _Probe(frame, links, now + frame.window_s, now + self.probe_ttl, True))
            elif not collect.sent and self._better(links, collect.links):
                collect.frame, collect.links = frame, links
            if not everyone:
                return
        if not self.forward or frame.full:
            self.stats["probes_ignored"] += 1
            return
        extended = frame.extended(self.node_id, link)
        links = tuple(hop.link for hop in extended.hops)
        due = now + self._probe_delay(links)
        if state is None:
            self._keep_probe(key, _Probe(extended, links, due, now + self.probe_ttl, False))
        elif not state.sent and self._better(links, state.links):
            state.frame, state.links, state.due = extended, links, min(state.due, due)

    def _on_routed_probe(self, plain: bytes, rssi: Optional[float]) -> None:
        """A probe along a route (task C5): only the next listed relay passes it on (lock held)."""

        try:
            base, relays = split_route(plain)
            frame = ProbeFrame.from_bytes(base)
        except ValueError:
            self.stats["probes_ignored"] += 1
            return
        key = _probe_key(frame)
        state = self._probes.get(key)
        if any(hop.node_id == self.node_id for hop in frame.hops) or self.node_id == frame.origin_id:
            if state is not None:
                state.acked = True  # the next relay carries our copy
            return
        now = self.clock()
        link = self._link(rssi)
        if self.node_id == frame.target_id:
            if state is None:
                links = tuple(hop.link for hop in frame.hops) + (link,)
                self._keep_probe(key, _Probe(frame, links, now + frame.window_s, now + self.probe_ttl, True))
            return
        if not relays or relays[0] != self.node_id or not self.forward or frame.full:
            self.stats["not_on_route"] += 1
            return
        if state is not None:
            return  # a repeat from behind: ours is out or due
        extended = frame.extended(self.node_id, link)
        links = tuple(hop.link for hop in extended.hops)
        due = now + self.probe_backoff[0] + self._rng.uniform(0.0, self.route_jitter)
        self._keep_probe(key, _Probe(extended, links, due, now + self.probe_ttl, False, route=relays[1:]))

    def _better(self, links: tuple[LinkQuality, ...], than: tuple[LinkQuality, ...]) -> bool:
        if self.probe_metric == "etx":
            return self.path_etx(links) < self.path_etx(than)
        return self._score_widest(links) > self._score_widest(than)

    def _score_widest(self, links: tuple[LinkQuality, ...]) -> tuple[int, int, int]:
        return _score(links, round(self.probe_good_snr * 4))

    def path_etx(self, links: tuple[LinkQuality, ...]) -> float:
        """Expected sends to cross ``links`` and back (lower is better)."""

        return sum(self._hop_etx(link.snr_db) for link in links)

    def _hop_etx(self, snr_db: float) -> float:
        return _link_etx(
            snr_db, self.probe_weak_snr, self.etx_sigma, self.etx_safety,
            self.etx_break, self.route_retries + 1,
        )

    def _keep_probe(self, key: tuple[int, int], probe: _Probe) -> None:
        if len(self._probes) >= self.max_probes:
            # Forget the oldest: one already passed on if possible.
            victims = sorted(self._probes.items(), key=lambda item: (not item[1].sent, item[1].expires))
            del self._probes[victims[0][0]]
        self._probes[key] = probe

    def _pass_trace(self, out: _TraceOut) -> None:
        """Called outside the lock: send a trace reply we appended to (sealed once, as our own)."""

        if out.raw is None:
            if self.sealer is None:
                self.stats["unsealed"] += 1
                return
            out.raw = self.sealer(out.plain)
            _, fcnt = open_sealed(self._aead, out.raw)
            with self._lock:
                self._window(self.node_id).accept(fcnt)
        self.radio.send(out.raw)

    def _pass_probe(self, probe: _Probe) -> None:
        """Called outside the lock: forward a probe, or answer it as its target."""

        if self.sealer is None:
            self.stats["unsealed"] += 1
            return
        frame = probe.frame
        if not probe.target:
            self.stats["probes_relayed"] += 1
            plain = frame.encode()
            if probe.route is not None:
                try:
                    plain = add_route(plain, probe.route)
                except ValueError:
                    self.stats["bad_route"] += 1
                    return
            self._originate(self.sealer(plain))
            return
        if frame.kind != FrameKind.PROBE:
            # JOIN at the gateway, DISCOVER at a node: the layer above answers.
            if self.path_found is not None:
                self.path_found(frame, probe.links)
            return
        answer = ProbeReplyFrame(
            sender_id=self.node_id,
            target_id=frame.origin_id,
            sequence=frame.sequence,
            links=probe.links,
            epoch=frame.epoch,
        )
        back = reply_route(hop.node_id for hop in frame.hops)
        try:
            plain = add_route(answer.encode(), back)
        except ValueError:
            # The hop list names us, the origin or broadcast: no way back.
            self.stats["bad_route"] += 1
            return
        self.stats["probes_answered"] += 1
        self._originate(self.sealer(plain))

    def _reject(self, counter: str, reason: str, raw: bytes) -> None:
        self.stats[counter] += 1
        if self._on_reject is not None:
            self._on_reject(reason, bytes(raw))
