"""Host tests for the two-radio/E77 hidden-node probe; no hardware is touched."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESCCM

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from cairnmesh.bridge import TX_OK, TxDone  # noqa: E402
from cairnmesh.frames import RequestFrame  # noqa: E402
from cairnmesh.secure import (  # noqa: E402
    MIC_LEN,
    AppKeys,
    SecureTransport,
    open_sealed,
    reseal,
    seal,
)
from tests.testbed import hidden_node_probe as H  # noqa: E402
from tests.testbed import rxfilter as F  # noqa: E402

KEYS = H.W.load_test_keys()
WRONG_KEYS = H.W.BenchKeys(bytes(16), bytes(16))
TOPO = "1-2,1-225"


@pytest.fixture(autouse=True)
def testbed_env(monkeypatch):
    monkeypatch.setenv(F.ENV, "1")
    yield
    F.close_all()


class FakeRadio:
    def __init__(self, bridge, index):
        self.bridge = bridge
        self.index = index
        self.receiver = None
        self.sent = []

    def set_receiver(self, callback):
        self.receiver = callback

    def send(self, data):
        self.sent.append(bytes(data))
        self.bridge.deliver(self.index, bytes(data))
        self.bridge.on_tx_done(TxDone(self.index, TX_OK, 0.0, 0.01))


class FakeBridge:
    """Two co-located radios; radio1 physically receives both direct and relayed copies."""

    def __init__(self, mode="forward", wire_keys=KEYS):
        self.mode = mode
        self.wire_keys = wire_keys
        self.ports = [FakeRadio(self, 0), FakeRadio(self, 1)]
        self.on_tx_done = None
        self.pinged = 0

    def ping(self, timeout=1.0):
        self.pinged += 1
        return object()

    def deliver(self, source, data):
        # The co-located listener hears the source radio directly.
        self.ports[1].receiver(data, -91.0)
        if self.mode == "direct-only":
            return
        if self.mode == "direct-only-e77-proof":
            self.ports[1].receiver(e77_app_request(self.wire_keys), -70.0)
            return
        if self.mode == "forward":
            # Radio0 and radio1 are physically close enough to hear the E77's copy.
            aead = AESCCM(self.wire_keys.network, tag_length=MIC_LEN)
            relayed = reseal(aead, data, H.DEFAULT_E77_ID)
            self.ports[1].receiver(relayed, -72.0)
            self.ports[0].receiver(relayed, -68.0)


class FakeTransport:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class Wire:
    def set_receiver(self, callback):
        pass

    def send(self, data):
        pass


def make_requests(keys=KEYS):
    return H.W.build_requests(
        keys,
        target=H.DEFAULT_TARGET_ID,
        count=1,
        initial_fcnt=100,
        sequence=7,
        epoch=3,
    )


def e77_app_request(keys=KEYS, *, sequence=11, epoch=4) -> bytes:
    secure = SecureTransport(
        Wire(),
        keys.network,
        initial_fcnt=200,
        app=AppKeys({H.SOURCE_TX: keys.app}),
    )
    plain = RequestFrame(
        sender_id=H.DEFAULT_E77_ID,
        target_id=H.SOURCE_TX,
        sequence=sequence,
        action=4,
        value=0,
        epoch=epoch,
    ).encode()
    return secure.seal_frame(plain)


def classify(raw, *, radio=1):
    return H.W.classify_frame(
        raw,
        KEYS.network,
        app_key=KEYS.app,
        at=1.0,
        radio=radio,
        rssi=-70.0,
        target=H.DEFAULT_TARGET_ID,
    )


def forwarded(request):
    return classify(reseal(AESCCM(KEYS.network, tag_length=MIC_LEN), request.wire, H.DEFAULT_E77_ID))


def execute(mode="forward", *, filter_keys=KEYS, wire_keys=KEYS):
    bridge = FakeBridge(mode=mode, wire_keys=wire_keys)
    probe = H.HiddenNodeProbe(bridge, keys=filter_keys, topo=TOPO)
    requests = make_requests(wire_keys)
    try:
        stats = probe.run(requests, interval=10.0, listen=0.0, listen_only=False)
        frames = probe.collector.snapshot()
        analysis = probe.analysis(requests, stats, listen_only=False)
    finally:
        probe.close()
    assert F.active_filters() == []
    return bridge, frames, analysis


def test_radio1_drops_direct_and_only_receives_the_tx1_forward():
    # Mutation check: removing FilteredPort makes dropped=0 and lets TX=225
    # reach matching_radio1, so at least one assertion below must fail.
    bridge, frames, analysis = execute()

    assert [H.transmitter(raw) for raw in bridge.ports[0].sent] == [H.SOURCE_TX]
    assert analysis.direct_dropped == 1
    assert analysis.direct_passed == 0
    radio1 = [frame for frame in frames if frame.radio == 1]
    assert [(frame.tx, frame.sender, frame.key) for frame in radio1] == [
        (H.DEFAULT_E77_ID, H.SOURCE_TX, (H.SOURCE_TX, 3 << 8 | 7))
    ]


def test_tx1_forward_for_the_same_message_is_received_via_e77():
    _, _, analysis = execute("forward")

    assert analysis.forwards == 1
    assert analysis.conclusion == H.RECEIVED


def test_no_forward_without_e77_app_proof_is_unknown():
    _, frames, analysis = execute("direct-only")

    assert analysis.direct_dropped == 1
    assert not [frame for frame in frames if frame.radio == 1]
    assert analysis.forwards == 0
    assert analysis.e77_app_verified == 0
    assert analysis.conclusion == H.UNKNOWN
    assert analysis.unknown_reason == H.UNKNOWN_NO_E77_KEY
    lines = H.summary_lines(
        analysis,
        make_requests(),
        H.W.RunStats(attempted=1, tx_ok=1),
        listen_only=False,
        key_source="vectors",
        topo=TOPO,
        e77_id=1,
        observer_id=2,
        target=9,
    )
    assert any(f"reason={H.UNKNOWN_NO_E77_KEY}" in line for line in lines)


def test_no_forward_after_e77_app_proof_means_not_received():
    _, _, analysis = execute("direct-only-e77-proof")

    assert analysis.direct_dropped == 1
    assert analysis.forwards == 0
    assert analysis.e77_app_verified == 1
    assert analysis.conclusion == H.NOT_RECEIVED


def test_same_wrong_key_on_requests_and_probe_is_unknown_without_e77_proof():
    _, frames, analysis = execute(
        "direct-only",
        filter_keys=WRONG_KEYS,
        wire_keys=WRONG_KEYS,
    )

    assert not frames
    assert analysis.direct_dropped == 1
    assert analysis.direct_passed == 0
    assert analysis.forwards == 0
    assert analysis.e77_app_verified == 0
    assert analysis.conclusion == H.UNKNOWN
    assert analysis.unknown_reason == H.UNKNOWN_NO_E77_KEY


def test_suspect_turns_a_would_be_received_result_into_unknown():
    requests = make_requests()
    frame = forwarded(requests[0])
    stats = H.W.RunStats(attempted=1, tx_ok=1)
    baseline = H.analyze(
        [frame],
        [],
        requests,
        stats,
        network_key=KEYS.network,
    )
    analysis = H.analyze(
        [frame],
        [],
        requests,
        stats,
        network_key=KEYS.network,
        filter_status={"unauth": 6, "suspect": "wrong key; passing everything"},
    )
    lines = H.summary_lines(
        analysis,
        requests,
        stats,
        listen_only=False,
        key_source="vectors",
        topo=TOPO,
        e77_id=1,
        observer_id=2,
        target=9,
    )

    assert len(lines) <= 10
    assert any("suspect=wrong key" in line for line in lines)
    assert any(f"reason={H.UNKNOWN_SUSPECT}" in line for line in lines)
    assert baseline.conclusion == H.RECEIVED
    assert analysis.conclusion == H.UNKNOWN
    assert analysis.unknown_reason == H.UNKNOWN_SUSPECT


def test_real_filter_status_suspect_overrides_a_received_result():
    requests = make_requests()
    radio = FakeRadio(FakeBridge(), 1)
    filtered = F.FilteredPort(
        radio,
        key=WRONG_KEYS.network,
        node=H.DEFAULT_OBSERVER_ID,
        topo=TOPO,
    )
    try:
        filtered.set_receiver(lambda *args: None)
        raw = reseal(AESCCM(KEYS.network, tag_length=MIC_LEN), requests[0].wire, H.DEFAULT_E77_ID)
        for _ in range(6):
            radio.receiver(raw, -70.0)
        status = filtered.status()
        assert status["suspect"]

        analysis = H.analyze(
            [forwarded(requests[0])],
            [],
            requests,
            H.W.RunStats(attempted=1, tx_ok=1),
            network_key=KEYS.network,
            filter_status=status,
        )
        assert analysis.conclusion == H.UNKNOWN
        assert analysis.unknown_reason == H.UNKNOWN_SUSPECT
    finally:
        filtered.close()
    assert F.active_filters() == []


def test_tx1_with_another_message_sender_or_fcnt_is_not_a_forward():
    requests = make_requests()
    aead = AESCCM(KEYS.network, tag_length=MIC_LEN)

    another_id = H.W.build_requests(
        KEYS,
        target=H.DEFAULT_TARGET_ID,
        initial_fcnt=300,
        sequence=8,
        epoch=3,
    )[0]
    another_sender = H.W.build_requests(
        KEYS,
        target=H.DEFAULT_TARGET_ID,
        sender=0xE2,
        initial_fcnt=300,
        sequence=7,
        epoch=3,
    )[0]
    secure = SecureTransport(
        Wire(),
        KEYS.network,
        initial_fcnt=300,
        app=AppKeys({H.DEFAULT_TARGET_ID: KEYS.app}),
    )
    same_ids_new_fcnt = secure.seal_frame(
        RequestFrame(
            sender_id=H.SOURCE_TX,
            target_id=H.DEFAULT_TARGET_ID,
            sequence=7,
            action=4,
            value=0,
            epoch=3,
        ).encode()
    )

    candidates = [
        forwarded(another_id),
        forwarded(another_sender),
        classify(reseal(aead, same_ids_new_fcnt, H.DEFAULT_E77_ID)),
    ]
    for frame in candidates:
        assert frame.tx == H.DEFAULT_E77_ID and frame.verified and frame.app_layer
        analysis = H.analyze(
            [frame],
            [],
            requests,
            H.W.RunStats(attempted=1, tx_ok=1),
            network_key=KEYS.network,
        )
        assert analysis.forwards == 0


def test_tx1_network_only_frame_does_not_prove_the_app_key():
    requests = make_requests()
    aead = AESCCM(KEYS.network, tag_length=MIC_LEN)
    plain, fcnt = open_sealed(aead, requests[0].wire)
    network_only = seal(
        aead,
        RequestFrame(
            sender_id=H.SOURCE_TX,
            target_id=H.DEFAULT_TARGET_ID,
            sequence=7,
            action=4,
            value=0,
            epoch=3,
        ).encode(),
        fcnt,
        tx=H.DEFAULT_E77_ID,
    )
    app_mic_failed_plain = bytearray(plain)
    app_mic_failed_plain[-1] ^= 1
    app_mic_failed = seal(aead, bytes(app_mic_failed_plain), fcnt, tx=H.DEFAULT_E77_ID)

    network_frame = classify(network_only)
    failed_frame = classify(app_mic_failed)
    assert network_frame.verified and not network_frame.app_layer
    assert failed_frame.app_layer and not failed_frame.verified

    for frame in (network_frame, failed_frame):
        analysis = H.analyze(
            [frame],
            [],
            requests,
            H.W.RunStats(attempted=1, tx_ok=1),
            network_key=KEYS.network,
        )
        assert analysis.forwards == 0
        assert analysis.e77_app_verified == 0
        assert analysis.conclusion == H.UNKNOWN


def test_direct_pass_and_listen_only_are_unknown_even_with_a_forward():
    requests = make_requests()
    direct = classify(requests[0].wire)
    forward = forwarded(requests[0])
    stats = H.W.RunStats(attempted=1, tx_ok=1)

    direct_pass = H.analyze(
        [direct, forward],
        [],
        requests,
        stats,
        network_key=KEYS.network,
    )
    assert direct_pass.direct_passed == 1
    assert direct_pass.forwards == 1
    assert direct_pass.conclusion == H.UNKNOWN
    assert direct_pass.unknown_reason == H.UNKNOWN_DIRECT_PASSED

    listen_only = H.analyze(
        [forward],
        [],
        requests,
        stats,
        network_key=KEYS.network,
        listen_only=True,
    )
    assert listen_only.conclusion == H.UNKNOWN
    assert listen_only.unknown_reason == H.UNKNOWN_LISTEN_ONLY


def test_dry_run_needs_no_gateway_serial_topology_or_filter(capsys):
    opened = []

    result = H.main(
        ["--dry-run", "--test-keys", "--count", "2"],
        bridge_factory=lambda port: opened.append(port),
    )

    lines = capsys.readouterr().out.splitlines()
    assert result == 0
    assert not opened
    assert len(lines) == 2
    assert "source_TX=225 e77_TX=1" in lines[0]


def test_listen_only_opens_the_bridge_but_sends_nothing(capsys):
    bridge, transport = FakeBridge("direct-only"), FakeTransport()

    result = H.main(
        ["fake-port", "--topo", TOPO, "--test-keys", "--listen-only", "--listen", "0"],
        bridge_factory=lambda port: (bridge, transport),
        clock=lambda: 0.0,
        sleep=lambda seconds: None,
    )

    output = capsys.readouterr().out
    assert result == H.EXIT_UNKNOWN
    assert bridge.pinged == 1 and transport.closed
    assert bridge.ports[0].sent == bridge.ports[1].sent == []
    assert "requests planned=0 sent=0 tx_ok=0" in output
    assert f"conclusion={H.UNKNOWN} reason={H.UNKNOWN_LISTEN_ONLY}" in output
    assert F.active_filters() == []


def test_guards_reject_e77_target_and_wle5fwd_limits():
    assert H.main(["--dry-run", "--test-keys", "--target", "1"]) == H.EXIT_USAGE
    assert H.main(["--dry-run", "--test-keys", "--count", "6"]) == H.EXIT_USAGE
    assert H.main(["--dry-run", "--test-keys", "--interval", "9.9"]) == H.EXIT_USAGE
    assert H.main(["fake-port", "--test-keys"]) == H.EXIT_USAGE


def test_runtime_requires_the_filter_environment(monkeypatch):
    monkeypatch.delenv(F.ENV)
    assert H.main(["fake-port", "--topo", TOPO, "--test-keys", "--listen", "0"]) == H.EXIT_USAGE


def test_topology_is_validated_before_the_serial_factory(capsys):
    opened = []
    result = H.main(
        ["fake-port", "--topo", "1-3", "--test-keys", "--listen", "0"],
        bridge_factory=lambda port: opened.append(port),
    )

    assert result == H.EXIT_USAGE
    assert not opened
    assert "must hear exactly TX=1" in capsys.readouterr().err


def test_main_conclusion_exit_codes_and_filter_cleanup(capsys):
    cases = [
        ("forward", H.EXIT_RECEIVED, H.RECEIVED),
        ("direct-only-e77-proof", H.EXIT_NOT_RECEIVED, H.NOT_RECEIVED),
        ("direct-only", H.EXIT_UNKNOWN, H.UNKNOWN),
    ]
    for mode, expected_code, expected_conclusion in cases:
        bridge, transport = FakeBridge(mode), FakeTransport()
        result = H.main(
            ["fake-port", "--topo", TOPO, "--test-keys", "--listen", "0"],
            bridge_factory=lambda port, bridge=bridge, transport=transport: (bridge, transport),
            clock=lambda: 0.0,
            sleep=lambda seconds: None,
        )
        output = capsys.readouterr().out
        assert result == expected_code
        assert f"conclusion={expected_conclusion}" in output
        assert transport.closed
        assert F.active_filters() == []


@pytest.mark.parametrize(
    ("request_count", "interval", "listen"),
    [
        (0, 10.0, 0.0),
        (6, 10.0, 0.0),
        (1, 9.9, 0.0),
        (1, float("nan"), 0.0),
        (1, float("inf"), 0.0),
        (1, 10.0, -1.0),
        (1, 10.0, float("nan")),
        (1, 10.0, float("inf")),
    ],
)
def test_hidden_probe_run_rejects_bad_limits_and_closes_the_filter(request_count, interval, listen):
    request = make_requests()[0]
    probe = H.HiddenNodeProbe(FakeBridge("direct-only"), keys=KEYS, topo=TOPO)
    try:
        with pytest.raises(ValueError):
            probe.run(
                [request] * request_count,
                interval=interval,
                listen=listen,
                listen_only=False,
            )
        assert F.active_filters() == []
    finally:
        probe.close()
    assert F.active_filters() == []
