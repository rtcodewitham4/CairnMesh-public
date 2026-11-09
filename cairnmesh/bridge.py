"""PC side of the dual-radio gateway board as a thin bridge (spec 6.1).

The board (``firmware/gateway``, ``firmware/cmd/gateway``) only puts frames
on its radios, hands up what they receive and arbitrates the two radios
(spec 15.3: never both transmit; while one waits for an answer the other
holds back frames not marked urgent; every transmission is reported so the
other radio counts as blinded meanwhile; a radio receiving a packet holds
its frames until the end and says so with BUSY, for listen-before-talk).
Sessions, probes and floods stay in ``gateway.py``. One bridge message per
serial-hop frame (spec 6), numbers big-endian::

    PC → board   TX       0x01 radio flags preamble:2 answer_ms:2 frame…
    board → PC   RX       0x81 radio rssi:2 snr frame…
                 TX_DONE  0x82 radio result queued_ms:2 air_ms:2
                 BUSY     0x84 radio receiving
    PC → board   TUNE     0x02 radio channel sf
                 PING     0x03          → STATUS 0x83 version radios radio_bytes
                                          {healthy channel sf queued crc:2 header:2 retries:2 faults:2 resets:2}×radios
                                          counters counters:4×counters

To grow: fields are only appended; readers ignore bytes past the ones they
know and message types they do not know (``decode`` returns ``None``).

``GatewayBridge`` sits on any ``Transport`` (``SerialTransport`` for the
board); ``GatewayBridge.ports[i]`` is radio ``i`` as a ``mesh.RadioPort``.
Callbacks run on the transport's reader thread.
"""

from __future__ import annotations

import math
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable, Optional, Union

from .mesh import RadioReceiver
from .transport import Transport

MSG_TX = 0x01
MSG_TUNE = 0x02
MSG_PING = 0x03
MSG_RX = 0x81
MSG_TX_DONE = 0x82
MSG_STATUS = 0x83
MSG_BUSY = 0x84

#: The bridge format this side speaks (STATUS carries the board's).
VERSION = 1

#: TX flag: rule 2 must not hold this frame back (session commands, retransmissions).
FLAG_URGENT = 0x01

TX_OK = 0
TX_TIMEOUT = 1
TX_REFUSED = 2
TX_QUEUE_FULL = 3

#: The radio's own preamble (8 symbols) unless a TX asks for another.
DEFAULT_PREAMBLE = 8

#: Bytes per radio in STATUS this side reads (the board may send more).
RADIO_STATUS = 12

#: STATUS counters, in order (the board may send more, or fewer: those read 0).
#: ``tx_stuck``: transmissions the board ended itself (no end from the radio,
#: which it then reset); ``watchdog_boot``: 1 when the board's last boot
#: followed a watchdog reset.
COUNTERS = (
    "received",
    "sent",
    "deferred",
    "refused",
    "queue_full",
    "malformed",
    "tune_failed",
    "held",
    "tx_stuck",
    "watchdog_boot",
)

#: How far ahead ``busy_until`` puts the end of a reception or of queued
#: frames: neither is known, so the radio asks again that much later.
BUSY_STEP = 0.01


@dataclass(frozen=True)
class Rx:
    radio: int
    rssi: int  # dBm
    snr: int  # dB
    frame: bytes


@dataclass(frozen=True)
class TxDone:
    radio: int
    result: int
    queued: float  # seconds the frame waited on the board
    air: float  # seconds from its start to its end
    at: float = 0.0  # local clock when it arrived


@dataclass(frozen=True)
class RadioStatus:
    healthy: bool
    channel: int
    sf: int
    queued: int
    crc_errors: int = 0
    header_errors: int = 0
    bus_retries: int = 0  # SPI transfers the chip did not take, sent again
    faults: int = 0
    resets: int = 0  # times the board reset the radio (failed check, stuck transmission)


@dataclass(frozen=True)
class Status:
    version: int
    radios: tuple[RadioStatus, ...]
    counters: dict


@dataclass(frozen=True)
class Busy:
    radio: int
    receiving: bool  # a packet is coming in (valid header, no end yet)


Message = Union[Rx, TxDone, Status, Busy]


def encode_tx(radio: int, frame: bytes, *, preamble: int = 0, urgent: bool = True, answer: float = 0.0) -> bytes:
    """A TX message; ``answer``: seconds radio ``radio`` then waits for an answer (rule 2)."""

    if not 1 <= len(frame) <= 255:
        raise ValueError("a LoRa frame is 1..255 bytes")
    answer_ms = min(round(answer * 1000), 0xFFFF)
    flags = FLAG_URGENT if urgent else 0
    return bytes([MSG_TX, radio, flags]) + preamble.to_bytes(2, "big") + answer_ms.to_bytes(2, "big") + bytes(frame)


def encode_tune(radio: int, channel: int, sf: int) -> bytes:
    return bytes([MSG_TUNE, radio, channel, sf])


PING = bytes([MSG_PING])


def decode(msg: bytes, at: float = 0.0) -> Optional[Message]:
    """One message from the board; ``None`` for a type this side does not
    know, ``ValueError`` when it does not parse."""

    if not msg:
        raise ValueError("empty message")
    kind = msg[0]
    if kind == MSG_RX and len(msg) > 5:
        rssi = int.from_bytes(msg[2:4], "big", signed=True)
        snr = int.from_bytes(msg[4:5], "big", signed=True)
        return Rx(msg[1], rssi, snr, bytes(msg[5:]))
    if kind == MSG_TX_DONE and len(msg) >= 7:
        queued = int.from_bytes(msg[3:5], "big") / 1000
        air = int.from_bytes(msg[5:7], "big") / 1000
        return TxDone(msg[1], msg[2], queued, air, at)
    if kind == MSG_BUSY and len(msg) >= 3:
        return Busy(msg[1], bool(msg[2]))
    if kind == MSG_STATUS and len(msg) >= 4 and msg[3] >= RADIO_STATUS:
        count, size = msg[2], msg[3]
        base = 4 + size * count
        if len(msg) > base and len(msg) >= base + 1 + 4 * msg[base]:
            radios = []
            for i in range(count):
                r = msg[4 + size * i : 4 + size * (i + 1)]
                errors = (int.from_bytes(r[k : k + 2], "big") for k in (4, 6, 8, 10, 12) if k + 2 <= size)
                radios.append(RadioStatus(bool(r[0]), r[1], r[2], r[3], *errors))
            sent = msg[base]
            counters = {
                name: int.from_bytes(msg[base + 1 + 4 * k : base + 5 + 4 * k], "big") if k < sent else 0
                for k, name in enumerate(COUNTERS)
            }
            return Status(msg[1], tuple(radios), counters)
    if kind not in (MSG_RX, MSG_TX_DONE, MSG_BUSY, MSG_STATUS):
        return None
    raise ValueError(f"bad message {bytes(msg[:8]).hex()}")


class BridgeRadio:
    """One radio of the board as a ``mesh.RadioPort`` (``send``, ``set_receiver``, ``set_radio``)."""

    def __init__(self, bridge: "GatewayBridge", index: int) -> None:
        self._bridge = bridge
        self.index = index
        self._receiver: Optional[RadioReceiver] = None
        #: Where the radio is asked to be (the board applies TUNE after this radio's queued frames).
        self.channel = 0
        self.sf: Optional[int] = None

    def set_receiver(self, callback: RadioReceiver) -> None:
        self._receiver = callback

    def send(
        self, data: bytes, preamble: int = DEFAULT_PREAMBLE, *, urgent: bool = True, answer: float = 0.0
    ) -> None:
        """Queue ``data`` on this radio; ``urgent=False`` lets rule 2 hold it back."""

        self._bridge._send(encode_tx(self.index, data, preamble=preamble, urgent=urgent, answer=answer), self.index)

    def set_radio(self, channel: int, sf: int) -> None:
        """Move to ``channel`` at ``sf`` (Q11 / Q12), after this radio's frames already queued."""

        self.channel, self.sf = channel, sf
        self._bridge._send(encode_tune(self.index, channel, sf))

    def busy_until(self) -> float:
        """Listen-before-talk (``mesh.RadioPort``): while the radio receives a
        packet or has frames queued, a moment from now; else the past."""

        return self._bridge._busy_until(self.index)

    def _deliver(self, rx: Rx) -> None:
        if self._receiver is not None:
            self._receiver(rx.frame, float(rx.rssi))


class GatewayBridge:
    """The board's radios over ``transport``; see the module docstring."""

    def __init__(
        self,
        transport: Transport,
        *,
        radios: int = 2,
        clock: Callable[[], float] = time.monotonic,
        on_tx_done: Optional[Callable[[TxDone], None]] = None,
        on_error: Optional[Callable[[Exception], None]] = None,
        keep: int = 256,
    ) -> None:
        self._transport = transport
        self._clock = clock
        self.on_tx_done = on_tx_done
        self._on_error = on_error
        self.ports = [BridgeRadio(self, i) for i in range(radios)]
        #: The last ``keep`` transmissions (``TxDone``), oldest first.
        self.transmissions: deque[TxDone] = deque(maxlen=keep)
        #: TX sent per radio without their TX_DONE yet.
        self.pending = [0] * radios
        #: Radios receiving a packet now (BUSY).
        self.receiving = [False] * radios
        self.malformed = 0
        #: Messages of a type this side does not know (a newer board).
        self.ignored = 0
        self._lock = threading.Lock()
        self._status: Optional[Status] = None
        self._status_ready = threading.Event()
        transport.set_receiver(self._on_message)

    def ping(self, timeout: float = 1.0) -> Optional[Status]:
        """Ask the board for its STATUS; ``None`` when none came within ``timeout``."""

        self._status_ready.clear()
        self._transport.send(PING)
        if not self._status_ready.wait(timeout):
            return None
        return self._status

    def blinded(self, radio: int, start: float, end: float) -> bool:
        """Whether another radio transmitted during [start, end] (local clock; rule 3)."""

        with self._lock:
            done = list(self.transmissions)
        return any(d.radio != radio and d.at - d.air < end and d.at > start for d in done)

    def _busy_until(self, radio: int) -> float:
        with self._lock:
            busy = self.receiving[radio] or self.pending[radio] > 0
        return self._clock() + BUSY_STEP if busy else -math.inf

    def _send(self, msg: bytes, radio: Optional[int] = None) -> None:
        if radio is not None:
            with self._lock:
                self.pending[radio] += 1
        self._transport.send(msg)

    def _on_message(self, msg: bytes) -> None:
        now = self._clock()
        try:
            message = decode(msg, now)
        except ValueError as exc:
            self.malformed += 1
            if self._on_error is not None:
                self._on_error(exc)
            return
        if message is None:
            self.ignored += 1
        elif isinstance(message, Busy):
            if message.radio < len(self.receiving):
                with self._lock:
                    self.receiving[message.radio] = message.receiving
        elif isinstance(message, Rx):
            if message.radio < len(self.ports):
                self.ports[message.radio]._deliver(message)
        elif isinstance(message, TxDone):
            with self._lock:
                if message.radio < len(self.pending) and self.pending[message.radio] > 0:
                    self.pending[message.radio] -= 1
                self.transmissions.append(message)
            if self.on_tx_done is not None:
                self.on_tx_done(message)
        else:
            self._status = message
            self._status_ready.set()
