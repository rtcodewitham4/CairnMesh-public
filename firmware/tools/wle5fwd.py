"""Probe a wle5node mesh image by flooding one sealed REQUEST through the gateway bridge.

The U083 gateway runs ``cmd/gateway`` (``make flash CMD=gateway``). This host tool drives it
through ``cairnmesh/bridge.py`` in the same way as ``bridge_bench.py``:

    flock -n ~/.cache/cairnmesh/bench.lock \
      .venv/bin/python firmware/tools/wle5fwd.py /dev/ttyACM0 --target 1

The bench lock is the caller's responsibility. This script never changes the radio settings, so
the gateway's -9 dBm default stays in effect. It sends at most five unrouted REQUESTs, at least
10 s apart, then listens for replies and relayed copies. Every received frame is printed with
KIND, SEQ, RSSI, host monotonic time and TX. The final summary is at most ten lines.

By default, keys are read only from ``~/.cache/cairnmesh/bench-keys/bench-keys.txt``. That file
has exactly two lines, ``net=<16-byte hex>`` and ``app=<16-byte hex>``. ``--test-keys`` instead
uses the public ``tests/vectors`` keys from the same source as
``provision.py --test-keys`` and does not read the bench-key file. The two sources are mutually
exclusive. E77 currently uses the vectors keys, so testing E77 requires ``--test-keys``. Logs
and the final summary label the selected source as ``vectors`` or ``bench-file``. Key material
is never printed or written.

The probe uses its own sender id, not the controller's 0x42. Replay windows are keyed by SENDER,
and the firmware persists only the controller's mark, so this keeps the probe's FCNT out of the
controller's replay state. Device dedup records are also per sender, so the probe consumes one
of their bounded sender slots. ``--fcnt-start`` makes the counter deterministic; within a run it
increments once per transmitted REQUEST.

Reading the result: if ``--target`` is the E77's own node id, the E77 only executes the
REQUEST and answers; it does not relay it, so "forward: not observed" is expected. To test
forwarding, pick a target id other than the E77's (it may be a node that does not exist). Executing
a REQUEST also appends device dedup records to flash (a few 8-byte entries per request, whatever
the sender id). Keep the default ``--sender``: the device keeps at most 2 sender slots, and a
different value can evict the controller's. The default action 0x04 exists only in a testbed
image built with ``WLE5_BENCH_ACTIONS=1``; production and testfilter-only images answer ERROR.
To test both the receive filter and action 0x04, build with
``WLE5_TESTFILTER=1 WLE5_BENCH_ACTIONS=1``.

``--listen-only`` sends no REQUEST. ``--dry-run`` constructs the sealed frames and prints only
their KIND and length; it does not open a serial port. The gateway bridge deliberately has no
transmit-power control, so this tool cannot raise the board's -9 dBm default.
"""

from __future__ import annotations

import argparse
import math
import os
import secrets
import sys
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional, Sequence

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.dirname(__file__))

from cairnmesh.bridge import TX_OK, GatewayBridge, TxDone  # noqa: E402
from cairnmesh.frames import APP, FrameKind, RequestFrame  # noqa: E402
from cairnmesh.secure import MIC_LEN, SecureTransport, AppKeys, open_sealed, transmitter  # noqa: E402
from cairnmesh.serial_link import SerialTransport  # noqa: E402
from serial_bench import Port  # noqa: E402

DEFAULT_CONTROLLER_ID = 0x42
PROBE_SENDER_ID = 0xE1
DEFAULT_TARGET_ID = 0x01
DEFAULT_ACTION = 4  # testbed action only (WLE5_BENCH_ACTIONS=1); otherwise ERROR
DEFAULT_LISTEN_SECONDS = 10.0
MIN_INTERVAL_SECONDS = 10.0
MAX_REQUESTS = 5
TX_RESULT_TIMEOUT = 2.0
KEY_DIR = Path.home() / ".cache" / "cairnmesh" / "bench-keys"
KEY_FILE = "bench-keys.txt"
KEY_SOURCE_VECTORS = "vectors"
KEY_SOURCE_BENCH_FILE = "bench-file"


@dataclass(frozen=True, repr=False)
class BenchKeys:
    """Bench keys held in memory; the repr intentionally reveals neither key."""

    network: bytes = field(repr=False)
    app: bytes = field(repr=False)

    def __repr__(self) -> str:
        return "BenchKeys(<redacted>)"


@dataclass(frozen=True)
class OutgoingRequest:
    sender: int
    target: int
    sequence: int
    epoch: int
    wire: bytes

    @property
    def message_id(self) -> int:
        return self.epoch << 8 | self.sequence


@dataclass(frozen=True)
class HeardFrame:
    at: float
    radio: int
    rssi: float
    kind: Optional[int]
    kind_name: str
    sender: Optional[int]
    target: Optional[int]
    sequence: Optional[int]
    epoch: Optional[int]
    tx: Optional[int]
    fcnt: Optional[int]
    app_layer: bool
    verified: bool

    @property
    def key(self) -> Optional[tuple[int, int]]:
        if self.sender is None or self.sequence is None or self.epoch is None:
            return None
        return self.sender, self.epoch << 8 | self.sequence


def load_keys(directory: Path = KEY_DIR) -> BenchKeys:
    """Read exactly ``net=<hex>`` and ``app=<hex>`` from ``bench-keys.txt``."""

    path = Path(directory) / KEY_FILE
    try:
        text = path.read_text(encoding="ascii")
    except UnicodeDecodeError as exc:
        raise ValueError(f"bench key file must be ASCII: {path}") from exc
    lines = text.strip().splitlines()
    if len(lines) != 2:
        raise ValueError(f"bench key file must contain exactly two lines: {path}")
    values = {}
    for line in lines:
        name, separator, value = line.strip().partition("=")
        if not separator or name not in ("net", "app") or name in values:
            raise ValueError(f"invalid bench key file format: {path}")
        values[name] = value
    found = []
    for name in ("net", "app"):
        value = values.get(name)
        if value is None or len(value) != 32:
            raise ValueError(f"bench key {name} must be exactly 32 hex digits: {path}")
        try:
            found.append(bytes.fromhex(value))
        except ValueError:
            raise ValueError(f"bench key {name} must be exactly 32 hex digits: {path}") from None
    return BenchKeys(network=found[0], app=found[1])


def load_test_keys() -> BenchKeys:
    """Return the public tests/vectors keys used by ``provision.py --test-keys``."""

    from cairnmesh.vectors import APP_KEY, NETWORK_KEY

    return BenchKeys(network=NETWORK_KEY, app=APP_KEY)


class _CaptureTransport:
    def __init__(self) -> None:
        self.receiver = None

    def set_receiver(self, callback) -> None:
        self.receiver = callback

    def send(self, data: bytes) -> None:
        pass


def build_requests(
    keys: BenchKeys,
    *,
    target: int = DEFAULT_TARGET_ID,
    count: int = 1,
    action: int = DEFAULT_ACTION,
    value: int = 0,
    sender: int = PROBE_SENDER_ID,
    initial_fcnt: Optional[int] = None,
    sequence: Optional[int] = None,
    epoch: Optional[int] = None,
) -> list[OutgoingRequest]:
    """Construct ``count`` unrouted, network- and application-sealed REQUEST frames."""

    if not 0 <= sender <= 0xFE or not 0 <= target <= 0xFE:
        raise ValueError("sender and target must be node ids in 0..254")
    if sender == DEFAULT_CONTROLLER_ID:
        raise ValueError("sender must differ from the controller id 0x42")
    if sender == target:
        raise ValueError("sender and target must differ")
    if not 1 <= count <= MAX_REQUESTS:
        raise ValueError(f"count must be 1..{MAX_REQUESTS}")
    if not 0 <= action <= 0xFF or not 0 <= value <= 0xFF:
        raise ValueError("action and value must be 0..255")

    first_sequence = secrets.randbelow(0x100) if sequence is None else sequence & 0xFF
    request_epoch = int(time.time()) & 0xFF if epoch is None else epoch & 0xFF
    first_fcnt = int(time.time()) if initial_fcnt is None else initial_fcnt
    if not 0 <= first_fcnt <= 0xFFFFFFFF or first_fcnt + count > 0xFFFFFFFF:
        raise ValueError("initial_fcnt must leave room for every REQUEST")
    secure = SecureTransport(
        _CaptureTransport(),
        keys.network,
        initial_fcnt=first_fcnt,
        app=AppKeys({target: keys.app}),
    )
    requests = []
    for offset in range(count):
        seq = (first_sequence + offset) & 0xFF
        plain = RequestFrame(
            sender_id=sender,
            target_id=target,
            sequence=seq,
            action=action,
            value=value,
            epoch=request_epoch,
        ).encode()
        requests.append(
            OutgoingRequest(
                sender=sender,
                target=target,
                sequence=seq,
                epoch=request_epoch,
                wire=secure.seal_frame(plain),
            )
        )
    return requests


def _frame_name(kind: Optional[int]) -> str:
    if kind is None:
        return "?"
    try:
        return FrameKind(kind).name
    except ValueError:
        return f"0x{kind:02x}"


class FrameDecoder:
    """Verify the network MIC, then the application MIC when the frame carries one."""

    def __init__(self, network_key: bytes, app_key: Optional[bytes] = None, target: int = DEFAULT_TARGET_ID) -> None:
        from cryptography.hazmat.primitives.ciphers.aead import AESCCM

        self._aead = AESCCM(network_key, tag_length=MIC_LEN)
        self._app = None
        if app_key is not None:
            self._app = SecureTransport(
                _CaptureTransport(),
                network_key,
                initial_fcnt=0,
                app=AppKeys({target: app_key}),
            )
        self._lock = threading.Lock()

    def classify(self, raw: bytes, *, at: float, radio: int, rssi: float) -> HeardFrame:
        wire_kind = raw[0] & 0x1F if raw else None
        verified = False
        app_layer = False
        fields = None
        fcnt = None
        if raw:
            try:
                fields, fcnt = open_sealed(self._aead, raw)
                app_layer = bool(fields[0] & APP)
                if app_layer:
                    if self._app is None:
                        app_ok = False
                    else:
                        with self._lock:
                            opened, _ = self._app._app_open(fields, fcnt)  # noqa: SLF001
                        app_ok = opened is not None
                else:
                    app_ok = True
                verified = app_ok
            except Exception:  # noqa: BLE001 - malformed, unsealed, wrong key, or bad MIC
                fields = None
                verified = False

        if fields is not None and len(fields) >= 5:
            kind = fields[0] & 0x1F
            sender, target, sequence, epoch = fields[1:5]
        elif len(raw) >= 9:
            kind = wire_kind
            sender, target, sequence, epoch = raw[1], raw[2], raw[3], raw[4]
        else:
            kind = wire_kind
            sender = raw[1] if len(raw) > 1 else None
            target = raw[2] if len(raw) > 2 else None
            sequence = raw[3] if len(raw) > 3 else None
            epoch = raw[4] if len(raw) > 4 else None
        return HeardFrame(
            at=at,
            radio=radio,
            rssi=rssi,
            kind=kind,
            kind_name=_frame_name(kind),
            sender=sender,
            target=target,
            sequence=sequence,
            epoch=epoch,
            tx=transmitter(raw) if len(raw) >= 9 else None,
            fcnt=fcnt,
            app_layer=app_layer,
            verified=verified,
        )


def classify_frame(
    raw: bytes,
    network_key: bytes,
    *,
    at: float,
    radio: int,
    rssi: float,
    app_key: Optional[bytes] = None,
    target: int = DEFAULT_TARGET_ID,
) -> HeardFrame:
    """Convenience wrapper around :class:`FrameDecoder`."""

    return FrameDecoder(network_key, app_key, target).classify(raw, at=at, radio=radio, rssi=rssi)


class FrameCollector:
    """Thread-safe host-side record of every bridge RX callback."""

    def __init__(
        self,
        network_key: bytes,
        app_key: Optional[bytes],
        *,
        target: int = DEFAULT_TARGET_ID,
        clock: Callable[[], float] = time.monotonic,
        on_event: Optional[Callable[[HeardFrame], None]] = None,
    ) -> None:
        self._decoder = FrameDecoder(network_key, app_key, target)
        self._clock = clock
        self._on_event = on_event
        self._lock = threading.Lock()
        self._heard: list[HeardFrame] = []

    def heard(self, radio: int, raw: bytes, rssi: float) -> None:
        event = self._decoder.classify(
            bytes(raw),
            at=self._clock(),
            radio=radio,
            rssi=rssi,
        )
        with self._lock:
            self._heard.append(event)
        if self._on_event is not None:
            self._on_event(event)

    def snapshot(self) -> list[HeardFrame]:
        with self._lock:
            return list(self._heard)


def observed_forwards(frames: Iterable[HeardFrame], requests: Sequence[OutgoingRequest]) -> list[HeardFrame]:
    """Relayed REQUEST copies: same sender/message id, a different authenticated TX."""

    wanted = {(request.sender, request.message_id) for request in requests}
    return [
        frame
        for frame in frames
        if frame.verified
        and frame.app_layer
        and frame.kind == FrameKind.REQUEST
        and frame.key in wanted
        and frame.tx is not None
        and frame.tx != frame.sender
    ]


def observed_echoes(frames: Iterable[HeardFrame], requests: Sequence[OutgoingRequest]) -> list[HeardFrame]:
    """Our own REQUEST heard by the gateway's other radio, not a forward."""

    wanted = {(request.sender, request.message_id) for request in requests}
    return [
        frame
        for frame in frames
        if frame.verified
        and frame.app_layer
        and frame.kind == FrameKind.REQUEST
        and frame.key in wanted
        and frame.tx == frame.sender
    ]


def observed_replies(frames: Iterable[HeardFrame], requests: Sequence[OutgoingRequest]) -> list[HeardFrame]:
    """REPLY copies whose sender and echoed message id match one of our requests."""

    wanted = {
        (request.target, request.sender, request.message_id)
        for request in requests
    }
    return [
        frame
        for frame in frames
        if frame.verified
        and frame.app_layer
        and frame.kind == FrameKind.REPLY
        and frame.sender is not None
        and frame.target is not None
        and frame.key is not None
        and (frame.sender, frame.target, frame.key[1]) in wanted
    ]


@dataclass
class RunStats:
    attempted: int = 0
    tx_ok: int = 0


class ProbeRun:
    """Drive one gateway bridge and wait through the listen windows."""

    def __init__(
        self,
        bridge,
        collector: FrameCollector,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        tx_timeout: float = TX_RESULT_TIMEOUT,
    ) -> None:
        self.bridge = bridge
        self.collector = collector
        self.clock = clock
        self.sleep = sleep
        self.tx_timeout = tx_timeout
        self._done: list[TxDone] = []
        self._lock = threading.Lock()
        for index, port in enumerate(bridge.ports):
            port.set_receiver(lambda frame, rssi, i=index: collector.heard(i, frame, rssi))
        bridge.on_tx_done = self._tx_done

    def _tx_done(self, done: TxDone) -> None:
        with self._lock:
            self._done.append(done)

    def _wait_until(self, deadline: float) -> None:
        while (remaining := deadline - self.clock()) > 0:
            self.sleep(min(remaining, 0.05))

    def _wait_for_tx(self, previous: int) -> Optional[TxDone]:
        deadline = self.clock() + self.tx_timeout
        while self.clock() < deadline:
            with self._lock:
                if len(self._done) > previous:
                    return self._done[previous]
            self.sleep(0.02)
        return None

    def run(
        self,
        requests: Sequence[OutgoingRequest],
        *,
        radio: int,
        interval: float,
        listen: float,
        listen_only: bool,
    ) -> RunStats:
        if not 1 <= len(requests) <= MAX_REQUESTS:
            raise ValueError(f"request count must be 1..{MAX_REQUESTS}")
        if radio not in (0, 1):
            raise ValueError("radio must be 0 or 1")
        if not math.isfinite(interval) or interval < MIN_INTERVAL_SECONDS:
            raise ValueError(f"interval must be finite and at least {MIN_INTERVAL_SECONDS:g} s")
        if not math.isfinite(listen) or listen < 0:
            raise ValueError("listen must be finite and >= 0")
        stats = RunStats()
        if listen_only:
            self.sleep(listen)
            return stats

        last_send = None
        for request in requests:
            if last_send is not None:
                self._wait_until(last_send + interval)
            with self._lock:
                previous = len(self._done)
            stats.attempted += 1
            self.bridge.ports[radio].send(request.wire)
            done = self._wait_for_tx(previous)
            if done is not None and done.result == TX_OK:
                stats.tx_ok += 1
            last_send = self.clock()
        self.sleep(listen)
        return stats


def heard_line(frame: HeardFrame, *, key_source: str = KEY_SOURCE_BENCH_FILE) -> str:
    missing = "-"
    return (
        f"heard time_ms={int(frame.at * 1000)} radio={frame.radio} "
        f"RSSI={frame.rssi:.0f} KIND={frame.kind_name} "
        f"SEQ={missing if frame.sequence is None else frame.sequence} "
        f"EPOCH={missing if frame.epoch is None else frame.epoch} "
        f"TX={missing if frame.tx is None else frame.tx} "
        f"verified={'yes' if frame.verified else 'no'} keys={key_source}"
    )


def summary_lines(
    frames: Sequence[HeardFrame],
    requests: Sequence[OutgoingRequest],
    stats: RunStats,
    *,
    listen_only: bool,
    key_source: str = KEY_SOURCE_BENCH_FILE,
) -> list[str]:
    active_requests = [] if listen_only else requests
    forwards = observed_forwards(frames, active_requests)
    echoes = observed_echoes(frames, active_requests)
    replies = observed_replies(frames, active_requests)
    kinds = Counter(
        frame.kind_name if frame.verified else f"{frame.kind_name}?"
        for frame in frames
    )
    kind_text = ",".join(f"{name}:{count}" for name, count in sorted(kinds.items())) or "none"
    forwarded = (
        "未观察到"
        if not forwards
        else "observed tx=" + ",".join(str(tx) for tx in sorted({frame.tx for frame in forwards}))
    )
    answered = (
        "未观察到"
        if not replies
        else "observed tx=" + ",".join(str(tx) for tx in sorted({frame.tx for frame in replies}))
    )
    request_count = 0 if listen_only else len(requests)
    return [
        f"wle5fwd summary keys={key_source}",
        f"requests planned={request_count} sent={stats.attempted} tx_ok={stats.tx_ok}",
        f"heard={len(frames)} kinds={kind_text}",
        f"request-copies echo={len(echoes)} other-tx={len(forwards)}",
        f"forward={forwarded}",
        f"reply={answered}",
    ]


def _request_count(text: str) -> int:
    value = int(text)
    if not 1 <= value <= MAX_REQUESTS:
        raise argparse.ArgumentTypeError(f"must be 1..{MAX_REQUESTS}")
    return value


def _interval(text: str) -> float:
    value = float(text)
    if not math.isfinite(value) or value < MIN_INTERVAL_SECONDS:
        raise argparse.ArgumentTypeError(f"must be finite and at least {MIN_INTERVAL_SECONDS:g} s")
    return value


def _nonnegative(text: str) -> float:
    value = float(text)
    if not math.isfinite(value) or value < 0:
        raise argparse.ArgumentTypeError("must be finite and >= 0")
    return value


def _positive(text: str) -> float:
    value = float(text)
    if not math.isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError("must be finite and > 0")
    return value


def _target(text: str) -> int:
    value = int(text, 0)
    if not 0 <= value <= 0xFE:
        raise argparse.ArgumentTypeError("must be a node id in 0..254")
    return value


def _byte(text: str) -> int:
    value = int(text, 0)
    if not 0 <= value <= 0xFF:
        raise argparse.ArgumentTypeError("must be 0..255")
    return value


def _sender(text: str) -> int:
    value = _target(text)
    if value == DEFAULT_CONTROLLER_ID:
        raise argparse.ArgumentTypeError("must differ from the controller id 0x42")
    return value


def _fcnt_start(text: str) -> int:
    value = int(text, 0)
    if not 0 <= value <= 0xFFFFFFFF:
        raise argparse.ArgumentTypeError("must be 0..4294967295")
    return value


def open_serial_bridge(port: str):
    transport = SerialTransport(Port(port))
    bridge = GatewayBridge(transport, on_error=lambda exc: print("error:", exc))
    transport.start()
    return bridge, transport


def main(
    argv: Optional[Sequence[str]] = None,
    *,
    bridge_factory: Optional[Callable[[str], tuple]] = None,
    key_dir: Path = KEY_DIR,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    parser = argparse.ArgumentParser(description="Flood a sealed REQUEST through the gateway bridge.")
    parser.add_argument("port", nargs="?", help="gateway bridge serial port (not needed with --dry-run)")
    parser.add_argument("--target", type=_target, default=DEFAULT_TARGET_ID, help="REQUEST target node id")
    parser.add_argument("--sender", type=_sender, default=PROBE_SENDER_ID, help="independent probe sender id")
    parser.add_argument("--fcnt-start", type=_fcnt_start, default=None, help="first FCNT base (default: Unix time)")
    parser.add_argument(
        "--action",
        type=_byte,
        default=DEFAULT_ACTION,
        help=f"REQUEST action (default {DEFAULT_ACTION}; 0x04 exists only in a WLE5 bench-action image)",
    )
    parser.add_argument("--value", type=_byte, default=0, help="REQUEST value (default 0)")
    parser.add_argument("--count", type=_request_count, default=1, help=f"REQUESTs to send, 1..{MAX_REQUESTS}")
    parser.add_argument(
        "--interval",
        type=_interval,
        default=MIN_INTERVAL_SECONDS,
        help=f"seconds between REQUESTs (minimum {MIN_INTERVAL_SECONDS:g})",
    )
    parser.add_argument(
        "--listen",
        type=_nonnegative,
        default=DEFAULT_LISTEN_SECONDS,
        help=f"seconds to listen after the last REQUEST (default {DEFAULT_LISTEN_SECONDS:g})",
    )
    parser.add_argument("--radio", type=int, choices=(0, 1), default=0, help="gateway radio that transmits")
    parser.add_argument("--listen-only", action="store_true", help="listen for --listen seconds and send no REQUEST")
    parser.add_argument("--test-keys", action="store_true", help="use public tests/vectors keys (required for E77)")
    parser.add_argument("--dry-run", action="store_true", help="construct the frames; do not touch serial")
    parser.add_argument("--status-timeout", type=_positive, default=2.0, help="seconds to wait for bridge STATUS")
    args = parser.parse_args(argv)

    if not args.dry_run and not args.port:
        parser.error("port is required unless --dry-run is used")

    try:
        if args.test_keys:
            keys = load_test_keys()
            key_source = KEY_SOURCE_VECTORS
        else:
            keys = load_keys(key_dir)
            key_source = KEY_SOURCE_BENCH_FILE
        requests = build_requests(
            keys,
            target=args.target,
            count=args.count,
            action=args.action,
            value=args.value,
            sender=args.sender,
            initial_fcnt=args.fcnt_start,
        )
    except (OSError, ValueError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 2

    if args.dry_run:
        for index, request in enumerate(requests, 1):
            print(f"dry-run keys={key_source} frame={index} KIND=REQUEST length={len(request.wire)}")
        return 0

    factory = bridge_factory or open_serial_bridge
    bridge, transport = factory(args.port)
    try:
        if bridge.ping(timeout=args.status_timeout) is None:
            print("FAIL: gateway STATUS did not answer", file=sys.stderr)
            return 1
        collector = FrameCollector(
            keys.network,
            keys.app,
            target=args.target,
            clock=clock,
            on_event=lambda event: print(heard_line(event, key_source=key_source), flush=True),
        )
        run = ProbeRun(bridge, collector, clock=clock, sleep=sleep)
        stats = run.run(
            requests,
            radio=args.radio,
            interval=args.interval,
            listen=args.listen,
            listen_only=args.listen_only,
        )
        frames = collector.snapshot()
    finally:
        transport.close()

    for line in summary_lines(
        frames,
        requests,
        stats,
        listen_only=args.listen_only,
        key_source=key_source,
    ):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
