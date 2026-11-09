"""Authenticated encryption for any ``Transport``.

``SecureTransport`` wraps a transport so that ``Controller`` and ``Device``
stay unchanged: they hand it plain frames, it puts sealed ones on the wire.

    KIND|0x80 | SENDER | TARGET | SEQ | FCNT (4 B, little-endian) | TX | ciphertext | MIC (4 B)

The first 9 bytes are authenticated but not encrypted (AAD); the rest of the
plain frame (ACTION, VALUE, RESULT, STATE ...) is encrypted with AES-128-CCM
(RFC 3610, 4-byte MIC, 2-byte length field, 13-byte nonce):

    nonce = SENDER | TARGET | FCNT (4 B) | KIND|0x80 | TX | 5 zero bytes

``TX`` names the node that put this copy on the air: the originator sets it
to SENDER, and a relay re-seals the frame with ``TX`` = itself and nothing
else changed (``reseal``). SENDER and FCNT, and with them every duplicate
key, survive the hop, while neighbours can still tell who transmitted. The
nonce stays unique: one (SENDER, FCNT) names exactly one plain frame, so
two copies under the same nonce are byte-identical. That holds only while
no relay alters the plain frame; a node that does (a PROBE relay appending
itself) must seal a new frame under its own SENDER and FCNT.

The firmware (``firmware/proto/secure.go``) implements this layout and passes
the v4 vectors on the host and, with the hardware AES, on the board.

Three properties matter:

* **Every transmission gets a fresh FCNT.** A retry is sealed again, so the
  receiver's replay window does not mistake it for a replayed frame. The
  device's duplicate cache (SEQUENCE) still recognizes it as a retry.
* **FCNT never repeats under one key.** It starts at the current Unix time
  (or ``initial_fcnt``), so a restarted sender is normally ahead of its
  earlier run; it also acts as the session epoch for replay protection. That
  holds only while the sender averages less than 1 frame/s: a busier sender
  runs ahead of the clock, and after a restart the receivers would reject it
  until the clock catches up. Pass ``state_file`` to make it hold always: the
  counter is then reserved in blocks of ``TX_RESERVE`` and the restart point is
  the larger of the clock and the file.
* **Replay marks survive a restart.** With ``state_file`` the highest FCNT
  accepted from each sender is written back (at most once per
  ``RX_FLUSH_INTERVAL`` seconds, and on ``flush()`` / ``close()``), so frames
  recorded before a restart cannot be replayed afterwards. A crash can lose
  the marks of the last second; every counter at or below a restored mark is
  refused, including legitimate stragglers. A device (flash, about 10 000
  erases a page) keeps only the controller's mark (``reboot``, spec
  section 5): the frames that make it act; every other sender's window
  restarts empty, like the relay windows.

**Application layer (task Q14).** With ``app`` (``AppKeys``), REQUEST,
REPORT, RESULT, EVENT (v4) and the REPLY to a REQUEST or RESULT are also sealed end to end, inside the
network envelope, under the device's own application key: relays (who hold
the network key) can neither read nor forge them. Below this layer such a
frame reads

    KIND|APP | SENDER | TARGET | SEQ | EPOCH | KEYVER | E(payload) | AMIC (4 B)

(then the route trailer, if any), with AES-128-CCM:

    nonce = SENDER | TARGET | FCNT (4 B) | KIND|APP | KEYVER | 5 zero bytes
    AAD   = KIND|APP | SENDER | TARGET | SEQ | EPOCH | KEYVER (+ the clear payload of a REPORT, v3)

FCNT is the originator's network counter, which relays keep: a relay that
re-sealed a recorded frame under a fresh FCNT breaks the inner MIC, so the
receiver's replay window covers the inner layer too, at no extra byte. The
key is the one of the end that is not the controller (``AppKeys``), picked
by KEYVER so that a key can be replaced node by node. Which REPLY carries the
layer: one answering a REQUEST or acknowledging a RESULT this transport
sealed or opened lately (``APP_EXCHANGES``), or one the controller still
awaits (``awaits_request_answer``); answers to WAKE, KEEPALIVE and JOIN and REPORT and
EVENT acknowledgements stay network-only (forging one does no more than dropping
the frame, which a relay can anyway). A REQUEST, REPORT, RESULT or EVENT without the layer,
or a REPLY without it to such a REQUEST or RESULT, is dropped (``app_missing``).
Above this layer every frame is the plain layout again.

Needs the ``cryptography`` package.
"""

from __future__ import annotations

import json
import os
import struct
import threading
import time
from typing import Callable, Optional

from .durable import write_json
from .frames import APP, APP_KINDS, ROUTED, FrameKind
from .transport import Receiver, Transport

SECURE = 0x80
HEADER = 9
#: Offset of the TX byte in the sealed header.
TX_OFFSET = 8
MIC_LEN = 4
NONCE_LEN = 13
KEY_LEN = 16
WINDOW = 32
#: With a ``state_file``, FCNT values are reserved this many at a time, so the
#: file is written once per block and a crash can never reuse a counter.
TX_RESERVE = 256
#: Minimum seconds between writes of the receive-side replay marks.
RX_FLUSH_INTERVAL = 1.0
#: Bytes the application layer adds: KEYVER and the 4-byte AMIC.
APP_OVERHEAD = 1 + MIC_LEN
#: REQUEST / RESULT exchanges remembered, to know which REPLY carries the layer.
APP_EXCHANGES = 16
#: Frames whose REPLY carries the layer: a REQUEST's answer, and the
#: controller's acknowledgement of a RESULT (both ends note the RESULT as they
#: seal or open it, so they agree however long ago the request was).
_EXCHANGES = (FrameKind.REQUEST, FrameKind.RESULT)
#: Kinds whose payload the application layer authenticates but leaves in
#: clear (wire format v3, 2026-10-21 review C1, user's decision): a REPORT's
#: SUBJECT and EVENT go into the inner AAD, so the neighbours that suppress
#: duplicate reports (section 8) can read them; forging one still takes the
#: node's application key. Same 12 B as before: head 6 B + payload + AMIC 4 B.
_CLEAR_KINDS = (FrameKind.REPORT,)


def _nonce(header: bytes) -> bytes:
    return (
        bytes([header[1], header[2]])
        + header[4:8]
        + bytes([header[0], header[TX_OFFSET]])
        + bytes(5)
    )


def seal(aead, plain: bytes, fcnt: int, tx: Optional[int] = None) -> bytes:
    """Wrap a plain frame (``KIND|SENDER|TARGET|SEQ|payload``) for the wire.

    ``tx`` is the transmitting node; it defaults to SENDER (an originated frame).
    """

    if len(plain) < 4:
        raise ValueError("frame must be at least 4 bytes")
    if plain[0] & SECURE:
        raise ValueError("frame is already sealed")
    if not 0 <= fcnt <= 0xFFFFFFFF:
        raise OverflowError("frame counter exhausted")
    tx = plain[1] if tx is None else tx
    if not 0 <= tx <= 0xFF:
        raise ValueError(f"tx out of range 0..255: {tx}")
    header = (
        bytes([plain[0] | SECURE, plain[1], plain[2], plain[3]])
        + struct.pack("<I", fcnt)
        + bytes([tx])
    )
    return header + aead.encrypt(_nonce(header), bytes(plain[4:]), header)


def transmitter(sealed: bytes) -> int:
    """The TX byte: who put this copy on the air (check the MIC first)."""

    return sealed[TX_OFFSET]


def reseal(aead, sealed: bytes, tx: int) -> bytes:
    """The same frame (SENDER, FCNT, plain bytes) as transmitted by ``tx``.

    Relays use it for frames they pass on unchanged; see the module docstring.
    """

    plain, fcnt = open_sealed(aead, sealed)
    return seal(aead, plain, fcnt, tx)


def open_sealed(aead, sealed: bytes) -> tuple[bytes, int]:
    """Verify and decrypt; returns ``(plain frame, fcnt)``.

    Raises ``ValueError`` for a short frame and
    ``cryptography.exceptions.InvalidTag`` for a failed MIC.
    """

    if len(sealed) < HEADER + MIC_LEN or not sealed[0] & SECURE:
        raise ValueError("not a sealed frame")
    header = bytes(sealed[:HEADER])
    body = aead.decrypt(_nonce(header), bytes(sealed[HEADER:]), header)
    plain = bytes([header[0] & ~SECURE & 0xFF]) + header[1:4] + body
    return plain, struct.unpack("<I", header[4:8])[0]


class AppKeys:
    """Application keys by node and key version (task Q14).

    A device holds its own key(s); the controller holds every device's. The
    frame's key belongs to the end that is not the controller: TARGET when
    it is in this table, else SENDER. New frames use ``current`` (the newest
    version added unless ``use`` says otherwise); any version still held is
    accepted.
    """

    def __init__(self, keys: Optional[dict[int, bytes]] = None, version: int = 0) -> None:
        # [MEM] application keys | 端: device 1–2 把 / controller 每节点 1–2 把 | 每项: 16 B + 版本 1 B
        self._keys: dict[int, dict[int, bytes]] = {}
        self.current: dict[int, int] = {}
        for node, key in (keys or {}).items():
            self.add(node, key, version)

    def add(self, node: int, key: bytes, version: int = 0) -> None:
        if len(key) != KEY_LEN or not 0 <= version <= 0xFF:
            raise ValueError(f"key must be {KEY_LEN} bytes and version 0..255")
        self._keys.setdefault(node, {})[version] = bytes(key)
        self.current[node] = version

    def use(self, node: int, version: int) -> None:
        if version not in self._keys.get(node, {}):
            raise KeyError(f"no key version {version} for node {node:#04x}")
        self.current[node] = version

    def drop(self, node: int, version: int) -> None:
        self._keys.get(node, {}).pop(version, None)

    def node_of(self, sender: int, target: int) -> int:
        return target if target in self._keys else sender

    def key(self, node: int, version: int) -> Optional[bytes]:
        return self._keys.get(node, {}).get(version)


def _app_nonce(head: bytes, fcnt: int) -> bytes:
    return bytes([head[1], head[2]]) + struct.pack("<I", fcnt) + bytes([head[0], head[5]]) + bytes(5)


def _split(plain: bytes) -> Optional[tuple[bytes, bytes]]:
    """``(frame, route trailer)``; the trailer parsed from the end (``frames.split_route``).

    ``None`` when the trailer leaves less than a frame head (a malformed frame
    can still carry a good MIC: review B4).
    """

    if not plain[0] & ROUTED:
        return plain, b""
    count = plain[-1] & 0x1F
    cut = len(plain) - 1 - count
    if cut < 5:
        return None
    return bytes([plain[0] & ~ROUTED & 0xFF]) + plain[1:cut], plain[cut:]


def _join(frame: bytes, trailer: bytes) -> bytes:
    return frame if not trailer else bytes([frame[0] | ROUTED]) + frame[1:] + trailer


class ReplayWindow:
    """Accept each frame counter once, tolerating reordering within 32 frames."""

    def __init__(self) -> None:
        self._last = 0
        self._bits = 0  # bit i set: counter _last - i was seen
        self._used = False

    @property
    def last(self) -> Optional[int]:
        """Highest counter accepted so far, or ``None`` if none yet."""

        return self._last if self._used else None

    @classmethod
    def restored(cls, last: int) -> "ReplayWindow":
        """A window that treats every counter up to ``last`` as already seen."""

        window = cls()
        window._last, window._bits, window._used = last, (1 << WINDOW) - 1, True
        return window

    def check(self, fcnt: int) -> bool:
        """Would ``fcnt`` be new? Does not record it."""

        if not self._used or fcnt > self._last:
            return True
        distance = self._last - fcnt
        return distance < WINDOW and not self._bits & (1 << distance)

    def accept(self, fcnt: int) -> None:
        """Record ``fcnt``; call only after the MIC verified."""

        if not self._used:
            self._last, self._bits, self._used = fcnt, 1, True
        elif fcnt > self._last:
            shift = fcnt - self._last
            self._bits = 0 if shift >= WINDOW else (self._bits << shift) & 0xFFFFFFFF
            self._bits |= 1
            self._last = fcnt
        elif self._last - fcnt < WINDOW:
            self._bits |= 1 << (self._last - fcnt)
        # Further below: outside the window, nothing to record (``check`` refuses it
        # anyway). A node records its own frames without ``check`` (``MeshRelay``):
        # shifting by the full distance built a huge int there (audit 2026-10-22).


class SecureTransport:
    """Seal outgoing and open incoming frames of an inner ``Transport``.

    A frame that fails the MIC, repeats a counter or arrives unsealed (unless
    ``allow_plain``) is dropped. ``on_reject(reason, raw)`` sees each one;
    ``stats`` counts them.

    ``state_file`` makes the counters survive restarts (see the module
    docstring). An unreadable or corrupt file is treated as empty and noted in
    ``state_error``; a failed write while sending raises, so a frame is never
    sent under a counter that was not reserved first.
    """

    def __init__(
        self,
        inner: Transport,
        key: bytes,
        *,
        initial_fcnt: Optional[int] = None,
        state_file: "Optional[str | os.PathLike[str]]" = None,
        allow_plain: bool = False,
        on_reject: Optional[Callable[[str, bytes], None]] = None,
        shares: "Optional[SecureTransport]" = None,
        app: Optional[AppKeys] = None,
    ) -> None:
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESCCM
        except ImportError as exc:  # pragma: no cover - depends on the environment
            raise ImportError("SecureTransport needs cryptography: pip install cryptography") from exc
        if len(key) != KEY_LEN:
            raise ValueError(f"key must be {KEY_LEN} bytes")
        self._aead = AESCCM(bytes(key), tag_length=MIC_LEN)
        self._inner = inner
        self._allow_plain = allow_plain
        self._on_reject = on_reject
        self._receiver: Optional[Receiver] = None
        #: Task B4.5: a second radio of the same node seals under the first
        #: one's counter (one node, one FCNT, or two frames share a nonce).
        self._shares = shares
        self.app = app if shares is None else shares.app
        # [MEM] recent REQUEST exchanges (peer, message id) | 端: device/controller | 上限: APP_EXCHANGES 16 | 每项: 3 B
        self._exchanges: dict[tuple[int, int], None] = {} if shares is None else shares._exchanges
        #: ``awaits_request_answer(node, message_id)``: does the controller still
        #: wait on that REQUEST or query (``Controller.awaits_request_answer``)?
        #: Its plain REPLY is then dropped as ``app_missing`` (receive side).
        self.awaits_request_answer: Optional[Callable[[int, int], bool]] = None
        self._lock = threading.Lock()
        self._state_path = (
            None if state_file is None else os.path.expanduser(os.fspath(state_file))
        )
        if self._state_path is not None:
            os.makedirs(os.path.dirname(os.path.abspath(self._state_path)), exist_ok=True)
        #: Why the state file was ignored at start-up (``None`` when it was read or absent).
        self.state_error: Optional[str] = None
        stored_tx, stored_rx = self._load_state()
        seed = int(time.time()) if initial_fcnt is None else initial_fcnt
        self._fcnt = max(seed, stored_tx)
        self._reserved = 0
        self._rx_dirty = False
        self._rx_flushed_at = 0.0
        self._timer: Optional[threading.Timer] = None
        # [MEM] replay windows | 端: controller/device | 上限: 256 个发送方 | 每项: 9 B
        self._windows: dict[int, ReplayWindow] = {
            sender: ReplayWindow.restored(last) for sender, last in stored_rx.items()
        }
        self.stats = {
            "sealed": 0,
            "opened": 0,
            "auth_failed": 0,
            "replayed": 0,
            "plain_dropped": 0,
            "app_sealed": 0,
            "app_opened": 0,
            "app_failed": 0,
            "app_missing": 0,
            "app_no_key": 0,
        }
        if self._state_path is not None:
            self._save_state(self._fcnt + TX_RESERVE)
        inner.set_receiver(self._on_frame)

    @property
    def fcnt(self) -> int:
        """Counter of the last frame sent."""

        return self._fcnt

    def set_receiver(self, callback: Receiver) -> None:
        self._receiver = callback

    def mark(self, sender: int) -> Optional[int]:
        """Highest FCNT accepted from ``sender``, or ``None``."""

        with self._lock:
            window = self._windows.get(sender)
            return None if window is None else window.last

    def reboot(self, marks: Optional[dict[int, int]] = None) -> None:
        """A device powers back on with the replay marks it persisted (sender -> FCNT).

        Those windows come back as ``ReplayWindow.restored`` (every counter up
        to the mark refused); every other sender's window was in RAM and
        starts empty. A device persists the controller's mark only (spec
        section 5, ``NodeStack.boot``).
        """

        with self._lock:
            self._windows = {sender: ReplayWindow.restored(last) for sender, last in (marks or {}).items()}

    def send(self, data: bytes) -> None:
        self._inner.send(self.seal_frame(data))

    def seal_frame(self, data: bytes) -> bytes:
        """Seal a plain frame under this node's next FCNT without sending it.

        For a layer below that originates frames of its own (a mesh relay
        re-sealing a PROBE, reporting a broken route): it must draw on this
        one persisted counter, never on a second one, or two frames would
        share a nonce.
        """

        if self._shares is not None:
            return self._shares.seal_frame(data)
        with self._lock:
            self._fcnt += 1
            if self._state_path is not None and self._fcnt > self._reserved:
                self._save_state(self._fcnt + TX_RESERVE)
            if self.app is not None:
                data = self._app_seal(bytes(data), self._fcnt)
            sealed = seal(self._aead, data, self._fcnt)
            self.stats["sealed"] += 1
        return sealed

    def sealed_length(self, plain: bytes) -> int:
        """Bytes on air for ``plain`` (above this layer's layout), app layer included."""

        kind = plain[0] & ~ROUTED & 0xFF if plain else 0
        extra = APP_OVERHEAD if self.app is not None and kind in APP_KINDS else 0
        return len(plain) + HEADER + MIC_LEN + extra

    # ------------------------------------------------------- application layer

    def _remember(self, peer: int, frame: bytes) -> None:
        exchanges = self._exchanges
        exchanges.pop((peer, frame[4] << 8 | frame[3]), None)
        exchanges[(peer, frame[4] << 8 | frame[3])] = None
        while len(exchanges) > APP_EXCHANGES:
            exchanges.pop(next(iter(exchanges)))

    def _carries_app(self, frame: bytes, peer: int) -> bool:
        kind = frame[0]
        if kind not in APP_KINDS or len(frame) != APP_KINDS[kind]:
            return False
        return kind != FrameKind.REPLY or (peer, frame[4] << 8 | frame[3]) in self._exchanges

    def _awaited(self, frame: bytes) -> bool:
        """A plain REPLY answering a REQUEST or query the controller still waits on (review B1).

        Receive side only: ``_exchanges`` remembers the last 16 exchanges of
        all nodes, and a confirming request asks with a new id every round, so
        its own ids fall out of it; a relay holding only the network key could
        then forge its answer. The controller knows what it still waits on.
        """

        hook = self.awaits_request_answer
        if hook is None and self._shares is not None:
            hook = self._shares.awaits_request_answer
        if hook is None or frame[0] != FrameKind.REPLY or len(frame) != APP_KINDS[FrameKind.REPLY]:
            return False
        return hook(frame[1], frame[4] << 8 | frame[3])

    def _app_seal(self, data: bytes, fcnt: int) -> bytes:
        """The frame with its application payload sealed under ``fcnt`` (lock held)."""

        split = _split(data)
        if split is None or len(split[0]) < 5:
            return data
        frame, trailer = split
        if not self._carries_app(frame, frame[2]):
            return data
        app = self.app
        node = app.node_of(frame[1], frame[2])
        version = app.current.get(node)
        key = None if version is None else app.key(node, version)
        if key is None:
            self.stats["app_no_key"] += 1  # goes without: the receiver drops it
            return data
        if frame[0] in _EXCHANGES:
            self._remember(frame[2], frame)
        head = bytes([frame[0] | APP]) + frame[1:5] + bytes([version])
        if frame[0] in _CLEAR_KINDS:
            head += frame[5:]
            body = self._app_aead(key).encrypt(_app_nonce(head, fcnt), b"", head)
        else:
            body = self._app_aead(key).encrypt(_app_nonce(head, fcnt), frame[5:], head)
        self.stats["app_sealed"] += 1
        return _join(head + body, trailer)

    def _app_open(self, plain: bytes, fcnt: int) -> tuple[Optional[bytes], Optional[str]]:
        """``(frame above this layer, None)``, or ``(None, the stats counter saying why it is dropped)``."""

        split = _split(plain)
        if split is None or len(split[0]) < 5:
            return None, "app_failed"
        frame, trailer = split
        if not frame[0] & APP:
            if self._carries_app(frame, frame[1]) or self._awaited(frame):
                return None, "app_missing"
            return plain, None
        kind = frame[0] & ~APP & 0xFF
        if kind not in APP_KINDS or len(frame) != APP_KINDS[kind] + APP_OVERHEAD:
            return None, "app_failed"
        node = self.app.node_of(frame[1], frame[2])
        key = self.app.key(node, frame[5])
        if key is None:
            return None, "app_no_key"
        # The clear payload is part of the AAD; the nonce takes the first 6 B either way.
        aad = frame[:-MIC_LEN] if kind in _CLEAR_KINDS else frame[:6]
        try:
            payload = self._app_aead(key).decrypt(_app_nonce(frame[:6], fcnt), frame[len(aad):], aad)
        except Exception:  # noqa: BLE001 - cryptography.exceptions.InvalidTag
            return None, "app_failed"
        if kind in _CLEAR_KINDS:
            payload = frame[6:-MIC_LEN]
        opened = bytes([kind]) + frame[1:5] + payload
        if kind in _EXCHANGES:
            self._remember(frame[1], opened)
        self.stats["app_opened"] += 1
        return _join(opened, trailer), None

    def _app_aead(self, key: bytes):
        from cryptography.hazmat.primitives.ciphers.aead import AESCCM

        return AESCCM(key, tag_length=MIC_LEN)

    # ------------------------------------------------------------ persistence

    def start(self) -> None:
        """No-op; lets a session start and stop this layer like a port."""

    def flush(self) -> None:
        """Write the receive-side replay marks now (no-op without ``state_file``)."""

        with self._lock:
            if self._state_path is not None and self._rx_dirty:
                self._save_state()

    def close(self) -> None:
        timer, self._timer = self._timer, None
        if timer is not None:
            timer.cancel()
        self.flush()

    def _load_state(self) -> tuple[int, dict[int, int]]:
        if self._state_path is None:
            return 0, {}
        try:
            with open(self._state_path, encoding="utf-8") as handle:
                state = json.load(handle)
            tx = int(state.get("tx", 0))
            rx = {
                int(sender): int(last)
                for sender, last in state.get("rx", {}).items()
                if 0 <= int(sender) <= 0xFF
            }
        except FileNotFoundError:
            return 0, {}
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            self.state_error = f"{type(exc).__name__}: {exc}"
            return 0, {}
        return tx, rx

    def _save_state(self, reserved: Optional[int] = None) -> None:
        """Atomically write the reserved counter bound and the replay marks.

        A new ``reserved`` bound takes effect only once it is on disk: after a
        failed write the next send must try to reserve again, not go out under
        a counter that a restart would hand out a second time.
        """

        reserved = self._reserved if reserved is None else reserved
        state = {
            "tx": reserved,
            "rx": {
                str(sender): window.last
                for sender, window in self._windows.items()
                if window.last is not None
            },
        }
        write_json(self._state_path, state)  # fsynced, the rename too (review B3)
        self._reserved = reserved
        self._rx_dirty = False
        self._rx_flushed_at = time.monotonic()

    def _note_accepted(self) -> None:
        """Schedule a write of the replay marks; call with the lock held."""

        if self._state_path is None:
            return
        self._rx_dirty = True
        wait = RX_FLUSH_INTERVAL - (time.monotonic() - self._rx_flushed_at)
        if wait > 0:
            # Too soon to write again. Make sure the marks still reach the
            # file if traffic stops, so a crash loses about one interval.
            if self._timer is None:
                self._timer = threading.Timer(wait, self._flush_later)
                self._timer.daemon = True
                self._timer.start()
            return
        self._write_marks()

    def _flush_later(self) -> None:
        with self._lock:
            self._timer = None
            self._write_marks()

    def _write_marks(self) -> None:
        if not self._rx_dirty:
            return
        try:
            self._save_state()
        except OSError as exc:
            # Receiving must go on; the next accepted frame retries the write.
            self.state_error = f"{type(exc).__name__}: {exc}"

    def _on_frame(self, raw: bytes) -> None:
        if not raw:
            return
        if not raw[0] & SECURE:
            if not self._allow_plain:
                self._reject("plain_dropped", "unsealed frame on a secure link", raw)
                return
            self._deliver(raw)
            return
        try:
            plain, fcnt = open_sealed(self._aead, raw)
        except ValueError as exc:
            self._reject("auth_failed", str(exc), raw)
            return
        except Exception:  # noqa: BLE001 - cryptography.exceptions.InvalidTag
            self._reject("auth_failed", "message authentication failed", raw)
            return
        # Under the lock: a send on another thread may be writing the state
        # file, which iterates over the windows.
        with self._lock:
            window = self._windows.setdefault(plain[1], ReplayWindow())
            fresh = window.check(fcnt)
            dropped = None
            if fresh and self.app is not None:
                # Before the window moves: a frame failing the inner MIC
                # must not push the sender's counter forward.
                opened, dropped = self._app_open(plain, fcnt)
                fresh = opened is not None
                plain = opened or plain
            if fresh:
                window.accept(fcnt)
                self.stats["opened"] += 1
                self._note_accepted()
        if dropped is not None:
            self._reject(dropped, "application layer: " + dropped, raw)
            return
        if not fresh:
            self._reject("replayed", f"replayed frame counter {fcnt}", raw)
            return
        self._deliver(plain)

    def _deliver(self, plain: bytes) -> None:
        if self._receiver is not None:
            self._receiver(plain)

    def _reject(self, counter: str, reason: str, raw: bytes) -> None:
        self.stats[counter] += 1
        if self._on_reject is not None:
            self._on_reject(reason, bytes(raw))
