"""The PC side of the gateway bridge (spec 6.1). The byte strings are the
ones ``firmware/gateway/bridge_test.go`` checks on the board's side."""

from __future__ import annotations

import pytest

from cairnmesh.bridge import (
    COUNTERS,
    PING,
    TX_OK,
    TX_REFUSED,
    Busy,
    GatewayBridge,
    Rx,
    Status,
    TxDone,
    decode,
    encode_tune,
    encode_tx,
)


class FakeTransport:
    def __init__(self) -> None:
        self.sent: list[bytes] = []
        self.receiver = None
        self.on_ping = None

    def send(self, data: bytes) -> None:
        self.sent.append(bytes(data))
        if data == PING and self.on_ping is not None:
            self.receiver(self.on_ping)

    def set_receiver(self, callback) -> None:
        self.receiver = callback


def status_bytes(extra_radio: bytes = b"", counters: int = len(COUNTERS)) -> bytes:
    radios = bytes([1, 0, 7, 0, 0, 3, 0, 0, 0, 0, 0, 0, 0, 0]) + extra_radio
    radios += bytes([0, 1, 9, 2, 0, 0, 0, 0, 0, 7, 0xFF, 0xFF, 0, 4]) + extra_radio
    values = b"".join(v.to_bytes(4, "big") for v in range(1, counters + 1))
    return bytes([0x83, 1, 2, 14 + len(extra_radio)]) + radios + bytes([counters]) + values


def test_encode_matches_the_board():
    # tx(0, FlagUrgent, 1000, 0, 0xAA, 0xBB) in bridge_test.go
    assert encode_tx(0, b"\xaa\xbb", preamble=1000) == bytes.fromhex("01000103e80000aabb")
    assert encode_tx(1, b"\x02", urgent=False, answer=0.5) == bytes.fromhex("0101000000" + "01f4" + "02")
    assert encode_tune(0, 1, 9) == bytes([0x02, 0, 1, 9])
    with pytest.raises(ValueError):
        encode_tx(0, b"")
    with pytest.raises(ValueError):
        encode_tx(0, bytes(256))


def test_decode_matches_the_board():
    assert decode(bytes([0x81, 1, 0xFF, 0x88, 0xF9, 1, 2, 3])) == Rx(1, -120, -7, b"\x01\x02\x03")
    assert decode(bytes([0x82, 1, 0, 0, 50, 0, 52]), at=3.0) == TxDone(1, TX_OK, 0.05, 0.052, 3.0)
    status = decode(status_bytes())
    assert isinstance(status, Status) and status.version == 1
    assert [(r.healthy, r.channel, r.sf, r.queued) for r in status.radios] == [(True, 0, 7, 0), (False, 1, 9, 2)]
    assert status.radios[0].crc_errors == 3 and status.radios[1].bus_retries == 7 and status.radios[1].faults == 0xFFFF
    assert status.radios[1].resets == 4 and status.radios[0].resets == 0
    assert status.counters["received"] == 1 and status.counters["watchdog_boot"] == len(COUNTERS)
    assert decode(bytes([0x84, 1, 1])) == Busy(1, True)
    for bad in (b"", b"\x81\x00\x00\x00\x00", b"\x82\x00", b"\x83\x01\x02", b"\x84\x00", status_bytes()[:-1]):
        with pytest.raises(ValueError):
            decode(bad)


def test_decode_reads_a_newer_board():
    """Appended fields, more counters, new message types: read what is known, skip the rest."""

    newer = decode(status_bytes(extra_radio=b"\x05\x06", counters=len(COUNTERS) + 2))
    assert newer == decode(status_bytes())
    older = decode(status_bytes(counters=7))
    assert older.counters["tune_failed"] == 7 and older.counters["held"] == 0
    # A board of before the resets field: 12 bytes a radio.
    old = bytes([0x83, 1, 1, 12, 1, 0, 7, 0, 0, 3, 0, 0, 0, 0, 0, 0, 1]) + (5).to_bytes(4, "big")
    assert decode(old).radios[0].resets == 0 and decode(old).radios[0].crc_errors == 3
    assert decode(bytes([0x82, 1, 0, 0, 50, 0, 52, 9]), at=3.0) == TxDone(1, TX_OK, 0.05, 0.052, 3.0)
    assert decode(bytes([0x84, 0, 0, 7])) == Busy(0, False)
    assert decode(b"\x55\x01") is None


def test_ports_send_and_receive():
    t = FakeTransport()
    bridge = GatewayBridge(t)
    heard = []
    bridge.ports[1].set_receiver(lambda frame, rssi: heard.append((frame, rssi)))
    bridge.ports[0].send(b"\x01\x02")
    bridge.ports[1].send(b"\x03", preamble=1000, urgent=False, answer=1.0)
    bridge.ports[1].set_radio(1, 9)
    assert t.sent == [
        encode_tx(0, b"\x01\x02", preamble=8),
        encode_tx(1, b"\x03", preamble=1000, urgent=False, answer=1.0),
        encode_tune(1, 1, 9),
    ]
    assert bridge.pending == [1, 1]
    t.receiver(bytes([0x81, 1, 0xFF, 0x88, 0xF9, 9]))
    t.receiver(bytes([0x81, 0, 0xFF, 0x88, 0xF9, 8]))  # radio 0 has no receiver
    assert heard == [(b"\x09", -120.0)]


def test_tx_done_and_blinding():
    clock = [10.0]
    t = FakeTransport()
    done = []
    bridge = GatewayBridge(t, clock=lambda: clock[0], on_tx_done=done.append)
    bridge.ports[0].send(b"\x01")
    t.receiver(bytes([0x82, 0, TX_OK, 0, 0, 0, 100]))  # radio 0 on air 9.9..10.0
    assert bridge.pending == [0, 0] and done == [TxDone(0, TX_OK, 0.0, 0.1, 10.0)]
    assert bridge.blinded(1, 9.95, 9.97)
    assert not bridge.blinded(1, 10.01, 10.5)
    assert not bridge.blinded(0, 9.95, 9.97)  # its own transmission does not blind it
    t.receiver(bytes([0x82, 1, TX_REFUSED, 0, 0, 0, 0]))
    assert bridge.pending == [0, 0]  # never below zero


def test_ping_and_malformed():
    t = FakeTransport()
    errors = []
    bridge = GatewayBridge(t, on_error=errors.append)
    t.on_ping = status_bytes()
    status = bridge.ping(timeout=0.1)
    assert status is not None and len(status.radios) == 2
    t.on_ping = None
    assert bridge.ping(timeout=0.01) is None
    t.receiver(b"\x82\x00")
    assert bridge.malformed == 1 and len(errors) == 1
    t.receiver(b"\x55")
    assert bridge.ignored == 1 and bridge.malformed == 1


def test_busy_until_listens_before_talk():
    clock = [5.0]
    t = FakeTransport()
    bridge = GatewayBridge(t, clock=lambda: clock[0])
    port = bridge.ports[1]
    assert port.busy_until() < clock[0]
    t.receiver(bytes([0x84, 1, 1]))
    assert port.busy_until() > clock[0] and bridge.ports[0].busy_until() < clock[0]
    t.receiver(bytes([0x84, 1, 0]))
    assert port.busy_until() < clock[0]
    port.send(b"\x01")  # queued on the board: busy until its TX_DONE
    assert port.busy_until() > clock[0]
    t.receiver(bytes([0x82, 1, TX_OK, 0, 0, 0, 52]))
    assert port.busy_until() < clock[0]
