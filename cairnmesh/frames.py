"""Frame definitions of the CairnMesh link protocol (the wire format; ``protocol_spec.md``).

Each frame starts with a one-byte ``FrameKind`` value, followed by
fixed-width unsigned byte fields:

    WAKE       KIND | SENDER_ID | TARGET_ID | SEQUENCE | EPOCH                    5 B
    REQUEST    KIND | SENDER_ID | TARGET_ID | SEQUENCE | EPOCH | ACTION | VALUE   7 B
    REPLY      KIND | SENDER_ID | TARGET_ID | SEQUENCE | EPOCH | RESULT           6 B
    HEARTBEAT  KIND | SENDER_ID | TARGET_ID | SEQUENCE | STATE                    5 B
    HELLO      KIND | SENDER_ID | TARGET_ID | SEQUENCE | STATE                    5 B
    REPORT     KIND | SENDER_ID | TARGET_ID | SEQUENCE | EPOCH | SUBJECT | EVENT  7 B
    RESULT     KIND | SENDER_ID | TARGET_ID | SEQUENCE | EPOCH | RESULT           6 B
    EVENT      KIND | SENDER_ID | TARGET_ID | SEQUENCE | EPOCH | EVENT_ID         6 B

HELLO is a one-hop beacon that relays never forward; REPORT tells the
controller that a neighbour (SUBJECT) was lost or came back, and is
acknowledged with a REPLY like a request (see ``liveness.py``). EVENT (wire
format v4) tells it that a module on the device raised an event whose id the
gateway defined, queued until the network is awake (``uplink.py``) and
acknowledged the same way.

``EPOCH`` and ``SEQUENCE`` together are the 16-bit message id. ``EPOCH``
names one run of the controller and ``SEQUENCE`` one message in that run, so
a restarted controller never reuses the id of a message the device still
remembers. A REPLY echoes both bytes of the frame it answers.

Routed sessions add six kinds and a route trailer (``protocol_spec.md``
section 11):

    KEEPALIVE    KIND | SENDER | TARGET | SEQ | EPOCH | LEASE | RADIO                 7 B
    PROBE        KIND | SENDER | TARGET | SEQ | EPOCH | ORIGIN | WINDOW | hop * K      7 + 3K B
                 (WINDOW top bit: WAKE, a directed wake-up probe, task D3)
    PROBE_REPLY  KIND | SENDER | TARGET | SEQ | EPOCH | link * (K + 1)                 5 + 2(K+1) B
    ROUTE_ERROR  KIND | SENDER | TARGET | SEQ | EPOCH | BROKEN                        6 B
    TRACE        KIND | SENDER | TARGET | SEQ | EPOCH | Q_SEQ | Q_EPOCH               7 B
    TRACE_REPLY  KIND | SENDER | TARGET | SEQ | EPOCH | record * k                    5 + 4k B

    hop    = NODE | -RSSI (dBm) | SNR (int8, 0.25 dB)
    RADIO  = CHANNEL (high 5 bits) | SF − 7 (low 3 bits): what the session runs on
             once the path has answered this keepalive (0: the common setting)
    link   = -RSSI (dBm) | SNR (int8, 0.25 dB)
    record = -RSSI out | SNR out | -RSSI back | repeats/outcomes (2 bits each)

Node discovery (task C) adds three kinds; JOIN and DISCOVER are floods laid
out and relayed like PROBE:

    JOIN            KIND | SENDER | GATEWAY | SEQ | EPOCH | ORIGIN | WINDOW | hop * K            7 + 3K B
    DISCOVER        KIND | SENDER | TARGET  | SEQ | EPOCH | ORIGIN | WINDOW | SPREAD | hop * K   8 + 3K B
    DISCOVER_REPLY  KIND | SENDER | TARGET  | SEQ | EPOCH | STATE | link * (K + 1) | hop * M     6 + 2(K+1) + 3M B

A DISCOVER_REPLY always carries a route (K relays), which is what tells its
links from its neighbours (M <= 3).

Waking a dormant network (task D2, ``wake.py``) adds two floods, relayed
unchanged like a REQUEST (same SENDER and FCNT, TX = the relay):

    WAKE_NET   KIND | SENDER | 0xFF | SEQ | EPOCH | LEASE   6 B
    SLEEP_NET  KIND | SENDER | 0xFF | SEQ | EPOCH           5 B

LEASE is how many seconds a node stays awake after hearing it; WAKE_NET goes
out with a long preamble (a physical-layer setting, not in the frame) so
sampling radios notice it. (The draft's DEPTH byte, one more per hop, was
dropped: a relay that altered the frame would have to seal a new one, see
``secure.py``.)

A frame whose KIND has ``ROUTED`` (0x40) set carries ``R1 .. RN | RINFO`` after
its fields: the relays in transmission order from SENDER to TARGET, and
``RINFO`` = format (high 3 bits, 0) | N (low 5 bits, at most 16). Only
REQUEST, REPLY, WAKE and RESULT may travel either way; KEEPALIVE, PROBE_REPLY,
ROUTE_ERROR, TRACE, TRACE_REPLY and DISCOVER_REPLY always carry a route. A PROBE is a flood,
or (task C5) follows a route the gateway picked from its node book: then,
like a TRACE_REPLY, it is sealed again by every node it passes, which
appends itself: SENDER is the last transmitter and the route lists only the
relays still to come.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import IntEnum
from typing import Iterable, Optional, Union


class FrameKind(IntEnum):
    """Values stored in the first byte of every frame."""

    REQUEST = 0x01
    REPLY = 0x02
    WAKE = 0x03
    HEARTBEAT = 0x04
    HELLO = 0x05
    REPORT = 0x06
    KEEPALIVE = 0x07
    PROBE = 0x08
    PROBE_REPLY = 0x09
    ROUTE_ERROR = 0x0A
    TRACE = 0x0B
    TRACE_REPLY = 0x0C
    JOIN = 0x0D
    DISCOVER = 0x0E
    DISCOVER_REPLY = 0x0F
    WAKE_NET = 0x10
    SLEEP_NET = 0x11
    #: An action's final result, sent by the device after it answered ACCEPTED (2026-10-21).
    RESULT = 0x12
    #: A module on the device raised an event the gateway defined (wire format v4, 2026-10-22).
    EVENT = 0x13


#: KIND flag: the frame ends with a route trailer (``R1 .. RN | RINFO``).
ROUTED = 0x40
#: KIND flag (task Q14): the payload is sealed end to end under the node's
#: application key (``secure.AppKeys``). Only below the security layer: the
#: frames above it (and their encoders here) never carry it.
APP = 0x20
#: Kinds that carry an application payload, and its plain length.
APP_KINDS = {
    FrameKind.REQUEST: 7,
    FrameKind.REPLY: 6,
    FrameKind.REPORT: 7,
    FrameKind.RESULT: 6,
    FrameKind.EVENT: 6,
}
#: 17 transmissions from gateway to target at most: 16 relays plus the target.
MAX_RELAYS = 16
#: Kinds that may carry a route.
ROUTABLE = frozenset(
    {
        FrameKind.REQUEST,
        FrameKind.REPLY,
        FrameKind.WAKE,
        FrameKind.KEEPALIVE,
        FrameKind.PROBE,
        FrameKind.PROBE_REPLY,
        FrameKind.ROUTE_ERROR,
        FrameKind.TRACE,
        FrameKind.TRACE_REPLY,
        FrameKind.DISCOVER_REPLY,
        FrameKind.RESULT,
    }
)
#: Kinds that never travel without one.
ROUTED_ONLY = frozenset(
    {
        FrameKind.KEEPALIVE,
        FrameKind.PROBE_REPLY,
        FrameKind.ROUTE_ERROR,
        FrameKind.TRACE,
        FrameKind.TRACE_REPLY,
        FrameKind.DISCOVER_REPLY,
    }
)
#: Floods every relay extends with itself and the link it heard, and seals
#: again as its own frame (task C reuses the PROBE layout and rules).
PATH_KINDS = frozenset({FrameKind.PROBE, FrameKind.JOIN, FrameKind.DISCOVER})
#: Seconds per unit of a DISCOVER's SPREAD byte (answers are spread over it).
DISCOVER_SPREAD_UNIT = 0.25
#: Neighbours a DISCOVER_REPLY lists at most.
MAX_NEIGHBOURS = 3
#: How a node's ``TraceRecord`` says a message fared there, each way.
TRACE_UNSEEN, TRACE_DONE, TRACE_GAVE_UP, TRACE_OTHER = 0, 1, 2, 3
#: Seconds per unit of a PROBE's WINDOW byte.
PROBE_WINDOW_UNIT = 0.05
#: A PROBE's WINDOW top bit: a directed wake-up probe (task D3). Its copies
#: go out with the long preamble, and the window is the low 7 bits (≤ 6.35 s).
PROBE_WAKE = 0x80


class ReplyResult(IntEnum):
    """Outcome values carried by a reply frame."""

    OK = 0x00
    ERROR = 0x01
    #: Answer to a ``QUERY_ACTION`` request: the asked request never ran and,
    #: from now on, never will (a late copy is answered, not run).
    NOT_RUN = 0x02
    #: The device logged the request and runs it (2026-10-21): it will not run
    #: it twice and reports the outcome in a RESULT frame once the action is
    #: over. A query is answered ACCEPTED while the action still runs.
    ACCEPTED = 0x03
    #: The device reset while the action was open (2026-11-08, wire format v5):
    #: no final result exists. The action may have completed or not; the device
    #: applied the safe state its action table registers (none: none needed)
    #: before answering this; it will not run again. Not ``ERROR`` (the action did
    #: not fail by its own account) and never "not done".
    INTERRUPTED = 0x04


#: A REQUEST with this ACTION is a status query (Q18): it asks for the result
#: of the request whose SEQUENCE is in VALUE (same SENDER and EPOCH) and runs
#: nothing. Applications must not use it as an action.
QUERY_ACTION = 0xFF


class NodeState(IntEnum):
    """Device state reported in every heartbeat."""

    SLEEP = 0x00
    READY = 0x01


class ReportEvent(IntEnum):
    """What a REPORT says about its SUBJECT."""

    LOST = 0x00
    RECOVERED = 0x01


DEFAULT_CONTROLLER_ID = 0x42
#: Only heartbeats may target it: a reliable request needs exactly one replier.
BROADCAST_ID = 0xFF


def _check_bytes(frame: object, *names: str) -> None:
    for name in names:
        value = getattr(frame, name)
        # bool is an int subclass, but True/False are never valid field values.
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError(f"{name} must be an int, got {type(value).__name__}")
        if not 0 <= value <= 0xFF:
            raise ValueError(f"{name} out of range 0..255: {value}")


def _message_id(frame) -> int:
    # EPOCH is the high byte: ids of one run sort together.
    return frame.epoch << 8 | frame.sequence


def _check_layout(data: bytes, kind: FrameKind, length: int) -> None:
    if len(data) != length:
        raise ValueError(f"{kind.name.lower()} frame must be exactly {length} bytes")
    if data[0] != kind:
        raise ValueError(f"frame kind is not {kind.name}")


@dataclass(frozen=True)
class LinkQuality:
    """One received link as the radio measured it.

    ``rssi`` in whole dBm (-255 .. 0), ``snr`` in quarter dB (-128 .. 127, the
    SX1262's own packet SNR unit). Links are taken as symmetric: a probe
    measures the outward direction only.
    """

    rssi: int
    snr: int

    def __post_init__(self) -> None:
        if not isinstance(self.rssi, int) or not -255 <= self.rssi <= 0:
            raise ValueError(f"rssi out of range -255..0 dBm: {self.rssi!r}")
        if not isinstance(self.snr, int) or not -128 <= self.snr <= 127:
            raise ValueError(f"snr out of range -128..127 quarter dB: {self.snr!r}")

    @classmethod
    def measured(cls, rssi_dbm: float, snr_db: float) -> "LinkQuality":
        """Round and clamp radio readings into the wire range."""

        rssi = min(0, max(-255, round(rssi_dbm)))
        snr = min(127, max(-128, round(snr_db * 4)))
        return cls(rssi, snr)

    @property
    def snr_db(self) -> float:
        return self.snr / 4

    def encode(self) -> bytes:
        return bytes([-self.rssi, self.snr & 0xFF])

    @classmethod
    def from_bytes(cls, data: bytes) -> "LinkQuality":
        snr = data[1] - 0x100 if data[1] & 0x80 else data[1]
        return cls(-data[0], snr)


@dataclass(frozen=True)
class ProbeHop:
    """A relay a PROBE passed, with the link it arrived over."""

    node_id: int
    link: LinkQuality

    def __post_init__(self) -> None:
        _check_bytes(self, "node_id")


def check_route(relays: Iterable[int], sender_id: int, target_id: int) -> tuple[int, ...]:
    """Validate a relay list (transmission order, SENDER and TARGET excluded)."""

    route = tuple(relays)
    if len(route) > MAX_RELAYS:
        raise ValueError(f"a route holds at most {MAX_RELAYS} relays, got {len(route)}")
    for node in route:
        if not isinstance(node, int) or isinstance(node, bool) or not 0 <= node <= 0xFF:
            raise ValueError(f"relay id out of range 0..255: {node!r}")
        if node in (sender_id, target_id, BROADCAST_ID):
            raise ValueError(f"relay {node:#04x} may not be the sender, the target or broadcast")
    if len(set(route)) != len(route):
        raise ValueError("a route may not visit a relay twice")
    return route


@dataclass
class WakeFrame:
    """Switch a sleeping device to ``READY``."""

    sender_id: int
    target_id: int
    sequence: int
    epoch: int = 0
    kind: FrameKind = field(default=FrameKind.WAKE, init=False)

    def __post_init__(self) -> None:
        _check_bytes(self, "sender_id", "target_id", "sequence", "epoch")

    @property
    def message_id(self) -> int:
        return _message_id(self)

    def encode(self) -> bytes:
        return bytes([self.kind, self.sender_id, self.target_id, self.sequence, self.epoch])

    @classmethod
    def from_bytes(cls, data: bytes) -> "WakeFrame":
        _check_layout(data, FrameKind.WAKE, 5)
        return cls(sender_id=data[1], target_id=data[2], sequence=data[3], epoch=data[4])


@dataclass
class RequestFrame:
    """Ask a device to perform ``action`` with ``value``."""

    sender_id: int
    target_id: int
    sequence: int
    action: int
    value: int
    epoch: int = 0
    kind: FrameKind = field(default=FrameKind.REQUEST, init=False)

    def __post_init__(self) -> None:
        _check_bytes(self, "sender_id", "target_id", "sequence", "epoch", "action", "value")

    @property
    def message_id(self) -> int:
        return _message_id(self)

    def encode(self) -> bytes:
        return bytes(
            [
                self.kind,
                self.sender_id,
                self.target_id,
                self.sequence,
                self.epoch,
                self.action,
                self.value,
            ]
        )

    @classmethod
    def from_bytes(cls, data: bytes) -> "RequestFrame":
        _check_layout(data, FrameKind.REQUEST, 7)
        return cls(
            sender_id=data[1],
            target_id=data[2],
            sequence=data[3],
            epoch=data[4],
            action=data[5],
            value=data[6],
        )


@dataclass
class ReplyFrame:
    """Answer to a WAKE, REQUEST, KEEPALIVE or JOIN, or the acknowledgement of a REPORT, RESULT or EVENT; ``sequence`` and ``epoch`` echo the frame answered."""

    sender_id: int
    target_id: int
    sequence: int
    result: int
    epoch: int = 0
    kind: FrameKind = field(default=FrameKind.REPLY, init=False)

    def __post_init__(self) -> None:
        _check_bytes(self, "sender_id", "target_id", "sequence", "epoch", "result")

    @property
    def message_id(self) -> int:
        return _message_id(self)

    def encode(self) -> bytes:
        return bytes(
            [self.kind, self.sender_id, self.target_id, self.sequence, self.epoch, self.result]
        )

    @classmethod
    def from_bytes(cls, data: bytes) -> "ReplyFrame":
        _check_layout(data, FrameKind.REPLY, 6)
        return cls(
            sender_id=data[1],
            target_id=data[2],
            sequence=data[3],
            epoch=data[4],
            result=data[5],
        )


@dataclass
class ResultFrame:
    """An action's final result, after the device answered its REQUEST ACCEPTED (2026-10-21).

    ``sequence`` and ``epoch`` name the request (the controller's message
    id), so the controller matches and deduplicates it without a field of
    its own. Acknowledged by a REPLY from the controller echoing them (RESULT
    OK); the device sends it again until one comes.
    """

    sender_id: int
    target_id: int
    sequence: int
    result: int
    epoch: int = 0
    kind: FrameKind = field(default=FrameKind.RESULT, init=False)

    def __post_init__(self) -> None:
        _check_bytes(self, "sender_id", "target_id", "sequence", "epoch", "result")

    @property
    def message_id(self) -> int:
        return _message_id(self)

    def encode(self) -> bytes:
        return bytes(
            [self.kind, self.sender_id, self.target_id, self.sequence, self.epoch, self.result]
        )

    @classmethod
    def from_bytes(cls, data: bytes) -> "ResultFrame":
        _check_layout(data, FrameKind.RESULT, 6)
        return cls(
            sender_id=data[1],
            target_id=data[2],
            sequence=data[3],
            epoch=data[4],
            result=data[5],
        )


@dataclass
class HeartbeatFrame:
    """Sent periodically by every device.

    ``sequence`` is the device's own heartbeat counter, independent of
    request sequences, so the receiver can count missed heartbeats.
    """

    sender_id: int
    target_id: int
    sequence: int
    state: NodeState
    kind: FrameKind = field(default=FrameKind.HEARTBEAT, init=False)

    def __post_init__(self) -> None:
        _check_bytes(self, "sender_id", "target_id", "sequence")
        try:
            self.state = NodeState(self.state)
        except ValueError:
            raise ValueError(f"unknown node state: {self.state!r}") from None

    def encode(self) -> bytes:
        return bytes(
            [self.kind, self.sender_id, self.target_id, self.sequence, self.state]
        )

    @classmethod
    def from_bytes(cls, data: bytes) -> "HeartbeatFrame":
        _check_layout(data, FrameKind.HEARTBEAT, 5)
        return cls(
            sender_id=data[1],
            target_id=data[2],
            sequence=data[3],
            state=data[4],
        )


@dataclass
class HelloFrame:
    """One-hop beacon: neighbours learn who is around; never relayed."""

    sender_id: int
    sequence: int
    state: NodeState
    target_id: int = BROADCAST_ID
    kind: FrameKind = field(default=FrameKind.HELLO, init=False)

    def __post_init__(self) -> None:
        _check_bytes(self, "sender_id", "target_id", "sequence")
        try:
            self.state = NodeState(self.state)
        except ValueError:
            raise ValueError(f"unknown node state: {self.state!r}") from None

    def encode(self) -> bytes:
        return bytes([self.kind, self.sender_id, self.target_id, self.sequence, self.state])

    @classmethod
    def from_bytes(cls, data: bytes) -> "HelloFrame":
        _check_layout(data, FrameKind.HELLO, 5)
        return cls(sender_id=data[1], target_id=data[2], sequence=data[3], state=data[4])


@dataclass
class ReportFrame:
    """A node tells the controller that ``subject`` was lost or came back.

    Acknowledged by a REPLY echoing SEQUENCE and EPOCH, like a request; the
    reporter's EPOCH names its own run.
    """

    sender_id: int
    target_id: int
    sequence: int
    subject: int
    event: ReportEvent
    epoch: int = 0
    kind: FrameKind = field(default=FrameKind.REPORT, init=False)

    def __post_init__(self) -> None:
        _check_bytes(self, "sender_id", "target_id", "sequence", "epoch", "subject")
        try:
            self.event = ReportEvent(self.event)
        except ValueError:
            raise ValueError(f"unknown report event: {self.event!r}") from None

    @property
    def message_id(self) -> int:
        return _message_id(self)

    def encode(self) -> bytes:
        return bytes(
            [
                self.kind,
                self.sender_id,
                self.target_id,
                self.sequence,
                self.epoch,
                self.subject,
                self.event,
            ]
        )

    @classmethod
    def from_bytes(cls, data: bytes) -> "ReportFrame":
        _check_layout(data, FrameKind.REPORT, 7)
        return cls(
            sender_id=data[1],
            target_id=data[2],
            sequence=data[3],
            epoch=data[4],
            subject=data[5],
            event=data[6],
        )


@dataclass
class EventFrame:
    """A module on the device raised the event ``event_id`` (wire format v4, 2026-10-22).

    The gateway defines what each id means (user: an id, no reading). Like a
    REPORT: the message id is the device's own (its EPOCH names its run,
    SEQUENCE comes from the counter it shares with REPORT), flooded to the
    controller and acknowledged by a network-only REPLY echoing it; the
    device sends it again until one comes.
    """

    sender_id: int
    target_id: int
    sequence: int
    event_id: int
    epoch: int = 0
    kind: FrameKind = field(default=FrameKind.EVENT, init=False)

    def __post_init__(self) -> None:
        _check_bytes(self, "sender_id", "target_id", "sequence", "epoch", "event_id")

    @property
    def message_id(self) -> int:
        return _message_id(self)

    def encode(self) -> bytes:
        return bytes(
            [self.kind, self.sender_id, self.target_id, self.sequence, self.epoch, self.event_id]
        )

    @classmethod
    def from_bytes(cls, data: bytes) -> "EventFrame":
        _check_layout(data, FrameKind.EVENT, 6)
        return cls(
            sender_id=data[1],
            target_id=data[2],
            sequence=data[3],
            epoch=data[4],
            event_id=data[5],
        )


def radio_byte(channel: int = 0, sf: int = 7) -> int:
    """A KEEPALIVE's RADIO byte (task Q11 / Q12)."""

    if not 0 <= channel < 32 or not 7 <= sf <= 12:
        raise ValueError(f"channel 0..31 and SF 7..12, got {channel}, {sf}")
    return channel << 3 | (sf - 7)


def radio_of(byte: int) -> tuple[int, int]:
    """``(channel, sf)`` of a RADIO byte."""

    sf = 7 + (byte & 0x07)
    if sf > 12:
        raise ValueError(f"bad SF in radio byte: {byte:#04x}")
    return byte >> 3, sf


@dataclass
class KeepaliveFrame:
    """Keeps a routed session alive; answered by a REPLY like a WAKE.

    ``lease`` is how many more seconds the session lasts; 0 closes it.
    ``radio`` (``radio_byte``) is the channel and SF the session's path
    moves to once it has passed this keepalive's answer on (0: the common
    setting). Idempotent, so devices do not put it in their duplicate cache.
    """

    sender_id: int
    target_id: int
    sequence: int
    lease: int
    epoch: int = 0
    radio: int = 0
    kind: FrameKind = field(default=FrameKind.KEEPALIVE, init=False)

    def __post_init__(self) -> None:
        _check_bytes(self, "sender_id", "target_id", "sequence", "epoch", "lease", "radio")
        radio_of(self.radio)

    @property
    def message_id(self) -> int:
        return _message_id(self)

    def encode(self) -> bytes:
        return bytes(
            [self.kind, self.sender_id, self.target_id, self.sequence, self.epoch, self.lease, self.radio]
        )

    @classmethod
    def from_bytes(cls, data: bytes) -> "KeepaliveFrame":
        _check_layout(data, FrameKind.KEEPALIVE, 7)
        return cls(
            sender_id=data[1], target_id=data[2], sequence=data[3], epoch=data[4], lease=data[5], radio=data[6]
        )


@dataclass
class ProbeFrame:
    """Flooded by the gateway to find a path to ``target_id``.

    Every relay appends itself and the link it heard the probe over, then
    seals it again as its own frame: ``sender_id`` is the last transmitter,
    ``origin_id`` the gateway. ``(origin_id, message_id)`` names the probe;
    every probe needs a fresh one. ``window`` (units of
    ``PROBE_WINDOW_UNIT``) is how long the target collects copies; on a
    PROBE its top bit is ``PROBE_WAKE`` (``wake``).
    """

    sender_id: int
    target_id: int
    sequence: int
    origin_id: int
    window: int
    hops: tuple[ProbeHop, ...] = ()
    epoch: int = 0
    kind: FrameKind = field(default=FrameKind.PROBE, init=False)

    def __post_init__(self) -> None:
        _check_bytes(self, "sender_id", "target_id", "sequence", "epoch", "origin_id", "window")
        self.hops = tuple(self.hops)
        check_route((hop.node_id for hop in self.hops), self.origin_id, self.target_id)
        last = self.hops[-1].node_id if self.hops else self.origin_id
        if self.sender_id != last:
            raise ValueError("a probe's sender must be its last hop (or the origin)")

    @property
    def message_id(self) -> int:
        return _message_id(self)

    @property
    def wake(self) -> bool:
        """A directed wake-up probe, or (Q22, v1 supplement 2026-10-21) a booting node's wake JOIN; DISCOVER uses the whole byte."""

        return self.kind in (FrameKind.PROBE, FrameKind.JOIN) and bool(self.window & PROBE_WAKE)

    @property
    def window_s(self) -> float:
        units = self.window & ~PROBE_WAKE if self.kind in (FrameKind.PROBE, FrameKind.JOIN) else self.window
        return units * PROBE_WINDOW_UNIT

    @property
    def full(self) -> bool:
        """No relay may append itself any more."""

        return len(self.hops) >= MAX_RELAYS

    def extended(self, node_id: int, link: LinkQuality) -> "ProbeFrame":
        """The probe as relay ``node_id`` sends it on."""

        return replace(self, sender_id=node_id, hops=self.hops + (ProbeHop(node_id, link),))

    def _head(self) -> bytes:
        return bytes(
            [
                self.kind,
                self.sender_id,
                self.target_id,
                self.sequence,
                self.epoch,
                self.origin_id,
                self.window,
            ]
        )

    def encode(self) -> bytes:
        return self._head() + b"".join(bytes([hop.node_id]) + hop.link.encode() for hop in self.hops)

    #: Bytes before the hops.
    HEAD = 7

    @classmethod
    def _fields(cls, data: bytes) -> dict:
        kind = cls.__dataclass_fields__["kind"].default
        head = cls.HEAD
        if len(data) < head or (len(data) - head) % 3 or (len(data) - head) // 3 > MAX_RELAYS:
            raise ValueError(f"{kind.name.lower()} frame must be {head} + 3 * hops bytes, at most 16 hops")
        if data[0] != kind:
            raise ValueError(f"frame kind is not {kind.name}")
        hops = tuple(
            ProbeHop(data[i], LinkQuality.from_bytes(data[i + 1 : i + 3]))
            for i in range(head, len(data), 3)
        )
        return dict(
            sender_id=data[1],
            target_id=data[2],
            sequence=data[3],
            epoch=data[4],
            origin_id=data[5],
            window=data[6],
            hops=hops,
        )

    @classmethod
    def from_bytes(cls, data: bytes) -> "ProbeFrame":
        return cls(**cls._fields(data))


@dataclass
class JoinFrame(ProbeFrame):
    """A node that just started announces itself: a PROBE flowing the other way.

    ``origin_id`` is the joining node, ``target_id`` the gateway. Relays pass
    it on exactly like a probe (appending themselves, sealing it again); the
    gateway collects copies for ``window`` and acknowledges the best path
    with a routed REPLY that echoes the message id. Every attempt needs a
    fresh message id, like a probe.
    """

    kind: FrameKind = field(default=FrameKind.JOIN, init=False)


@dataclass
class DiscoverFrame(ProbeFrame):
    """The gateway's scan: a PROBE to every node (``target_id`` 0xFF) or to one.

    Every node passes it on once and keeps the best path, like a probe's
    target; after ``window`` it answers at a random point within ``spread``
    (units of ``DISCOVER_SPREAD_UNIT``) with a DISCOVER_REPLY back along that
    path. Addressed to one node, only that node answers.
    """

    spread: int = 0
    kind: FrameKind = field(default=FrameKind.DISCOVER, init=False)
    HEAD = 8

    def __post_init__(self) -> None:
        super().__post_init__()
        _check_bytes(self, "spread")

    @property
    def spread_s(self) -> float:
        return self.spread * DISCOVER_SPREAD_UNIT

    def _head(self) -> bytes:
        return super()._head() + bytes([self.spread])

    @classmethod
    def from_bytes(cls, data: bytes) -> "DiscoverFrame":
        fields = cls._fields(data)
        return cls(**fields, spread=data[7])


@dataclass
class DiscoverReplyFrame:
    """A node's answer to a DISCOVER, sent back along the path it kept (routed).

    ``links`` has one entry per hop from the gateway outward, as in a
    PROBE_REPLY; ``neighbours`` (at most ``MAX_NEIGHBOURS``) are other nodes
    it heard, strongest first: candidates for another path. The two lists
    are told apart by the route's length (K relays: K + 1 links), so the
    frame decodes only together with its route. ``sequence`` and ``epoch``
    echo the scan.
    """

    sender_id: int
    target_id: int
    sequence: int
    state: int
    links: tuple[LinkQuality, ...]
    neighbours: tuple[ProbeHop, ...] = ()
    epoch: int = 0
    kind: FrameKind = field(default=FrameKind.DISCOVER_REPLY, init=False)

    def __post_init__(self) -> None:
        _check_bytes(self, "sender_id", "target_id", "sequence", "epoch", "state")
        self.links = tuple(self.links)
        self.neighbours = tuple(self.neighbours)
        if not 1 <= len(self.links) <= MAX_RELAYS + 1:
            raise ValueError(f"a path has 1..{MAX_RELAYS + 1} links, got {len(self.links)}")
        if len(self.neighbours) > MAX_NEIGHBOURS:
            raise ValueError(f"at most {MAX_NEIGHBOURS} neighbours, got {len(self.neighbours)}")

    @property
    def message_id(self) -> int:
        return _message_id(self)

    def encode(self) -> bytes:
        head = bytes([self.kind, self.sender_id, self.target_id, self.sequence, self.epoch, self.state])
        links = b"".join(link.encode() for link in self.links)
        return head + links + b"".join(bytes([n.node_id]) + n.link.encode() for n in self.neighbours)

    @classmethod
    def from_bytes(cls, data: bytes, hops: int) -> "DiscoverReplyFrame":
        """``hops`` = links on the path (the route's relays + 1)."""

        rest = len(data) - 6 - 2 * hops
        if data[:1] != bytes([FrameKind.DISCOVER_REPLY]):
            raise ValueError("frame kind is not DISCOVER_REPLY")
        if not 1 <= hops <= MAX_RELAYS + 1 or rest < 0 or rest % 3 or rest // 3 > MAX_NEIGHBOURS:
            raise ValueError("discover reply must be 6 + 2 * links + 3 * neighbours bytes")
        end = 6 + 2 * hops
        links = tuple(LinkQuality.from_bytes(data[i : i + 2]) for i in range(6, end, 2))
        neighbours = tuple(
            ProbeHop(data[i], LinkQuality.from_bytes(data[i + 1 : i + 3])) for i in range(end, len(data), 3)
        )
        return cls(
            sender_id=data[1],
            target_id=data[2],
            sequence=data[3],
            epoch=data[4],
            state=data[5],
            links=links,
            neighbours=neighbours,
        )


@dataclass
class ProbeReplyFrame:
    """The target's chosen path, sent back along it (a routed frame).

    ``links`` has one entry per hop, from the gateway's side outward: the
    first is gateway -> first relay, the last the link into the target.
    ``sequence`` and ``epoch`` echo the probe.
    """

    sender_id: int
    target_id: int
    sequence: int
    links: tuple[LinkQuality, ...]
    epoch: int = 0
    kind: FrameKind = field(default=FrameKind.PROBE_REPLY, init=False)

    def __post_init__(self) -> None:
        _check_bytes(self, "sender_id", "target_id", "sequence", "epoch")
        self.links = tuple(self.links)
        if not 1 <= len(self.links) <= MAX_RELAYS + 1:
            raise ValueError(f"a path has 1..{MAX_RELAYS + 1} links, got {len(self.links)}")

    @property
    def message_id(self) -> int:
        return _message_id(self)

    def encode(self) -> bytes:
        head = bytes([self.kind, self.sender_id, self.target_id, self.sequence, self.epoch])
        return head + b"".join(link.encode() for link in self.links)

    @classmethod
    def from_bytes(cls, data: bytes) -> "ProbeReplyFrame":
        if len(data) < 7 or (len(data) - 5) % 2:
            raise ValueError("probe reply frame must be 5 + 2 * links bytes")
        if data[0] != FrameKind.PROBE_REPLY:
            raise ValueError("frame kind is not PROBE_REPLY")
        links = tuple(LinkQuality.from_bytes(data[i : i + 2]) for i in range(5, len(data), 2))
        return cls(
            sender_id=data[1], target_id=data[2], sequence=data[3], epoch=data[4], links=links
        )


@dataclass
class RouteErrorFrame:
    """A relay could not hand a routed frame to ``broken``, its next hop.

    Sent by that relay as its own frame, back toward the gateway;
    ``sequence`` and ``epoch`` name the frame that failed, so the gateway can
    fall back to flooding it at once.
    """

    sender_id: int
    target_id: int
    sequence: int
    broken: int
    epoch: int = 0
    kind: FrameKind = field(default=FrameKind.ROUTE_ERROR, init=False)

    def __post_init__(self) -> None:
        _check_bytes(self, "sender_id", "target_id", "sequence", "epoch", "broken")

    @property
    def message_id(self) -> int:
        return _message_id(self)

    def encode(self) -> bytes:
        return bytes(
            [self.kind, self.sender_id, self.target_id, self.sequence, self.epoch, self.broken]
        )

    @classmethod
    def from_bytes(cls, data: bytes) -> "RouteErrorFrame":
        _check_layout(data, FrameKind.ROUTE_ERROR, 6)
        return cls(
            sender_id=data[1], target_id=data[2], sequence=data[3], epoch=data[4], broken=data[5]
        )


@dataclass(frozen=True)
class TraceRecord:
    """What one node saw of the traced message (4 B on the wire).

    ``rssi_out`` / ``snr_out``: how strong the request came in from the node
    before it; ``rssi_back``: how strong the answer came in from the node
    after it (``None``: not heard; whole dBm, quarter dB). Repeats (0..3,
    saturating) and outcomes (``TRACE_*``) of its own sends, each way.
    """

    rssi_out: "int | None" = None
    snr_out: "int | None" = None
    rssi_back: "int | None" = None
    out_repeats: int = 0
    out_state: int = TRACE_UNSEEN
    back_repeats: int = 0
    back_state: int = TRACE_UNSEEN

    def __post_init__(self) -> None:
        for name in ("rssi_out", "rssi_back"):
            value = getattr(self, name)
            if value is not None and not -255 <= value <= -1:
                raise ValueError(f"{name} out of range -255..-1 dBm: {value!r}")
        if self.snr_out is not None and not -127 <= self.snr_out <= 127:
            raise ValueError(f"snr_out out of range -127..127 quarter dB: {self.snr_out!r}")
        for name in ("out_repeats", "out_state", "back_repeats", "back_state"):
            if not 0 <= getattr(self, name) <= 3:
                raise ValueError(f"{name} must fit 2 bits: {getattr(self, name)!r}")

    def encode(self) -> bytes:
        flags = self.out_repeats << 6 | self.out_state << 4 | self.back_repeats << 2 | self.back_state
        return bytes(
            [
                0 if self.rssi_out is None else -self.rssi_out,
                0x80 if self.snr_out is None else self.snr_out & 0xFF,  # -128: not heard
                0 if self.rssi_back is None else -self.rssi_back,
                flags,
            ]
        )

    @classmethod
    def from_bytes(cls, data: bytes) -> "TraceRecord":
        snr = None if data[1] == 0x80 else (data[1] - 0x100 if data[1] & 0x80 else data[1])
        return cls(
            rssi_out=-data[0] if data[0] else None,
            snr_out=snr,
            rssi_back=-data[2] if data[2] else None,
            out_repeats=data[3] >> 6,
            out_state=data[3] >> 4 & 3,
            back_repeats=data[3] >> 2 & 3,
            back_state=data[3] & 3,
        )


@dataclass
class TraceFrame:
    """Asks every node on a route how message ``(query_sequence, query_epoch)`` fared there."""

    sender_id: int
    target_id: int
    sequence: int
    query_sequence: int
    query_epoch: int
    epoch: int = 0
    kind: FrameKind = field(default=FrameKind.TRACE, init=False)

    def __post_init__(self) -> None:
        _check_bytes(
            self, "sender_id", "target_id", "sequence", "epoch", "query_sequence", "query_epoch"
        )

    @property
    def message_id(self) -> int:
        return _message_id(self)

    def encode(self) -> bytes:
        return bytes(
            [
                self.kind,
                self.sender_id,
                self.target_id,
                self.sequence,
                self.epoch,
                self.query_sequence,
                self.query_epoch,
            ]
        )

    @classmethod
    def from_bytes(cls, data: bytes) -> "TraceFrame":
        _check_layout(data, FrameKind.TRACE, 7)
        return cls(
            sender_id=data[1],
            target_id=data[2],
            sequence=data[3],
            epoch=data[4],
            query_sequence=data[5],
            query_epoch=data[6],
        )


@dataclass
class TraceReplyFrame:
    """The trace's answer: one ``TraceRecord`` per node passed, target first.

    ``sender_id`` is the node that last appended (it seals the frame again as
    its own); ``sequence`` and ``epoch`` echo the TRACE.
    """

    sender_id: int
    target_id: int
    sequence: int
    records: tuple[TraceRecord, ...]
    epoch: int = 0
    kind: FrameKind = field(default=FrameKind.TRACE_REPLY, init=False)

    def __post_init__(self) -> None:
        _check_bytes(self, "sender_id", "target_id", "sequence", "epoch")
        self.records = tuple(self.records)
        if not 1 <= len(self.records) <= MAX_RELAYS + 1:
            raise ValueError(f"a trace holds 1..{MAX_RELAYS + 1} records, got {len(self.records)}")

    @property
    def message_id(self) -> int:
        return _message_id(self)

    def encode(self) -> bytes:
        head = bytes([self.kind, self.sender_id, self.target_id, self.sequence, self.epoch])
        return head + b"".join(record.encode() for record in self.records)

    @classmethod
    def from_bytes(cls, data: bytes) -> "TraceReplyFrame":
        if len(data) < 9 or (len(data) - 5) % 4:
            raise ValueError("trace reply frame must be 5 + 4 * records bytes")
        if data[0] != FrameKind.TRACE_REPLY:
            raise ValueError("frame kind is not TRACE_REPLY")
        records = tuple(TraceRecord.from_bytes(data[i : i + 4]) for i in range(5, len(data), 4))
        return cls(
            sender_id=data[1], target_id=data[2], sequence=data[3], epoch=data[4], records=records
        )


@dataclass(frozen=True)
class WakeNetFrame:
    """Wake every dormant node for ``lease`` seconds (a long-preamble flood)."""

    sender_id: int
    sequence: int
    lease: int
    epoch: int = 0
    target_id: int = field(default=BROADCAST_ID, init=False)
    kind: FrameKind = field(default=FrameKind.WAKE_NET, init=False)

    def __post_init__(self) -> None:
        _check_bytes(self, "sender_id", "sequence", "epoch", "lease")

    @property
    def message_id(self) -> int:
        return _message_id(self)

    def encode(self) -> bytes:
        return bytes(
            [self.kind, self.sender_id, self.target_id, self.sequence, self.epoch, self.lease]
        )

    @classmethod
    def from_bytes(cls, data: bytes) -> "WakeNetFrame":
        _check_layout(data, FrameKind.WAKE_NET, 6)
        if data[2] != BROADCAST_ID:
            raise ValueError("wake_net frame must be broadcast")
        return cls(sender_id=data[1], sequence=data[3], epoch=data[4], lease=data[5])


@dataclass(frozen=True)
class SleepNetFrame:
    """Send every awake node back to dormant (an ordinary flood; leases are the fallback)."""

    sender_id: int
    sequence: int
    epoch: int = 0
    target_id: int = field(default=BROADCAST_ID, init=False)
    kind: FrameKind = field(default=FrameKind.SLEEP_NET, init=False)

    def __post_init__(self) -> None:
        _check_bytes(self, "sender_id", "sequence", "epoch")

    @property
    def message_id(self) -> int:
        return _message_id(self)

    def encode(self) -> bytes:
        return bytes([self.kind, self.sender_id, self.target_id, self.sequence, self.epoch])

    @classmethod
    def from_bytes(cls, data: bytes) -> "SleepNetFrame":
        _check_layout(data, FrameKind.SLEEP_NET, 5)
        if data[2] != BROADCAST_ID:
            raise ValueError("sleep_net frame must be broadcast")
        return cls(sender_id=data[1], sequence=data[3], epoch=data[4])


Frame = Union[
    WakeFrame,
    RequestFrame,
    ReplyFrame,
    ResultFrame,
    HeartbeatFrame,
    HelloFrame,
    ReportFrame,
    EventFrame,
    KeepaliveFrame,
    ProbeFrame,
    ProbeReplyFrame,
    RouteErrorFrame,
    TraceFrame,
    TraceReplyFrame,
    JoinFrame,
    DiscoverFrame,
    DiscoverReplyFrame,
    WakeNetFrame,
    SleepNetFrame,
]

_DECODERS = {
    FrameKind.REQUEST: RequestFrame.from_bytes,
    FrameKind.REPLY: ReplyFrame.from_bytes,
    FrameKind.RESULT: ResultFrame.from_bytes,
    FrameKind.WAKE: WakeFrame.from_bytes,
    FrameKind.HEARTBEAT: HeartbeatFrame.from_bytes,
    FrameKind.HELLO: HelloFrame.from_bytes,
    FrameKind.REPORT: ReportFrame.from_bytes,
    FrameKind.EVENT: EventFrame.from_bytes,
    FrameKind.KEEPALIVE: KeepaliveFrame.from_bytes,
    FrameKind.PROBE: ProbeFrame.from_bytes,
    FrameKind.PROBE_REPLY: ProbeReplyFrame.from_bytes,
    FrameKind.ROUTE_ERROR: RouteErrorFrame.from_bytes,
    FrameKind.TRACE: TraceFrame.from_bytes,
    FrameKind.TRACE_REPLY: TraceReplyFrame.from_bytes,
    FrameKind.JOIN: JoinFrame.from_bytes,
    FrameKind.DISCOVER: DiscoverFrame.from_bytes,
    FrameKind.WAKE_NET: WakeNetFrame.from_bytes,
    FrameKind.SLEEP_NET: SleepNetFrame.from_bytes,
}


def _decode(data: bytes, relays: Optional[int] = None) -> Frame:
    """``relays``: the route's length, for a frame that needs it to decode."""

    if not data:
        raise ValueError("frame is empty")
    if data[0] == FrameKind.DISCOVER_REPLY:
        if relays is None:
            raise ValueError("a discover reply decodes only together with its route")
        return DiscoverReplyFrame.from_bytes(data, relays + 1)
    decoder = _DECODERS.get(data[0])
    if decoder is None:
        raise ValueError(f"unknown frame kind: {data[0]:#04x}")
    return decoder(data)


def decode_frame(data: bytes) -> Frame:
    """Decode any frame without a route by looking at its first byte.

    Raises ``ValueError`` for empty data, an unknown kind, a wrong length
    or an invalid field value, and for routed frames (use ``decode_routed``)
    and kinds that only ever travel routed.
    """

    if data and data[0] & ROUTED:
        raise ValueError("routed frame: use decode_routed")
    if data and data[0] in ROUTED_ONLY:
        raise ValueError(f"{FrameKind(data[0]).name} frames always carry a route")
    return _decode(data)


def is_routed(data: bytes) -> bool:
    return bool(data) and bool(data[0] & ROUTED)


def _check_path_fits(kind: int, base_length: int, relays: int) -> None:
    # A PROBE_REPLY lists one link per hop: relays + 1.
    if kind == FrameKind.PROBE_REPLY and (base_length - 5) // 2 != relays + 1:
        raise ValueError("probe reply must list one link per hop of its route")


def add_route(plain: bytes, relays: Iterable[int]) -> bytes:
    """Append a route trailer to a plain frame and set ``ROUTED`` in its KIND."""

    plain = bytes(plain)
    if not plain or plain[0] & ROUTED:
        raise ValueError("frame is empty or already routed")
    if plain[0] not in ROUTABLE:
        raise ValueError(f"frame kind {plain[0]:#04x} cannot carry a route")
    route = check_route(relays, plain[1], plain[2])
    _decode(plain, len(route))  # the frame itself must be valid
    _check_path_fits(plain[0], len(plain), len(route))
    return bytes([plain[0] | ROUTED]) + plain[1:] + bytes(route) + bytes([len(route)])


def split_route(data: bytes) -> tuple[bytes, tuple[int, ...]]:
    """``(frame without its route, relays)`` of a routed plain frame.

    Parses from the end, so a relay needs no per-kind layout; the frame
    itself is not decoded.
    """

    data = bytes(data)
    if not is_routed(data):
        raise ValueError("frame is not routed")
    if len(data) < 5:
        raise ValueError("routed frame too short")
    info = data[-1]
    if info >> 5:
        raise ValueError(f"unknown route format: {info >> 5}")
    count = info & 0x1F
    if count > MAX_RELAYS or len(data) < 4 + count + 1:
        raise ValueError(f"bad route length: {count}")
    base = bytes([data[0] & ~ROUTED & 0xFF]) + data[1 : len(data) - 1 - count]
    route = check_route(data[len(data) - 1 - count : -1], base[1], base[2])
    return base, route


def decode_routed(data: bytes) -> tuple[Frame, tuple[int, ...]]:
    """Decode a routed frame: ``(frame, relays in transmission order)``."""

    base, route = split_route(data)
    if base[0] not in ROUTABLE:
        raise ValueError(f"frame kind {base[0]:#04x} cannot carry a route")
    frame = _decode(base, len(route))
    _check_path_fits(base[0], len(base), len(route))
    return frame, route


def reply_route(route: Iterable[int]) -> tuple[int, ...]:
    """The route of an answer: the request's route walked backwards."""

    return tuple(reversed(tuple(route)))
