"""Real-bench hidden-node probe: one E77 relay and two gateway radios.

Gateway radio 0 sends one application-sealed REQUEST. Gateway radio 1 is behind
``FilteredPort`` and may hear only TX=1 (the E77), not TX=0xE1 (the probe source).
The direct copy is therefore dropped at the software filter, while a copy
re-sealed by the E77 under TX=1 can reach the observer collector.

Run with the bench lock held by the caller, for example::

    CAIRN_TESTFILTER=1 flock -n ~/.cache/cairnmesh/bench.lock \
      .venv/bin/python -m tests.testbed.hidden_node_probe /dev/ttyACM0 \
      --topo 1-2,1-225 --test-keys

The topology is mandatory. ``--dry-run`` builds frames without opening a serial
port; ``--listen-only`` opens the bridge but sends no REQUEST. The probe never
changes radio settings or transmit power. It sends at most five REQUESTs, at
least ten seconds apart. The summary is at most ten lines and ends with one of
the three conservative conclusions documented in ``README.md``. Exit status is
0 for received, 1 for not received, 2 for unknown, and 64 for usage or
validation failure.
"""

from __future__ import annotations

import argparse
import math
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Optional, Sequence

from cryptography.hazmat.primitives.ciphers.aead import AESCCM

ROOT = Path(__file__).resolve().parents[2]
TOOLS = ROOT / "firmware" / "tools"
for _path in (str(ROOT), str(TOOLS)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import wle5fwd as W  # noqa: E402
from cairnmesh.frames import FrameKind  # noqa: E402
from cairnmesh.secure import MIC_LEN, open_sealed, transmitter  # noqa: E402

from .rxfilter import ENV, FilterError, FilteredPort, assert_no_active_filters  # noqa: E402
from .topo import parse_links, neighbours  # noqa: E402

DEFAULT_E77_ID = 0x01
DEFAULT_OBSERVER_ID = 0x02
DEFAULT_TARGET_ID = 0x09
SOURCE_TX = W.PROBE_SENDER_ID

RECEIVED = "隐藏节点经 E77 收到"
NOT_RECEIVED = "隐藏节点未收到"
UNKNOWN = "无法判断(密钥不符/没有帧)"

EXIT_RECEIVED = 0
EXIT_NOT_RECEIVED = 1
EXIT_UNKNOWN = 2
EXIT_USAGE = 64

UNKNOWN_LISTEN_ONLY = "listen-only"
UNKNOWN_SUSPECT = "suspect"
UNKNOWN_DIRECT_PASSED = "direct_passed"
UNKNOWN_NO_E77_KEY = "未证明与 E77 同钥"
UNKNOWN_NO_FRAMES = "无帧"
UNKNOWN_NOT_SENT = "未发送"


@dataclass(frozen=True)
class ProbeAnalysis:
    conclusion: str
    direct_dropped: int
    direct_passed: int
    forwards: int
    radio0_heard: int
    radio0_matching: int
    e77_app_verified: int
    filter_unauth: int
    suspect: Optional[str]
    unknown_reason: Optional[str]


def _request_identities(
    requests: Sequence[W.OutgoingRequest], network_key: bytes
) -> tuple[set[tuple[int, int, int, int]], dict[tuple[int, int], int]]:
    """Map each generated request to the fields available in a filter drop record.

    ``DropRecord`` deliberately carries no SEQUENCE/EPOCH. The network FCNT is
    unique for every generated request, so it safely ties the owned originals
    to their drop records without changing the filter's privacy boundary.
    """

    aead = AESCCM(bytes(network_key), tag_length=MIC_LEN)
    identities = set()
    fcnts = {}
    for request in requests:
        try:
            plain, fcnt = open_sealed(aead, request.wire)
        except Exception:  # wrong key: no drop identity can be trusted
            continue
        identities.add((request.sender, transmitter(request.wire), fcnt, plain[0]))
        fcnts[(request.sender, request.message_id)] = fcnt
    return identities, fcnts


def analyze(
    frames: Sequence[W.HeardFrame],
    drops: Iterable[object],
    requests: Sequence[W.OutgoingRequest],
    stats: W.RunStats,
    *,
    network_key: bytes,
    e77_id: int = DEFAULT_E77_ID,
    listen_only: bool = False,
    filter_status: Optional[dict] = None,
) -> ProbeAnalysis:
    """Count the hidden-node evidence and choose one of the three conclusions."""

    active_requests = [] if listen_only else list(requests)
    wanted = {(request.sender, request.message_id) for request in active_requests}
    identities, request_fcnts = _request_identities(active_requests, network_key)
    drop_list = list(drops)

    direct_dropped = sum(
        1
        for record in drop_list
        if (
            getattr(record, "sender", None),
            getattr(record, "tx", None),
            getattr(record, "fcnt", None),
            getattr(record, "kind", None),
        )
        in identities
        and getattr(record, "tx", None) == SOURCE_TX
    )

    matching_radio1 = [
        frame
        for frame in frames
        if frame.radio == 1
        and frame.verified
        and frame.app_layer
        and frame.kind == FrameKind.REQUEST
        and frame.key in wanted
        and frame.fcnt is not None
        and frame.fcnt == request_fcnts.get(frame.key)
    ]
    direct_passed = sum(1 for frame in matching_radio1 if frame.tx == SOURCE_TX)
    forwards = sum(1 for frame in matching_radio1 if frame.tx == e77_id)
    radio0_matching = sum(
        1
        for frame in frames
        if frame.radio == 0
        and frame.verified
        and frame.app_layer
        and frame.kind == FrameKind.REQUEST
        and frame.key in wanted
        and frame.fcnt is not None
        and frame.fcnt == request_fcnts.get(frame.key)
    )
    e77_app_verified = sum(
        1
        for frame in frames
        if frame.radio in (0, 1)
        and frame.verified
        and frame.app_layer
        and frame.tx == e77_id
    )

    status = filter_status or {}
    suspect = status.get("suspect")
    filter_unauth = int(status.get("unauth", 0))

    unknown_reason = None
    if listen_only or not active_requests:
        conclusion = UNKNOWN
        unknown_reason = UNKNOWN_LISTEN_ONLY
    elif stats.attempted == 0:
        conclusion = UNKNOWN
        unknown_reason = UNKNOWN_NOT_SENT
    elif suspect:
        conclusion = UNKNOWN
        unknown_reason = UNKNOWN_SUSPECT
    elif direct_passed:
        # The observer saw the source directly: the controlled hidden-node
        # setup is not in force, so even a forward cannot prove exclusivity.
        conclusion = UNKNOWN
        unknown_reason = UNKNOWN_DIRECT_PASSED
    elif forwards:
        conclusion = RECEIVED
    elif not frames and not drop_list:
        conclusion = UNKNOWN
        unknown_reason = UNKNOWN_NO_FRAMES
    elif e77_app_verified == 0:
        conclusion = UNKNOWN
        unknown_reason = UNKNOWN_NO_E77_KEY
    elif direct_dropped:
        conclusion = NOT_RECEIVED
    else:
        conclusion = UNKNOWN
        unknown_reason = UNKNOWN_NO_FRAMES

    return ProbeAnalysis(
        conclusion=conclusion,
        direct_dropped=direct_dropped,
        direct_passed=direct_passed,
        forwards=forwards,
        radio0_heard=sum(1 for frame in frames if frame.radio == 0),
        radio0_matching=radio0_matching,
        e77_app_verified=e77_app_verified,
        filter_unauth=filter_unauth,
        suspect=str(suspect) if suspect else None,
        unknown_reason=unknown_reason,
    )


class _FilteredBridgeView:
    """Expose radio0 raw and radio1 wrapped, without changing the real bridge."""

    def __init__(self, bridge, filtered_radio1) -> None:
        self._bridge = bridge
        self.ports = (bridge.ports[0], filtered_radio1)

    @property
    def on_tx_done(self):
        return self._bridge.on_tx_done

    @on_tx_done.setter
    def on_tx_done(self, callback) -> None:
        self._bridge.on_tx_done = callback


class _ProbeFrameCollector(W.FrameCollector):
    """``wle5fwd`` collector with the E77's own application key added."""

    def __init__(self, network_key: bytes, app_key: bytes, *, target: int, e77_id: int, **kwargs):
        super().__init__(network_key, app_key, target=target, **kwargs)
        if self._decoder._app is not None:  # noqa: SLF001 - extend the reused decoder
            self._decoder._app.app.add(e77_id, app_key)  # noqa: SLF001


def validate_topology(topo: str, *, e77_id: int, observer_id: int) -> None:
    """Raise a plain validation error unless radio1 hears exactly the E77."""

    links = parse_links(topo)
    heard = set(neighbours(links, observer_id))
    if heard != {e77_id}:
        raise ValueError(
            f"observer {observer_id} must hear exactly TX={e77_id} in --topo; "
            f"it hears {sorted(heard)}"
        )


class HiddenNodeProbe:
    """Own the radio-1 filter, collector and the guarded radio-0 sender."""

    def __init__(
        self,
        bridge,
        *,
        keys: W.BenchKeys,
        topo: str,
        e77_id: int = DEFAULT_E77_ID,
        observer_id: int = DEFAULT_OBSERVER_ID,
        target: int = DEFAULT_TARGET_ID,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        validate_topology(topo, e77_id=e77_id, observer_id=observer_id)

        self.keys = keys
        self.topo = str(topo)
        self.e77_id = e77_id
        self.observer_id = observer_id
        self.target = target
        self.filtered_radio1 = FilteredPort(
            bridge.ports[1],
            key=keys.network,
            node=observer_id,
            topo=topo,
        )
        try:
            self.collector = _ProbeFrameCollector(
                keys.network,
                keys.app,
                target=target,
                e77_id=e77_id,
                clock=clock,
                on_event=lambda event: print(
                    W.heard_line(event, key_source=self.key_source), flush=True
                ),
            )
            self.runner = W.ProbeRun(
                _FilteredBridgeView(bridge, self.filtered_radio1),
                self.collector,
                clock=clock,
                sleep=sleep,
            )
        except BaseException:
            self.filtered_radio1.close()
            assert_no_active_filters()
            raise

    key_source = W.KEY_SOURCE_BENCH_FILE

    def run(
        self,
        requests: Sequence[W.OutgoingRequest],
        *,
        interval: float,
        listen: float,
        listen_only: bool,
    ) -> W.RunStats:
        try:
            return self.runner.run(
                requests,
                radio=0,
                interval=interval,
                listen=listen,
                listen_only=listen_only,
            )
        except BaseException:
            self.close()
            raise

    def analysis(
        self,
        requests: Sequence[W.OutgoingRequest],
        stats: W.RunStats,
        *,
        listen_only: bool,
    ) -> ProbeAnalysis:
        return analyze(
            self.collector.snapshot(),
            list(self.filtered_radio1.log),
            requests,
            stats,
            network_key=self.keys.network,
            e77_id=self.e77_id,
            listen_only=listen_only,
            filter_status=self.filtered_radio1.status(),
        )

    def close(self) -> None:
        try:
            self.filtered_radio1.close()
        finally:
            assert_no_active_filters()


def summary_lines(
    analysis: ProbeAnalysis,
    requests: Sequence[W.OutgoingRequest],
    stats: W.RunStats,
    *,
    listen_only: bool,
    key_source: str,
    topo: str,
    e77_id: int,
    observer_id: int,
    target: int,
) -> list[str]:
    """The final summary; kept to ten lines."""

    planned = 0 if listen_only else len(requests)
    suspect = analysis.suspect or "none"
    return [
        f"hidden-node summary keys={key_source}",
        (
            f"topo={topo} source_TX={SOURCE_TX} e77_TX={e77_id} "
            f"observer={observer_id} target={target}"
        ),
        f"requests planned={planned} sent={stats.attempted} tx_ok={stats.tx_ok}",
        (
            f"radio1 direct TX={SOURCE_TX} dropped={analysis.direct_dropped} "
            f"passed={analysis.direct_passed}"
        ),
        (
            f"radio1 forward TX={e77_id} same-message={analysis.forwards} "
            f"e77-app-verified={analysis.e77_app_verified}"
        ),
        f"radio0 heard={analysis.radio0_heard} same-message={analysis.radio0_matching}",
        f"filter unauth={analysis.filter_unauth} suspect={suspect}",
        (
            f"conclusion={analysis.conclusion}"
            + (
                f" reason={analysis.unknown_reason}"
                if analysis.conclusion == UNKNOWN and analysis.unknown_reason
                else ""
            )
        ),
    ]


def _node_id(text: str) -> int:
    value = int(text, 0)
    if not 0 <= value <= 0xFE:
        raise argparse.ArgumentTypeError("must be a node id in 0..254")
    return value


def _count(text: str) -> int:
    value = int(text)
    if not 1 <= value <= W.MAX_REQUESTS:
        raise argparse.ArgumentTypeError(f"must be 1..{W.MAX_REQUESTS}")
    return value


def _interval(text: str) -> float:
    value = float(text)
    if not math.isfinite(value) or value < W.MIN_INTERVAL_SECONDS:
        raise argparse.ArgumentTypeError(
            f"must be finite and at least {W.MIN_INTERVAL_SECONDS:g} s"
        )
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


def _byte(text: str) -> int:
    value = int(text, 0)
    if not 0 <= value <= 0xFF:
        raise argparse.ArgumentTypeError("must be 0..255")
    return value


def _fcnt_start(text: str) -> int:
    value = int(text, 0)
    if not 0 <= value <= 0xFFFFFFFF:
        raise argparse.ArgumentTypeError("must be 0..4294967295")
    return value


class _UsageError(Exception):
    pass


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise _UsageError(message)


def main(
    argv: Optional[Sequence[str]] = None,
    *,
    bridge_factory: Optional[Callable[[str], tuple]] = None,
    key_dir: Path = W.KEY_DIR,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    parser = _ArgumentParser(
        description="Prove one E77 relay is the only path from gateway radio0 to radio1."
    )
    parser.add_argument("port", nargs="?", help="gateway bridge serial port (not needed with --dry-run)")
    parser.add_argument("--topo", help="explicit topology, e.g. 1-2,1-225")
    parser.add_argument("--target", type=_node_id, default=DEFAULT_TARGET_ID, help="REQUEST target (default 9)")
    parser.add_argument("--e77-id", type=_node_id, default=DEFAULT_E77_ID, help="E77 node id (default 1)")
    parser.add_argument(
        "--observer-id",
        type=_node_id,
        default=DEFAULT_OBSERVER_ID,
        help="virtual node id of gateway radio1 (default 2)",
    )
    parser.add_argument("--action", type=_byte, default=W.DEFAULT_ACTION, help=f"REQUEST action (default {W.DEFAULT_ACTION})")
    parser.add_argument("--value", type=_byte, default=0, help="REQUEST value (default 0)")
    parser.add_argument("--count", type=_count, default=1, help=f"REQUESTs to send, 1..{W.MAX_REQUESTS}")
    parser.add_argument(
        "--interval",
        type=_interval,
        default=W.MIN_INTERVAL_SECONDS,
        help=f"seconds between REQUESTs (minimum {W.MIN_INTERVAL_SECONDS:g})",
    )
    parser.add_argument(
        "--listen",
        type=_nonnegative,
        default=W.DEFAULT_LISTEN_SECONDS,
        help=f"seconds to listen after the last REQUEST (default {W.DEFAULT_LISTEN_SECONDS:g})",
    )
    parser.add_argument("--fcnt-start", type=_fcnt_start, default=None, help="first FCNT base (default: Unix time)")
    parser.add_argument("--test-keys", action="store_true", help="use public tests/vectors keys (required for E77)")
    parser.add_argument("--listen-only", action="store_true", help="listen for --listen seconds and send no REQUEST")
    parser.add_argument("--dry-run", action="store_true", help="construct the frames; do not touch serial")
    parser.add_argument("--status-timeout", type=_positive, default=2.0, help="seconds to wait for bridge STATUS")
    try:
        args = parser.parse_args(argv)

        if args.target == args.e77_id:
            parser.error("--target must differ from --e77-id (an E77-targeted REQUEST is not relayed)")
        if not args.dry_run and not args.port:
            parser.error("port is required unless --dry-run is used")
        if not args.dry_run and not args.topo:
            parser.error("--topo is required unless --dry-run is used")
        if not args.dry_run and os.environ.get(ENV) != "1":
            parser.error(f"{ENV}=1 is required (the filter must be enabled deliberately)")
        if args.e77_id == SOURCE_TX or args.observer_id in (args.e77_id, SOURCE_TX):
            parser.error("--e77-id and --observer-id must be distinct from each other and source TX 225")
    except _UsageError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return EXIT_USAGE

    try:
        if args.test_keys:
            keys = W.load_test_keys()
            key_source = W.KEY_SOURCE_VECTORS
        else:
            keys = W.load_keys(key_dir)
            key_source = W.KEY_SOURCE_BENCH_FILE
        requests = W.build_requests(
            keys,
            target=args.target,
            count=args.count,
            action=args.action,
            value=args.value,
            sender=SOURCE_TX,
            initial_fcnt=args.fcnt_start,
        )
    except (OSError, ValueError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return EXIT_USAGE

    if args.dry_run:
        for index, request in enumerate(requests, 1):
            print(
                f"dry-run keys={key_source} frame={index} KIND=REQUEST "
                f"length={len(request.wire)} source_TX={SOURCE_TX} e77_TX={args.e77_id}"
            )
        return EXIT_RECEIVED

    try:
        validate_topology(args.topo, e77_id=args.e77_id, observer_id=args.observer_id)
    except ValueError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return EXIT_USAGE

    factory = bridge_factory or W.open_serial_bridge
    try:
        bridge, transport = factory(args.port)
    except OSError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return EXIT_USAGE
    probe = None
    stats = W.RunStats()
    try:
        if bridge.ping(timeout=args.status_timeout) is None:
            print("FAIL: gateway STATUS did not answer", file=sys.stderr)
            return EXIT_UNKNOWN
        try:
            probe = HiddenNodeProbe(
                bridge,
                keys=keys,
                topo=args.topo,
                e77_id=args.e77_id,
                observer_id=args.observer_id,
                target=args.target,
                clock=clock,
                sleep=sleep,
            )
        except (FilterError, ValueError) as exc:
            print(f"FAIL: {exc}", file=sys.stderr)
            return EXIT_USAGE
        probe.key_source = key_source
        stats = probe.run(
            requests,
            interval=args.interval,
            listen=args.listen,
            listen_only=args.listen_only,
        )
        analysis = probe.analysis(requests, stats, listen_only=args.listen_only)
    finally:
        try:
            if probe is not None:
                probe.close()
        finally:
            try:
                transport.close()
            finally:
                assert_no_active_filters()

    for line in summary_lines(
        analysis,
        requests,
        stats,
        listen_only=args.listen_only,
        key_source=key_source,
        topo=args.topo,
        e77_id=args.e77_id,
        observer_id=args.observer_id,
        target=args.target,
    ):
        print(line)
    return {
        RECEIVED: EXIT_RECEIVED,
        NOT_RECEIVED: EXIT_NOT_RECEIVED,
        UNKNOWN: EXIT_UNKNOWN,
    }[analysis.conclusion]


if __name__ == "__main__":
    sys.exit(main())
