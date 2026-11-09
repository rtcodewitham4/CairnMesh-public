"""Host tests for the wle5 forwarding bench tool; no serial port or gateway hardware."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESCCM

from cairnmesh import vectors

TOOLS = Path(__file__).resolve().parents[2] / "firmware" / "tools"
sys.path.insert(0, str(TOOLS))

import wle5fwd as W  # noqa: E402
from cairnmesh.bridge import TX_OK, TxDone  # noqa: E402
from cairnmesh.frames import APP, FrameKind, ReplyFrame, RequestFrame  # noqa: E402
from cairnmesh.secure import MIC_LEN, AppKeys, SecureTransport, open_sealed, seal, transmitter  # noqa: E402

NETWORK_KEY = bytes((index * 7 + 3) & 0xFF for index in range(16))
APP_KEY = bytes((index * 11 + 5) & 0xFF for index in range(16))


def write_keys(directory: Path) -> W.BenchKeys:
    directory.mkdir(exist_ok=True)
    (directory / W.KEY_FILE).write_text(
        f"net={NETWORK_KEY.hex()}\napp={APP_KEY.hex()}\n",
        encoding="ascii",
    )
    return W.BenchKeys(NETWORK_KEY, APP_KEY)


def framed(keys, at=1.0, radio=0, rssi=-91.0):
    requests = W.build_requests(keys, initial_fcnt=77, sequence=9, epoch=3)
    event = W.classify_frame(
        requests[0].wire,
        keys.network,
        app_key=keys.app,
        at=at,
        radio=radio,
        rssi=rssi,
    )
    return requests, event


class Wire:
    def set_receiver(self, callback):
        pass

    def send(self, data):
        pass


def app_sealed_frame(plain: bytes, fcnt: int, *, tx=None, after=None) -> bytes:
    secure = SecureTransport(
        Wire(),
        NETWORK_KEY,
        initial_fcnt=fcnt - 1,
        app=AppKeys({1: APP_KEY}),
    )
    if after is not None:
        for request in (after,) if isinstance(after, bytes) else after:
            secure._on_frame(request)  # remember the REQUEST exchange, as the live receiver does
    raw = secure.seal_frame(plain)
    if tx is not None and tx != W.DEFAULT_CONTROLLER_ID:
        below, actual_fcnt = open_sealed(AESCCM(NETWORK_KEY, tag_length=MIC_LEN), raw)
        raw = seal(AESCCM(NETWORK_KEY, tag_length=MIC_LEN), below, actual_fcnt, tx=tx)
    return raw


def test_loads_bench_keys_only_from_the_key_directory(tmp_path):
    directory = tmp_path / "keys"
    expected = write_keys(directory)
    (directory / "network.key").write_text("not used\n", encoding="ascii")
    loaded = W.load_keys(directory)
    assert loaded == expected
    assert "redacted" in repr(loaded)
    assert NETWORK_KEY.hex() not in repr(loaded)
    assert APP_KEY.hex() not in repr(loaded)


@pytest.mark.parametrize(
    "body",
    [
        f"net={NETWORK_KEY.hex()}\n",
        f"net={NETWORK_KEY.hex()}\napp={APP_KEY.hex()}\nextra=00\n",
        f"network={NETWORK_KEY.hex()}\napp={APP_KEY.hex()}\n",
        f"net={NETWORK_KEY.hex()[:-2]}\napp={APP_KEY.hex()}\n",
        f"net={'g' * 32}\napp={APP_KEY.hex()}\n",
        "0123456789abcdef\n",
    ],
)
def test_bad_bench_key_files_fail_without_echoing_contents(tmp_path, body):
    directory = tmp_path / "keys"
    directory.mkdir()
    (directory / W.KEY_FILE).write_text(body, encoding="ascii")
    with pytest.raises(ValueError) as caught:
        W.load_keys(directory)
    assert NETWORK_KEY.hex() not in str(caught.value)
    assert APP_KEY.hex() not in str(caught.value)


def test_request_is_an_unrouted_application_sealed_flood():
    keys = W.BenchKeys(NETWORK_KEY, APP_KEY)
    request = W.build_requests(keys, target=1, initial_fcnt=77, sequence=9, epoch=3)[0]
    below, fcnt = open_sealed(AESCCM(NETWORK_KEY, tag_length=MIC_LEN), request.wire)

    assert fcnt == 78
    assert below[0] == FrameKind.REQUEST | APP
    assert below[1:5] == bytes([W.PROBE_SENDER_ID, 1, 9, 3])
    assert transmitter(request.wire) == W.PROBE_SENDER_ID
    assert request.message_id == (3 << 8 | 9)
    assert len(request.wire) == 21  # 7-byte REQUEST + 9-byte network header/MIC + 5-byte app layer


def test_fcnt_start_is_deterministic_and_increases_once_per_request():
    keys = W.BenchKeys(NETWORK_KEY, APP_KEY)
    requests = W.build_requests(
        keys,
        count=3,
        sender=W.PROBE_SENDER_ID,
        initial_fcnt=1000,
        sequence=9,
        epoch=3,
    )
    fcnts = [
        open_sealed(AESCCM(NETWORK_KEY, tag_length=MIC_LEN), request.wire)[1]
        for request in requests
    ]
    assert fcnts == [1001, 1002, 1003]
    with pytest.raises(ValueError, match="0x42"):
        W.build_requests(keys, sender=W.DEFAULT_CONTROLLER_ID)


class FakePort:
    def __init__(self, bridge, index, clock):
        self.bridge = bridge
        self.index = index
        self.clock = clock
        self.receiver = None
        self.sent = []
        self.sent_at = []

    def set_receiver(self, callback):
        self.receiver = callback

    def send(self, data):
        self.sent.append(bytes(data))
        self.sent_at.append(self.clock())
        self.bridge.on_tx_done(TxDone(self.index, TX_OK, 0.0, 0.05))


class FakeBridge:
    def __init__(self, clock=lambda: 0.0):
        self.ports = [FakePort(self, 0, clock), FakePort(self, 1, clock)]
        self.on_tx_done = None
        self.pinged = 0

    def ping(self, timeout=1.0):
        self.pinged += 1
        return object()


class FakeTransport:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


def test_dry_run_uses_no_gateway_or_serial(tmp_path, capsys):
    write_keys(tmp_path)
    opened = []

    result = W.main(
        ["--dry-run", "--count", "2"],
        bridge_factory=lambda port: opened.append(port),
        key_dir=tmp_path,
    )

    lines = capsys.readouterr().out.splitlines()
    assert result == 0
    assert not opened
    assert lines == [
        "dry-run keys=bench-file frame=1 KIND=REQUEST length=21",
        "dry-run keys=bench-file frame=2 KIND=REQUEST length=21",
    ]


def test_test_keys_builds_with_vectors_without_touching_the_bench_directory(
    tmp_path, monkeypatch, capsys
):
    missing_key_dir = tmp_path / "missing-bench-keys"
    monkeypatch.setattr(W, "KEY_DIR", missing_key_dir)
    bridge, transport = FakeBridge(), FakeTransport()

    result = W.main(
        ["fake-port", "--test-keys", "--listen", "0", "--fcnt-start", "77"],
        bridge_factory=lambda port: (bridge, transport),
        clock=lambda: 0.0,
        sleep=lambda seconds: None,
    )

    assert result == 0
    assert not missing_key_dir.exists()
    assert len(bridge.ports[0].sent) == 1
    sent = bridge.ports[0].sent[0]
    event = W.classify_frame(
        sent,
        vectors.NETWORK_KEY,
        app_key=vectors.APP_KEY,
        at=0.0,
        radio=0,
        rssi=0.0,
    )
    assert event.verified and event.app_layer
    text = capsys.readouterr().out
    assert "keys=vectors" in text
    assert vectors.NETWORK_KEY.hex() not in text
    assert vectors.APP_KEY.hex() not in text


def test_listen_only_opens_the_fake_gateway_but_sends_nothing(tmp_path, capsys):
    write_keys(tmp_path)
    bridge, transport = FakeBridge(), FakeTransport()
    now = [0.0]

    def sleep(seconds):
        now[0] += seconds

    result = W.main(
        ["fake-port", "--listen-only", "--listen", "0"],
        bridge_factory=lambda port: (bridge, transport),
        key_dir=tmp_path,
        clock=lambda: now[0],
        sleep=sleep,
    )

    assert result == 0
    assert bridge.pinged == 1 and transport.closed
    assert bridge.ports[0].sent == bridge.ports[1].sent == []
    assert "requests planned=0 sent=0 tx_ok=0" in capsys.readouterr().out


def test_fake_gateway_sends_the_configured_count_at_the_cli_interval(tmp_path, capsys):
    write_keys(tmp_path)
    now = [0.0]
    bridge, transport = FakeBridge(lambda: now[0]), FakeTransport()

    def sleep(seconds):
        now[0] += seconds

    result = W.main(
        ["fake-port", "--count", "3", "--interval", "10", "--listen", "0", "--fcnt-start", "500"],
        bridge_factory=lambda port: (bridge, transport),
        key_dir=tmp_path,
        clock=lambda: now[0],
        sleep=sleep,
    )

    assert result == 0
    assert now[0] >= 20.0
    assert len(bridge.ports[0].sent) == 3
    assert bridge.ports[1].sent == []
    assert [transmitter(raw) for raw in bridge.ports[0].sent] == [W.PROBE_SENDER_ID] * 3
    assert [
        open_sealed(AESCCM(NETWORK_KEY, tag_length=MIC_LEN), raw)[1]
        for raw in bridge.ports[0].sent
    ] == [501, 502, 503]
    sent_at = bridge.ports[0].sent_at
    assert all(right - left >= W.MIN_INTERVAL_SECONDS for left, right in zip(sent_at, sent_at[1:]))
    assert "requests planned=3 sent=3 tx_ok=3" in capsys.readouterr().out


def test_bench_and_vectors_sources_seal_requests_that_match_their_keys(tmp_path):
    bench_keys = write_keys(tmp_path)
    vector_keys = W.load_test_keys()
    assert vector_keys == W.BenchKeys(vectors.NETWORK_KEY, vectors.APP_KEY)

    for keys in (bench_keys, vector_keys):
        request = W.build_requests(keys, initial_fcnt=77, sequence=9, epoch=3)[0]
        event = W.classify_frame(
            request.wire,
            keys.network,
            app_key=keys.app,
            at=1.0,
            radio=0,
            rssi=-91.0,
        )
        assert event.verified and event.app_layer


def test_forward_detection_positive_and_negative():
    keys = W.BenchKeys(NETWORK_KEY, APP_KEY)
    requests, own = framed(keys)
    below, actual_fcnt = open_sealed(AESCCM(NETWORK_KEY, tag_length=MIC_LEN), requests[0].wire)
    relayed_wire = seal(AESCCM(NETWORK_KEY, tag_length=MIC_LEN), below, actual_fcnt, tx=7)
    relayed = W.classify_frame(
        relayed_wire,
        NETWORK_KEY,
        app_key=APP_KEY,
        at=1.1,
        radio=1,
        rssi=-88,
    )
    assert W.observed_forwards([own], requests) == []

    other_id = app_sealed_frame(
        RequestFrame(W.DEFAULT_CONTROLLER_ID, 1, 10, action=4, value=0, epoch=3).encode(),
        79,
        tx=7,
    )
    other = W.classify_frame(other_id, NETWORK_KEY, app_key=APP_KEY, at=1.2, radio=1, rssi=-89)
    # A variant that calls every other-TX frame a forward must fail here.
    assert W.observed_forwards([own, other], requests) == []
    assert W.observed_forwards([own, relayed], requests) == [relayed]


def test_forward_negative_cases_cover_all_candidate_filters():
    keys = W.BenchKeys(NETWORK_KEY, APP_KEY)
    requests, _ = framed(keys)
    plain = RequestFrame(W.PROBE_SENDER_ID, 1, 9, action=4, value=0, epoch=3).encode()
    aead = AESCCM(NETWORK_KEY, tag_length=MIC_LEN)

    below, fcnt = open_sealed(aead, requests[0].wire)
    tampered_below = bytearray(below)
    tampered_below[-1] ^= 1
    failed_wire = seal(aead, bytes(tampered_below), fcnt, tx=7)
    failed = W.classify_frame(failed_wire, NETWORK_KEY, app_key=APP_KEY, at=1.1, radio=1, rssi=-80)
    assert failed.app_layer and not failed.verified
    assert W.observed_forwards([failed], requests) == []

    no_app = W.classify_frame(
        seal(aead, plain, fcnt, tx=7),
        NETWORK_KEY,
        app_key=APP_KEY,
        at=1.2,
        radio=1,
        rssi=-81,
    )
    assert no_app.verified and not no_app.app_layer
    assert W.observed_forwards([no_app], requests) == []

    hello = W.classify_frame(
        seal(aead, bytes([FrameKind.HELLO, 7, 0xFF, 9, 1]), 82, tx=7),
        NETWORK_KEY,
        app_key=APP_KEY,
        at=1.3,
        radio=1,
        rssi=-82,
    )
    join = W.classify_frame(
        seal(aead, bytes([FrameKind.JOIN, 7, 0xFF, 9, 3, 7, 1]), 83, tx=7),
        NETWORK_KEY,
        app_key=APP_KEY,
        at=1.4,
        radio=1,
        rssi=-83,
    )
    assert {hello.kind, join.kind} == {FrameKind.HELLO, FrameKind.JOIN}
    assert W.observed_forwards([hello, join], requests) == []

    wrong_key = seal(AESCCM(bytes(16), tag_length=MIC_LEN), plain, 84, tx=7)
    bad = W.classify_frame(wrong_key, NETWORK_KEY, app_key=APP_KEY, at=1.5, radio=1, rssi=-84)
    assert not bad.verified
    assert W.observed_forwards([bad], requests) == []


def test_reply_detection_and_unknown_conclusions_are_not_inferred():
    keys = W.BenchKeys(NETWORK_KEY, APP_KEY)
    requests, own = framed(keys)
    reply_wire = app_sealed_frame(
        ReplyFrame(1, W.PROBE_SENDER_ID, 9, result=0, epoch=3).encode(),
        80,
        tx=1,
        after=requests[0].wire,
    )
    reply = W.classify_frame(reply_wire, NETWORK_KEY, app_key=APP_KEY, at=1.3, radio=1, rssi=-87)

    assert W.observed_replies([own], requests) == []
    assert W.observed_replies([reply], requests) == [reply]
    lines = W.summary_lines([own], requests, W.RunStats(attempted=1, tx_ok=1), listen_only=False)
    assert len(lines) <= 10
    assert "forward=未观察到" in lines
    assert "reply=未观察到" in lines


def test_wrong_and_missing_ids_do_not_count_as_replies():
    keys = W.BenchKeys(NETWORK_KEY, APP_KEY)
    requests, _ = framed(keys)
    other_request = app_sealed_frame(
        RequestFrame(W.PROBE_SENDER_ID, 1, 10, action=4, value=0, epoch=3).encode(),
        79,
    )
    wrong_id_wire = app_sealed_frame(
        ReplyFrame(1, W.PROBE_SENDER_ID, 10, result=0, epoch=3).encode(),
        80,
        tx=1,
        after=(requests[0].wire, other_request),
    )
    wrong_id = W.classify_frame(
        wrong_id_wire,
        NETWORK_KEY,
        app_key=APP_KEY,
        at=1.3,
        radio=1,
        rssi=-87,
    )
    assert wrong_id.verified and wrong_id.app_layer
    assert W.observed_replies([wrong_id], requests) == []


def test_app_verification_exception_leaves_frame_unverified(monkeypatch):
    keys = W.BenchKeys(NETWORK_KEY, APP_KEY)
    requests, _ = framed(keys)
    decoder = W.FrameDecoder(NETWORK_KEY, APP_KEY)

    def fail(*args, **kwargs):
        raise RuntimeError("synthetic app failure")

    monkeypatch.setattr(decoder._app, "_app_open", fail)
    event = decoder.classify(requests[0].wire, at=2.0, radio=0, rssi=-80)
    assert event.app_layer and not event.verified


def test_network_only_reply_forgery_is_recorded_but_not_called_a_reply():
    keys = W.BenchKeys(NETWORK_KEY, APP_KEY)
    requests, _ = framed(keys)
    forged = seal(
        AESCCM(NETWORK_KEY, tag_length=MIC_LEN),
        ReplyFrame(1, W.PROBE_SENDER_ID, 9, result=0, epoch=3).encode(),
        81,
        tx=1,
    )
    event = W.classify_frame(forged, NETWORK_KEY, app_key=APP_KEY, at=2.0, radio=1, rssi=-86)
    assert event.verified and not event.app_layer
    assert W.observed_replies([event], requests) == []


def test_interval_floor_and_request_cap_are_enforced():
    with pytest.raises(SystemExit):
        W.main(["--dry-run", "--count", "6"])
    with pytest.raises(SystemExit):
        W.main(["--dry-run", "--interval", "9.9"])
    for value in ("nan", "inf", "-inf"):
        with pytest.raises(SystemExit):
            W.main(["--dry-run", "--interval", value])
        with pytest.raises(SystemExit):
            W.main(["--dry-run", "--listen", value])
    with pytest.raises(SystemExit):
        W.main(["--dry-run", "--status-timeout", "inf"])
    with pytest.raises(SystemExit):
        W.main(["--dry-run", "--sender", "0x42"])


def test_probe_run_rechecks_count_interval_and_listen():
    keys = W.BenchKeys(NETWORK_KEY, APP_KEY)
    request = W.build_requests(keys, initial_fcnt=77, sequence=9, epoch=3)[0]
    collector = W.FrameCollector(NETWORK_KEY, APP_KEY)
    bridge = FakeBridge()
    run = W.ProbeRun(bridge, collector, clock=lambda: 0.0, sleep=lambda seconds: None)
    with pytest.raises(ValueError, match="count"):
        run.run([request] * 6, radio=0, interval=10, listen=0, listen_only=True)
    for interval in (9.9, float("nan"), float("inf")):
        with pytest.raises(ValueError, match="interval"):
            run.run([request], radio=0, interval=interval, listen=0, listen_only=True)
    with pytest.raises(ValueError, match="listen"):
        run.run([request], radio=0, interval=10, listen=float("nan"), listen_only=True)
    assert bridge.ports[0].sent == []


def test_summary_separates_request_echoes_from_forwards_and_listen_only_judges_none():
    keys = W.BenchKeys(NETWORK_KEY, APP_KEY)
    requests, own = framed(keys)
    below, fcnt = open_sealed(AESCCM(NETWORK_KEY, tag_length=MIC_LEN), requests[0].wire)
    relayed = W.classify_frame(
        seal(AESCCM(NETWORK_KEY, tag_length=MIC_LEN), below, fcnt, tx=7),
        NETWORK_KEY,
        app_key=APP_KEY,
        at=1.1,
        radio=1,
        rssi=-88,
    )
    lines = W.summary_lines([own, relayed], requests, W.RunStats(attempted=1, tx_ok=1), listen_only=False)
    assert "request-copies echo=1 other-tx=1" in lines
    assert "forward=observed tx=7" in lines

    listen_only = W.summary_lines(
        [own, relayed],
        requests,
        W.RunStats(),
        listen_only=True,
    )
    assert "request-copies echo=0 other-tx=0" in listen_only
    assert "forward=未观察到" in listen_only
    assert "reply=未观察到" in listen_only
