"""``tests/testbed/rxfilter.FilteredPort``: the gateway-side test receive filter.

Held here: it starts only on purpose (topo passed + ``CAIRN_TESTFILTER=1``); the MIC is checked first
and the key is TX; a dropped frame never reaches the stack (no replay window, relay window,
neighbour table, ``_heard_at`` or counter moves); with nobody excluded it is frame-for-frame the same
as no wrapper; an asymmetric rule is explicit and marked NONPHYSICAL; only drops are logged.
"""

from __future__ import annotations

import logging
import random
import sys
from pathlib import Path

import pytest

pytest.importorskip("cryptography")

from cryptography.hazmat.primitives.ciphers.aead import AESCCM

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from cairnmesh.bridge import GatewayBridge
from cairnmesh.frames import ReplyFrame, RequestFrame
from cairnmesh.mesh import MeshRelay
from cairnmesh.secure import MIC_LEN, SecureTransport, reseal, seal
from tests.testbed import rxfilter as F
from tests.testbed import topo as T

KEY = bytes(range(16))
AEAD = AESCCM(KEY, tag_length=MIC_LEN)
WEAK = -115.0
ME = 5  # the virtual node under test


@pytest.fixture(autouse=True)
def testbed_env(monkeypatch):
    """Opt in for this file (tests/conftest.py lets a test with this fixture keep a filter open)."""

    monkeypatch.setenv(F.ENV, "1")
    yield
    F.close_all()


class FakeClock:
    now = 0.0

    def __call__(self) -> float:
        return self.now


class Radio:
    def __init__(self) -> None:
        self.sent: list[bytes] = []
        self.receiver = None
        self.channel = None

    def send(self, data: bytes, preamble: int = 8) -> None:
        self.sent.append(data)

    def set_receiver(self, callback) -> None:
        self.receiver = callback

    def set_radio(self, channel: int, sf: int) -> None:
        self.channel = (channel, sf)

    def busy_until(self) -> float:
        return 12.5


def request(seq=1, fcnt=1, sender=0x42, target=9) -> bytes:
    return seal(AEAD, RequestFrame(sender_id=sender, target_id=target, sequence=seq, epoch=7, action=3, value=1).encode(), fcnt)


def reply(seq=1, fcnt=1, sender=9, target=0x42) -> bytes:
    return seal(AEAD, ReplyFrame(sender_id=sender, target_id=target, sequence=seq, epoch=7, result=0).encode(), fcnt)


def by(raw: bytes, tx: int) -> bytes:
    """``raw`` as node ``tx`` puts it on the air (only TX differs)."""

    return reseal(AEAD, raw, tx)


def node(radio, clock, **options) -> MeshRelay:
    return MeshRelay(radio, KEY, ME, clock=clock, rng=random.Random(1), **options)


def settle(n: MeshRelay, clock: FakeClock, seconds: float = 5.0) -> None:
    for _ in range(int(seconds / 0.01)):
        clock.now += 0.01
        n.tick(clock.now)


def snapshot(n: MeshRelay, radio: Radio) -> dict:
    return {
        "sent": list(radio.sent),
        "stats": dict(n.stats),
        "links": list(n._links.items()),
        "heard_at": dict(n._heard_at),
        "windows": {o: (w._last, w._bits, w._used) for o, w in n._windows.items()},
    }


# --------------------------------------------------------- starts only on purpose


def test_needs_the_environment_variable(monkeypatch):
    monkeypatch.delenv(F.ENV)
    with pytest.raises(F.FilterError, match=F.ENV):
        F.FilteredPort(Radio(), key=KEY, node=ME, topo="5-1")
    monkeypatch.setenv(F.ENV, "0")
    with pytest.raises(F.FilterError, match=F.ENV):
        F.FilteredPort(Radio(), key=KEY, node=ME, topo="5-1")
    assert F.active_filters() == []


@pytest.mark.parametrize("topo", [None, "", "  "])
def test_needs_a_topology(topo):
    with pytest.raises(F.FilterError, match="topo"):
        F.FilteredPort(Radio(), key=KEY, node=ME, topo=topo)
    assert F.active_filters() == []


def test_topo_is_keyword_only_and_has_no_default():
    with pytest.raises(TypeError):
        F.FilteredPort(Radio(), KEY, ME)  # type: ignore[misc]
    with pytest.raises(TypeError):
        F.FilteredPort(Radio(), key=KEY, node=ME)  # type: ignore[call-arg]


def test_a_bad_topology_or_a_node_outside_it_is_refused():
    with pytest.raises(T.TopoError):
        F.FilteredPort(Radio(), key=KEY, node=ME, topo="5>1")
    with pytest.raises(F.FilterError, match="hear nothing"):
        F.FilteredPort(Radio(), key=KEY, node=ME, topo="1-2")
    assert F.active_filters() == []


def test_a_refused_start_leaves_the_radio_alone():
    radio = Radio()
    with pytest.raises(F.FilterError):
        F.FilteredPort(radio, key=KEY, node=ME, topo="1-2")
    assert radio.receiver is None


def test_it_announces_itself(caplog):
    with caplog.at_level(logging.WARNING, logger="cairnmesh.testbed"):
        f = F.FilteredPort(Radio(), key=KEY, node=ME, topo="5-1, 2-5")
    assert f.banner == "TESTFILTER ACTIVE node=5 topo=1-5,2-5 mode=topo"
    assert any(f.banner in r.getMessage() for r in caplog.records)


def test_asymmetry_must_be_named_and_marked_nonphysical():
    for bad in (
        dict(asym_hear=[1]),
        dict(asym_hear=[1], asym_name="x"),
        dict(asym_hear=[1], nonphysical=True),
        dict(asym_name="x", nonphysical=True),
    ):
        with pytest.raises(F.FilterError, match="asymmetric"):
            F.FilteredPort(Radio(), key=KEY, node=ME, topo="5-1,5-2", **bad)
    f = F.FilteredPort(Radio(), key=KEY, node=ME, topo="5-1,5-2", asym_hear=[2], asym_name="uplink-only", nonphysical=True)
    assert "NONPHYSICAL" in f.banner and "uplink-only" in f.banner
    assert f.status()["mode"] == "asym" and f.status()["hear"] == [2]


# ------------------------------------------------------------ what it drops


def test_the_key_is_tx_not_sender():
    radio, clock = Radio(), FakeClock()
    f = F.FilteredPort(radio, key=KEY, node=ME, topo="5-1")  # hears 1 only
    n = node(f, clock)
    # SENDER is 0x42 in all of them: only the transmitter decides.
    radio.receiver(by(request(fcnt=1), 1), WEAK)  # neighbour
    radio.receiver(by(request(fcnt=2), 7), WEAK)  # not a neighbour
    radio.receiver(request(fcnt=3), WEAK)  # TX == SENDER == 0x42, not a neighbour
    assert (f.passed, f.dropped, f.unauth) == (1, 2, 0)
    assert n._windows[0x42].last == 1  # only fcnt 1 reached the stack


def test_a_dropped_frame_moves_nothing_in_the_stack():
    base_radio, base_clock = Radio(), FakeClock()
    base = node(base_radio, base_clock)  # the same node, never shown the frames
    radio, clock = Radio(), FakeClock()
    f = F.FilteredPort(radio, key=KEY, node=ME, topo="5-1")
    n = node(f, clock)
    for raw in (by(request(fcnt=10), 7), by(reply(fcnt=11), 8), by(request(fcnt=12, sender=0x43), 9)):
        radio.receiver(raw, WEAK)
        settle(n, clock)
        settle(base, base_clock)
        assert snapshot(n, radio) == snapshot(base, base_radio)
    assert f.dropped == 3 and n._windows == {} and n._heard_at == {} and list(n._links) == []
    # The windows really are untouched: the same counters are fresh when a neighbour carries them.
    radio.receiver(by(request(fcnt=10), 1), WEAK)
    settle(n, clock)
    assert n.stats["relayed"] == 1 and n.stats["duplicates"] == 0
    assert n._windows[0x42].last == 10 and 1 in n._heard_at and 7 not in n._heard_at


def test_a_dropped_frame_does_not_move_a_secure_link_replay_window():
    class Wire:
        receiver = None

        def send(self, data):
            pass

        def set_receiver(self, callback):
            self.receiver = callback

    wire = Wire()
    f = F.FilteredPort(wire, key=KEY, node=ME, topo="5-1")
    secure = SecureTransport(f, KEY, initial_fcnt=0)  # type: ignore[arg-type]
    got: list[bytes] = []
    secure.set_receiver(got.append)
    wire.receiver(by(request(fcnt=50), 7))
    assert secure.mark(0x42) is None and got == [] and secure.stats["opened"] == 0
    wire.receiver(by(request(fcnt=50), 1))
    assert secure.mark(0x42) == 50 and len(got) == 1


def test_unauthenticated_frames_go_to_the_stack_which_judges_them():
    radio, clock = Radio(), FakeClock()
    f = F.FilteredPort(radio, key=KEY, node=ME, topo="5-1")
    n = node(f, clock)
    good = by(request(), 7)
    forged = bytearray(good)
    forged[-1] ^= 1
    for raw in (bytes(forged), good[:5], b"\x01\x02\x03\x04\x05", b""):
        radio.receiver(raw, WEAK)
    assert (f.unauth, f.dropped, f.passed) == (4, 0, 0)
    assert n.stats["auth_failed"] == 2 and n.stats["plain_dropped"] == 1
    assert len(f.log) == 0  # only drops are logged


def test_a_frame_under_another_key_is_unauthenticated_not_dropped():
    radio, clock = Radio(), FakeClock()
    f = F.FilteredPort(radio, key=KEY, node=ME, topo="5-1")
    n = node(f, clock)
    other = AESCCM(bytes(16), tag_length=MIC_LEN)
    radio.receiver(seal(other, RequestFrame(sender_id=1, target_id=9, sequence=1, epoch=7, action=3, value=1).encode(), 1, tx=7), WEAK)
    assert f.unauth == 1 and f.dropped == 0 and n.stats["auth_failed"] == 1


def test_the_drop_log_has_the_fields_and_only_drops():
    radio = Radio()
    f = F.FilteredPort(radio, key=KEY, node=ME, topo="5-1")
    node(f, FakeClock())
    kept, lost = by(request(fcnt=21), 1), by(request(seq=2, fcnt=22), 7)
    radio.receiver(kept, WEAK)
    radio.receiver(lost, -97.5)
    assert len(f.log) == 1
    rec = f.log[0]
    assert (rec.sender, rec.tx, rec.fcnt, rec.rssi) == (0x42, 7, 22, -97.5)
    assert rec.kind == lost[0] & 0x7F
    import hashlib

    assert rec.mic_hash == hashlib.sha256(lost[-MIC_LEN:]).hexdigest()[:8]
    text = repr(rec)
    assert KEY.hex() not in text and bytes(lost[9:-MIC_LEN]).hex() not in text  # no key, no ciphertext
    assert f.status() == {"node": 5, "mode": "topo", "name": None, "hear": [1], "passed": 1, "dropped": 1, "unauth": 0, "suspect": None}


def test_the_log_is_bounded():
    radio = Radio()
    f = F.FilteredPort(radio, key=KEY, node=ME, topo="5-1", log_size=4)
    node(f, FakeClock())
    for i in range(10):
        radio.receiver(by(request(fcnt=i + 1), 7), WEAK)
    assert f.dropped == 10 and [r.fcnt for r in f.log] == [7, 8, 9, 10]


def test_asymmetric_rule_overrides_the_topology():
    radio, clock = Radio(), FakeClock()
    f = F.FilteredPort(radio, key=KEY, node=ME, topo="5-1,5-2", asym_hear=[2], asym_name="only-2", nonphysical=True)
    n = node(f, clock)
    radio.receiver(by(request(fcnt=1), 1), WEAK)  # a topology neighbour, but the rule drops it
    radio.receiver(by(request(fcnt=2), 2), WEAK)
    assert (f.passed, f.dropped) == (1, 1) and set(n._heard_at) == {2}


# --------------------------------------------- nobody excluded == no wrapper


def script(seed: int):
    """A mixed run: fresh frames, repeats, relayed copies from three neighbours, noise."""

    rng = random.Random(seed)
    steps = []
    for i in range(40):
        kind = rng.choice(["req", "rep", "dup", "tamper", "short", "plain"])
        tx = rng.choice([1, 2, 3])
        origin = rng.choice([0x42, 0x43, 9])
        fcnt = rng.randint(1, 12)
        raw = request(seq=rng.randint(1, 3), fcnt=fcnt, sender=origin) if kind != "rep" else reply(seq=rng.randint(1, 3), fcnt=fcnt, sender=origin)
        raw = by(raw, tx)
        if kind == "tamper":
            raw = raw[:-1] + bytes([raw[-1] ^ 0x55])
        elif kind == "short":
            raw = raw[:6]
        elif kind == "plain":
            raw = bytes([0x01, 0x02, 0x03, 0x04, 0x05])
        steps.append((raw, rng.choice([None, -60.0, -110.0])))
        if kind == "dup":
            steps.append((raw, -80.0))
    return steps


@pytest.mark.parametrize("seed", range(12))
def test_with_nobody_excluded_it_is_frame_for_frame_the_plain_radio(seed):
    plain_radio, plain_clock = Radio(), FakeClock()
    plain = node(plain_radio, plain_clock)
    radio, clock = Radio(), FakeClock()
    f = F.FilteredPort(radio, key=KEY, node=ME, topo=T.full_mesh([1, 2, 3, ME]))
    wrapped = node(f, clock)
    for raw, rssi in script(seed):
        for r, rx in ((plain_radio, plain_radio.receiver), (radio, radio.receiver)):
            rx(raw) if rssi is None else rx(raw, rssi)
        for n, c in ((plain, plain_clock), (wrapped, clock)):
            settle(n, c, 0.3)
        assert snapshot(wrapped, radio) == snapshot(plain, plain_radio)
    settle(plain, plain_clock)
    settle(wrapped, clock)
    assert snapshot(wrapped, radio) == snapshot(plain, plain_radio)
    assert f.dropped == 0 and f.passed + f.unauth == len(script(seed))


def test_every_call_reaches_the_stack_with_the_same_arguments():
    seen = []
    radio = Radio()
    f = F.FilteredPort(radio, key=KEY, node=ME, topo="5-1")
    f.set_receiver(lambda *a: seen.append(a))
    good = by(request(), 1)
    radio.receiver(good, -71.0)
    radio.receiver(good)
    radio.receiver(b"\x00")
    assert seen == [(good, -71.0), (good,), (b"\x00",)]


# ------------------------------------------------------- the radio's own API


def test_everything_else_is_the_real_radios():
    radio = Radio()
    f = F.FilteredPort(radio, key=KEY, node=ME, topo="5-1")
    f.send(b"abc", preamble=99)
    f.set_radio(2, 9)
    assert radio.sent == [b"abc"] and radio.channel == (2, 9) and f.busy_until() == 12.5
    with pytest.raises(AttributeError):
        f.no_such_thing


def test_close_gives_the_stack_the_radio_back_and_leaves_the_registry():
    radio, clock = Radio(), FakeClock()
    f = F.FilteredPort(radio, key=KEY, node=ME, topo="5-1")
    n = node(f, clock)
    assert F.active_filters() == [f]
    f.close()
    assert F.active_filters() == [] and radio.receiver == n._on_radio
    radio.receiver(by(request(), 7), WEAK)  # no filter now
    assert n._windows[0x42].last == 1


def test_wrapping_a_real_bridge_radio():
    class Transport:
        receiver = None

        def send(self, data):
            pass

        def set_receiver(self, callback):
            self.receiver = callback

    transport = Transport()
    bridge = GatewayBridge(transport)
    got = []
    filtered = [F.FilteredPort(p, key=KEY, node=nid, topo="5-1,6-1") for p, nid in zip(bridge.ports, (5, 6))]
    for p in filtered:
        p.set_receiver(lambda frame, rssi, p=p: got.append((p.node, frame, rssi)))

    def rx(radio: int, frame: bytes, rssi: int = -90) -> bytes:
        return bytes([0x81, radio]) + rssi.to_bytes(2, "big", signed=True) + bytes([0]) + frame

    from_1, from_5 = by(request(fcnt=3), 1), by(request(fcnt=4), 5)
    transport.receiver(rx(0, from_1))  # port 0 is node 5: hears 1
    transport.receiver(rx(1, from_1))  # port 1 is node 6: hears 1
    transport.receiver(rx(0, from_5))  # node 5 does not hear itself
    transport.receiver(rx(1, from_5, -50))  # node 6 does not hear 5 (no 5-6 link)
    assert got == [(5, from_1, -90.0), (6, from_1, -90.0)]
    assert [(p.passed, p.dropped) for p in filtered] == [(1, 1), (1, 1)]
    assert filtered[0].index == 0 and filtered[1].index == 1  # BridgeRadio's own attributes show through


# ------------------------------------------------- fail-open: a wrong key passes everything


WRONG = bytes(16)


def noisy(f: F.FilteredPort, radio: Radio, count: int, good: bytes | None = None) -> None:
    for i in range(count):
        radio.receiver(by(request(fcnt=i + 1), 7) if good is None else good, WEAK)


def test_a_wrong_key_is_flagged_not_silent(caplog):
    radio, clock = Radio(), FakeClock()
    f = F.FilteredPort(radio, key=WRONG, node=ME, topo="5-1")  # the stack's frames use KEY
    n = node(f, clock)
    with caplog.at_level(logging.WARNING, logger="cairnmesh.testbed"):
        noisy(f, radio, 4)
        assert f.suspect() is None and f.status()["suspect"] is None  # below the bar
        noisy(f, radio, 6)
    assert f.dropped == 0 and f.passed == 0 and f.unauth == 10
    assert "wrong key" in f.suspect() and "wrong key" in f.status()["suspect"]
    warned = [r for r in caplog.records if "TESTFILTER SUSPECT" in r.getMessage()]
    assert len(warned) == 1  # once, not per frame
    with pytest.raises(F.FilterError, match="wrong key"):
        f.check()


def test_strict_raises_on_the_frame_that_shows_it_and_again_at_close():
    radio, clock = Radio(), FakeClock()
    f = F.FilteredPort(radio, key=WRONG, node=ME, topo="5-1", strict=True, suspect_after=3)
    node(f, clock)
    noisy(f, radio, 2)
    with pytest.raises(F.FilterError, match="wrong key"):
        noisy(f, radio, 1)
    with pytest.raises(F.FilterError, match="wrong key"):
        F.close_all()  # teardown of the run that used it fails too
    assert F.active_filters() == []  # and it is closed regardless


def test_a_healthy_filter_with_some_noise_is_not_suspect():
    radio, clock = Radio(), FakeClock()
    f = F.FilteredPort(radio, key=KEY, node=ME, topo="5-1", strict=True)
    node(f, clock)
    for i in range(10):
        radio.receiver(by(request(fcnt=i + 1), 1 if i % 2 else 7), WEAK)
    for _ in range(8):  # authenticated frames still outnumber the garbage
        radio.receiver(b"\x01\x02\x03\x04\x05", WEAK)
    assert f.unauth == 8 and f.passed + f.dropped == 10 and f.suspect() is None
    f.check()
    f.close()
