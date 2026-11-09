"""Tests for serial framing and the secure envelope.

The envelope vectors are built from the documented layout (header with TX,
nonce with TX) and raw AES-CCM, not through ``seal``, so they pin the format
the firmware must reproduce (``firmware/proto`` does, see
``firmware/README.md``).
"""

from __future__ import annotations

import threading
import time

import pytest

pytest.importorskip("cryptography")

from cairnmesh.channel import SimulatedMedium
from cairnmesh.controller import Controller
from cairnmesh.device import Device
from cairnmesh.events import FrameRejected, LinkError
from cairnmesh.secure import (
    ReplayWindow,
    SecureTransport,
    open_sealed,
    reseal,
    seal,
    transmitter,
)
from cairnmesh.serial_link import (
    MAX_FRAME,
    SerialTransport,
    SlipDecoder,
    crc16,
    slip_encode,
)
from cairnmesh.session import LinkSession

KEY = b"0123456789abcdef"

# The old Go vector's plain frame: REQUEST 0x42 -> 0x01, SEQ 5, ACTION 3,
# VALUE 100 (pre-EPOCH layout; the envelope does not parse it), FCNT 77.
VECTOR_PLAIN = bytes([0x01, 0x42, 0x01, 0x05, 0x03, 0x64])
#: Sent by its originator: TX = SENDER = 0x42.
VECTOR_SEALED = bytes.fromhex("814201054d00000042bfac3f846fe7")
#: The same frame relayed by node 0x07: only TX, and so the ciphertext, differ.
VECTOR_RELAYED = bytes.fromhex("814201054d00000007805d614fff55")


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class PairTransport:
    """In-memory frame pipe: what one end sends, the other receives."""

    def __init__(self) -> None:
        self.peer: PairTransport | None = None
        self.receiver = None
        self.sent: list[bytes] = []

    def send(self, data: bytes) -> None:
        self.sent.append(data)
        if self.peer and self.peer.receiver:
            self.peer.receiver(data)

    def set_receiver(self, callback) -> None:
        self.receiver = callback


def pair():
    a, b = PairTransport(), PairTransport()
    a.peer, b.peer = b, a
    return a, b


def aead():
    from cryptography.hazmat.primitives.ciphers.aead import AESCCM

    return AESCCM(KEY, tag_length=4)


# -------------------------------------------------------------------- SLIP


def test_crc16_check_value():
    assert crc16(b"123456789") == 0x29B1


def test_slip_roundtrip_with_escaped_bytes():
    frame = bytes([0x01, 0xC0, 0xDB, 7, 0xC0, 0])
    decoder = SlipDecoder()
    assert decoder.feed(b"\xc0\xc0" + slip_encode(frame)) == [frame]
    assert decoder.crc_errors == 0


def test_slip_decoder_handles_split_and_back_to_back_packets():
    a, b = bytes([2, 1, 0x42, 9, 0]), bytes([4, 1, 0x42, 3, 1])
    stream = slip_encode(a) + slip_encode(b)
    decoder = SlipDecoder()
    frames = []
    for byte in stream:  # one byte at a time
        frames += decoder.feed(bytes([byte]))
    assert frames == [a, b]


def test_slip_decoder_counts_corruption_and_overflow():
    packet = bytearray(slip_encode(bytes([1, 0x42, 1, 5, 3, 100])))
    packet[3] ^= 0x01
    decoder = SlipDecoder()
    assert decoder.feed(bytes(packet)) == []
    assert decoder.crc_errors == 1

    assert decoder.feed(b"\xc0" + b"\x11" * (MAX_FRAME + 5) + b"\xc0") == []
    assert decoder.overruns == 1
    # and it recovers on the next good packet
    good = bytes([2, 1, 0x42, 1, 0])
    assert decoder.feed(slip_encode(good)) == [good]


class LoopbackPort:
    """Two ends of a byte pipe with the read/write API of ``serial.Serial``."""

    def __init__(self) -> None:
        self.peer: LoopbackPort | None = None
        self._buf = bytearray()
        self._cv = threading.Condition()
        self.closed = False

    def write(self, data: bytes) -> None:
        with self.peer._cv:
            self.peer._buf += data
            self.peer._cv.notify_all()

    def read(self, n: int = 1) -> bytes:
        with self._cv:
            if not self._buf:
                self._cv.wait(timeout=0.02)
            chunk, self._buf = bytes(self._buf[:n]), self._buf[n:]
            return chunk

    def close(self) -> None:
        self.closed = True


def test_serial_transport_moves_frames_between_two_ports():
    left, right = LoopbackPort(), LoopbackPort()
    left.peer, right.peer = right, left
    got, done = [], threading.Event()
    with SerialTransport(left) as a, SerialTransport(right) as b:
        b.set_receiver(lambda frame: (got.append(frame), done.set()))
        a.send(bytes([1, 0x42, 1, 5, 3, 100]))
        assert done.wait(2.0)
    assert got == [bytes([1, 0x42, 1, 5, 3, 100])]
    assert left.closed and right.closed


def test_serial_transport_reports_a_dead_port():
    class DeadPort:
        def read(self, n=1):
            raise OSError("unplugged")

        def write(self, data):
            pass

    errors, seen = [], threading.Event()
    transport = SerialTransport(DeadPort(), on_error=lambda e: (errors.append(e), seen.set()))
    transport.start()
    assert seen.wait(2.0)
    transport.close()
    assert isinstance(errors[0], OSError)


# ------------------------------------------------------------ secure frames


def test_seal_matches_the_documented_layout_byte_for_byte():
    # header = KIND|0x80 SENDER TARGET SEQ FCNT(4, LE) TX
    # nonce  = SENDER TARGET FCNT(4) KIND|0x80 TX 00*5
    header = bytes([0x81, 0x42, 0x01, 0x05]) + (77).to_bytes(4, "little") + bytes([0x07])
    nonce = bytes([0x42, 0x01]) + (77).to_bytes(4, "little") + bytes([0x81, 0x07]) + bytes(5)
    assert header + aead().encrypt(nonce, VECTOR_PLAIN[4:], header) == VECTOR_RELAYED

    assert seal(aead(), VECTOR_PLAIN, 77) == VECTOR_SEALED
    assert seal(aead(), VECTOR_PLAIN, 77, tx=0x07) == VECTOR_RELAYED
    assert reseal(aead(), VECTOR_SEALED, 0x07) == VECTOR_RELAYED
    assert open_sealed(aead(), VECTOR_SEALED) == (VECTOR_PLAIN, 77)
    assert open_sealed(aead(), VECTOR_RELAYED) == (VECTOR_PLAIN, 77)
    assert transmitter(VECTOR_RELAYED) == 0x07


def test_the_transmitter_byte_is_authenticated():
    forged = bytearray(VECTOR_RELAYED)
    forged[8] = 0x09  # claim another relay sent it
    with pytest.raises(Exception):
        open_sealed(aead(), bytes(forged))


@pytest.mark.parametrize("payload", [0, 2, 12, 16, 17, 40, 200])
def test_seal_roundtrip_and_tamper_detection(payload):
    plain = bytes([0x01, 0x42, 1, 5]) + bytes(range(payload))
    sealed = seal(aead(), plain, 123456)
    assert open_sealed(aead(), sealed) == (plain, 123456)

    tampered = bytearray(sealed)
    tampered[-1] ^= 1
    with pytest.raises(Exception):
        open_sealed(aead(), bytes(tampered))
    header_tampered = bytearray(sealed)
    header_tampered[3] ^= 1  # the sequence number is authenticated too
    with pytest.raises(Exception):
        open_sealed(aead(), bytes(header_tampered))


def test_replay_window_matches_the_go_sequence():
    window = ReplayWindow()
    steps = [(10, True), (10, False), (12, True), (11, True), (11, False),
             (50, True), (19, True), (18, False), (12, False), (49, True)]
    for fcnt, expected in steps:
        assert window.check(fcnt) is expected
        if expected:
            window.accept(fcnt)


def test_replay_window_records_a_far_lower_counter_without_growing():
    # A node notes its own frames without check(): one far below the highest
    # counter (its clock went back, or another frame carried its id with a
    # higher one) used to shift by the whole distance and exhaust memory.
    window = ReplayWindow()
    window.accept(3_000_000_000)
    window.accept(5)
    assert window.last == 3_000_000_000
    assert window._bits.bit_length() <= 32
    assert window.check(3_000_000_000 - 31) and not window.check(5)


def test_secure_transport_delivers_plain_frames_and_hides_payloads():
    a, b = pair()
    sender = SecureTransport(a, KEY, initial_fcnt=0)
    receiver = SecureTransport(b, KEY, initial_fcnt=0)
    got = []
    receiver.set_receiver(got.append)

    sender.send(VECTOR_PLAIN)
    assert got == [VECTOR_PLAIN]
    assert a.sent[0][0] == 0x81 and VECTOR_PLAIN[4:] not in a.sent[0]


def test_every_retransmission_is_sealed_with_a_fresh_counter():
    a, b = pair()
    sender = SecureTransport(a, KEY, initial_fcnt=0)
    receiver = SecureTransport(b, KEY, initial_fcnt=0)
    got = []
    receiver.set_receiver(got.append)

    sender.send(VECTOR_PLAIN)
    sender.send(VECTOR_PLAIN)  # a retry: same plain frame

    assert got == [VECTOR_PLAIN, VECTOR_PLAIN]  # the replay window lets it through
    assert a.sent[0] != a.sent[1]


def test_a_device_reboot_keeps_only_the_controllers_replay_mark():
    # Spec 5: a device persists the controller's mark only (flash wear).
    a, b = pair()
    rejected = []
    sender = SecureTransport(a, KEY, initial_fcnt=0)
    receiver = SecureTransport(b, KEY, on_reject=lambda reason, raw: rejected.append(reason))
    got = []
    receiver.set_receiver(got.append)
    other = bytes([0x01, 0x07]) + VECTOR_PLAIN[2:]  # the same frame from sender 0x07
    sender.send(VECTOR_PLAIN)
    sender.send(other)
    first, second = a.sent

    assert receiver.mark(0x42) == 1 and receiver.mark(0x07) == 2
    receiver.reboot({0x42: receiver.mark(0x42)})
    assert receiver.mark(0x07) is None
    got.clear()
    b.receiver(first)
    b.receiver(second)
    assert rejected == ["replayed frame counter 1"] and got == [other]


def test_replayed_forged_and_plain_frames_are_rejected():
    a, b = pair()
    rejected = []
    sender = SecureTransport(a, KEY, initial_fcnt=0)
    receiver = SecureTransport(b, KEY, on_reject=lambda reason, raw: rejected.append(reason))
    got = []
    receiver.set_receiver(got.append)

    sender.send(VECTOR_PLAIN)
    b.receiver(a.sent[0])  # a recorded frame played back
    forged = bytearray(a.sent[0])
    forged[-1] ^= 1
    b.receiver(bytes(forged))
    b.receiver(VECTOR_PLAIN)  # no envelope at all

    assert got == [VECTOR_PLAIN]
    assert receiver.stats["replayed"] == 1
    assert receiver.stats["auth_failed"] == 1
    assert receiver.stats["plain_dropped"] == 1
    assert len(rejected) == 3


def test_wrong_key_is_rejected():
    a, b = pair()
    sender = SecureTransport(a, KEY, initial_fcnt=0)
    receiver = SecureTransport(b, b"another key 16B!")
    got = []
    receiver.set_receiver(got.append)
    sender.send(VECTOR_PLAIN)
    assert got == [] and receiver.stats["auth_failed"] == 1


def test_a_restarted_sender_is_ahead_of_its_earlier_run():
    first = SecureTransport(PairTransport(), KEY)
    start = first.fcnt
    assert abs(start - time.time()) < 5  # seeded from the clock, not zero


def test_key_length_is_checked():
    with pytest.raises(ValueError):
        SecureTransport(PairTransport(), b"short")


# ----------------------------------------------------- full stack end to end


def test_controller_and_device_work_through_the_secure_layer_with_loss():
    clock = FakeClock()
    medium = SimulatedMedium(
        downlink_drop=0.2, uplink_drop=0.2, latency=(0.01, 0.01), clock=clock
    )
    medium.rng.seed(4)
    device = Device(
        SecureTransport(medium.node(1), KEY, initial_fcnt=0), 1, awake=True, clock=clock
    )
    controller = Controller(
        SecureTransport(medium.gateway, KEY, initial_fcnt=0),
        timeout=0.5,
        max_retries=8,
        clock=clock,
    )
    futures = [controller.request(1, 3, value) for value in range(5)]
    for _ in range(3000):
        clock.now += 0.01
        medium.tick()
        device.tick()
        controller.tick()

    assert all(f.result(timeout=0).ok for f in futures)
    assert device.handled_count == 5  # retries never ran an action twice


def test_session_serial_wires_rejections_and_port_errors_to_events():
    left, right = LoopbackPort(), LoopbackPort()
    left.peer, right.peer = right, left
    session = LinkSession.serial(left, nodes=[1], key=KEY, timeout=0.05)
    events = []
    session.events.subscribe(events.append)

    with session:
        # a module that does not know the key answers with a plain frame
        peer = SerialTransport(right)
        peer.start()
        peer.send(bytes([0x04, 1, 0x42, 0, 1]))
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and not any(isinstance(e, FrameRejected) for e in events):
            time.sleep(0.01)
        peer.close()

    assert any(isinstance(e, FrameRejected) for e in events)
    assert left.closed
