"""The bench's air seen from outside: an RTL-SDR decodes, times and measures the gateway's frames.

``firmware/tools/lora_sdr.py`` records the channel while the gateway
firmware sends through the bridge; what the SDR decodes must be what was
sent, in the time the formula gives, on the channel table's frequency,
inside its 125 kHz. Skipped without the board or the SDR (``-m hardware``).
The SDR's own frequency error is measured on channel 0 against the radios,
whose crystals a spectrum analyzer showed to be accurate. Numbers go to
``firmware/results/hw-test-<date>.json`` under ``sdr_*``, the recordings to
``firmware/results/sdr/``.
"""

from __future__ import annotations

import sys
import threading
import time

import pytest

from board import FIRMWARE

sys.path.insert(0, str(FIRMWARE / "tools"))

np = pytest.importorskip("numpy")
import lora_sdr  # noqa: E402
from bridge_bench import tuned  # noqa: E402

from cairnmesh.airtime import lora_airtime  # noqa: E402
from cairnmesh.bridge import TX_OK  # noqa: E402
from cairnmesh.energy import symbol_time  # noqa: E402

pytestmark = pytest.mark.hardware

RECORDINGS = FIRMWARE / "results" / "sdr"
GAIN = 30.0  # at -9 dBm a desk away: about -11 dBFS, no clipping (2026-10-26)
LENGTHS = (1, 16, 64, 255)
# The SDR times a frame from its carrier's rise, before the first symbol,
# to its fall: 0.17-0.21 ms longer than the formula at SF7, SF9 and SF12
# alike (2026-10-26), a fixed time, not symbols.
AIR_SLACK = 0.0005


@pytest.fixture(scope="module")
def sdr():
    reason = lora_sdr.available()
    if reason:
        pytest.skip(reason)
    RECORDINGS.mkdir(parents=True, exist_ok=True)


class Gateway:
    """The bridge, with every TX_DONE kept."""

    def __init__(self, board, ldflags: str = "") -> None:
        board.flash("gateway", ldflags)
        time.sleep(1.0)
        self.bridge, self.transport = board.bridge()
        self.done = []
        self.heard: list[list[bytes]] = [[], []]
        self.cond = threading.Condition()
        self.bridge.on_tx_done = self._done
        for i, port in enumerate(self.bridge.ports):
            port.set_receiver(lambda frame, rssi, i=i: self.heard[i].append(frame))
        assert self.bridge.ping(timeout=2.0) is not None

    def _done(self, done) -> None:
        with self.cond:
            self.done.append(done)
            self.cond.notify_all()

    def send(self, radio: int, frame: bytes, sf: int = 7, **kw) -> None:
        """Send and wait for its TX_DONE."""

        with self.cond:
            n = len(self.done)
        self.bridge.ports[radio].send(frame, **kw)
        with self.cond:
            assert self.cond.wait_for(lambda: len(self.done) > n, lora_airtime(len(frame), sf) * 3 + 2.0)

    def tune(self, channel: int, sf: int) -> None:
        for port in self.bridge.ports:
            port.set_radio(channel, sf)
        assert tuned(self.bridge, channel, sf, timeout=2.0), f"TUNE to channel {channel} SF{sf}"

    def close(self) -> None:
        if self.transport is not None:
            self.transport.close()
            self.transport = None


@pytest.fixture(scope="module")
def gateway(board, sdr):
    g = Gateway(board)
    yield g
    g.close()


@pytest.fixture(scope="module")
def sdr_ppm():
    """Filled by the first test: the SDR's error measured on channel 0."""

    return {}


def on_air(name: str, channel: int, seconds: float, act, gain: float = GAIN):
    with lora_sdr.Capture(lora_sdr.channel_hz(channel), seconds, RECORDINGS / f"{name}.u8", gain=gain) as cap:
        time.sleep(0.2)
        act()
        return cap.wait()


def body(radio: int, index: int, n: int) -> bytes:
    return (bytes([0xA0 | radio << 4 | index]) + bytes(range(1, 256)))[:n]


def test_sdr_decodes_what_both_radios_send(gateway, record, sdr_ppm):
    """Channel 0, SF7: 1-, 16-, 64- and 255-byte frames from each radio, decoded byte for byte."""

    sent = [(r, body(r, i, n)) for r in (0, 1) for i, n in enumerate(LENGTHS)]

    def act():
        for radio, frame in sent:
            gateway.send(radio, frame)
            time.sleep(0.1)

    rec = on_air("ch0-sf7", 0, 4.5, act)
    frames = lora_sdr.decode(rec, 7)
    by_payload = {f.payload: f for f in frames if f.ok}
    rows = []
    for radio, frame in sent:
        f = by_payload.get(frame)
        rows.append({
            "radio": radio, "len": len(frame), "decoded": f is not None,
            **({} if f is None else {
                "air_ms": round(f.airtime * 1000, 3),
                "air_minus_formula_ms": round((f.airtime - lora_airtime(len(frame), 7)) * 1000, 3),
                "freq_hz": round(f.freq_hz), "snr_db": round(f.snr_db, 1), "preamble": f.preamble,
            }),
        })
    ok = [by_payload[fr] for _, fr in sent if fr in by_payload]
    ppm = lora_sdr.ppm_from(ok, lora_sdr.CHANNEL0_HZ) if ok else None
    sdr_ppm["ppm"] = ppm
    radio_hz = {r: float(np.median([by_payload[fr].corrected_hz(ppm) for rr, fr in sent if rr == r and fr in by_payload]))
                for r in (0, 1)} if ppm is not None else {}
    record["sdr_decode"] = {
        "frames": rows,
        "bursts_seen": len(frames),
        "sdr_ppm": None if ppm is None else round(ppm, 3),
        "radio1_minus_radio0_hz": round(radio_hz[1] - radio_hz[0]) if len(radio_hz) == 2 else None,
    }
    assert len(ok) == len(sent), [r for r in rows if not r["decoded"]]
    assert len(frames) == len(sent), "a burst that is no frame of ours"
    for f in ok:
        assert f.preamble == 8 and f.sync == lora_sdr.SYNC_WORD
        assert f.decoded.cr == 1 and f.decoded.has_crc and f.decoded.fec_errors == 0
    assert all(0 <= r["air_minus_formula_ms"] / 1000 <= AIR_SLACK for r in rows), rows
    assert abs(ppm) < 30, "the SDR is further off than an RTL dongle should be"
    # Both radios on the same frequency, measured against each other (the SDR's error cancels).
    assert abs(radio_hz[1] - radio_hz[0]) < 1000, radio_hz


def test_sdr_session_channels(gateway, record, sdr_ppm):
    """TUNE: channel 1 at SF9 and channel 31 at SF12 (low data rate on): frequency of the table, frames whole."""

    ppm = sdr_ppm.get("ppm")
    if ppm is None:
        pytest.skip("no SDR error from channel 0")
    results = {}
    try:
        for channel, sf in ((1, 9), (31, 12)):
            gateway.tune(channel, sf)
            sent = [(r, body(r, 7, 16)) for r in (0, 1)]

            def act():
                for radio, frame in sent:
                    gateway.send(radio, frame, sf=sf)
                    time.sleep(0.1)

            seconds = 2 * lora_airtime(16, sf) + 1.5
            rec = on_air(f"ch{channel}-sf{sf}", channel, seconds, act)
            frames = [f for f in lora_sdr.decode(rec, sf) if f.ok]
            errors = [round(f.corrected_hz(ppm) - lora_sdr.channel_hz(channel)) for f in frames]
            results[f"ch{channel}_sf{sf}"] = {
                "nominal_hz": lora_sdr.channel_hz(channel),
                "decoded": sorted(f.payload.hex() for f in frames),
                "freq_error_hz": errors,
                "air_minus_formula_ms": [round((f.airtime - lora_airtime(16, sf)) * 1000, 2) for f in frames],
            }
            assert sorted(f.payload for f in frames) == sorted(fr for _, fr in sent), results
            # 2 ppm: the radios' crystals and what is left of the SDR's after the correction.
            assert all(abs(e) < 2e-6 * lora_sdr.channel_hz(channel) for e in errors), results
            assert all(0 <= (f.airtime - lora_airtime(16, sf)) <= AIR_SLACK for f in frames), results
    finally:
        gateway.tune(0, 7)
        record["sdr_session_channels"] = results


def test_sdr_air_timing_of_the_rules(gateway, record):
    """On the air: rule 1 (one after the other), rule 2 (answer window; urgent skips it), a 200-symbol preamble."""

    window = 0.5
    f1a, f1b = body(0, 1, 16), body(1, 1, 16)
    f2a, f2b = body(0, 2, 16), body(1, 2, 16)
    f3a, f3b = body(0, 3, 16), body(1, 3, 16)
    f4 = body(0, 4, 16)
    air = lora_airtime(16, 7)

    def act():
        # rule 1: both at once
        gateway.bridge.ports[0].send(f1a)
        gateway.bridge.ports[1].send(f1b)
        time.sleep(2 * air + 0.4)
        # rule 2, not urgent: radio 1 waits for radio 0's answer window
        gateway.bridge.ports[0].send(f2a, answer=window)
        gateway.bridge.ports[1].send(f2b, urgent=False)
        time.sleep(2 * air + window + 0.4)
        # rule 2, urgent: it does not
        gateway.bridge.ports[0].send(f3a, answer=window)
        gateway.bridge.ports[1].send(f3b, urgent=True)
        time.sleep(2 * air + window + 0.4)
        gateway.send(0, f4, preamble=200)

    rec = on_air("ch0-rules", 0, 5.5, act)
    frames = {f.payload: f for f in lora_sdr.decode(rec, 7) if f.ok}
    missing = [x.hex() for x in (f1a, f1b, f2a, f2b, f3a, f3b, f4) if x not in frames]
    assert not missing, missing

    def gap(a, b):
        return frames[b].start - frames[a].end

    long_air = air + (200 - 8) * symbol_time(7)
    result = {
        "rule1_gap_ms": round(gap(f1a, f1b) * 1000, 2),
        "rule2_gap_ms": round(gap(f2a, f2b) * 1000, 2),
        "rule2_urgent_gap_ms": round(gap(f3a, f3b) * 1000, 2),
        "long_preamble_symbols": frames[f4].preamble,
        "long_air_minus_formula_ms": round((frames[f4].airtime - long_air) * 1000, 2),
    }
    record["sdr_rules"] = result
    # Rule 1 and rule 4: radio 1 starts once radio 0's frame is over (and heard).
    assert 0 <= result["rule1_gap_ms"] < 50, result
    assert abs(result["rule2_gap_ms"] - window * 1000) < 30, result
    assert 0 <= result["rule2_urgent_gap_ms"] < 50, result
    assert result["long_preamble_symbols"] == 200, result
    assert 0 <= result["long_air_minus_formula_ms"] <= AIR_SLACK * 1000, result
    assert f1a in gateway.heard[1] and f1b in gateway.heard[0]


def test_sdr_spectrum(board, gateway, record):
    """+10 dBm, 64-byte frames: about 125 kHz occupied, centred, nothing seen in the ±250 kHz neighbours."""

    gateway.close()
    loud = Gateway(board, "-X main.powerDBm=10")  # the board fixture flashes the plain gateway back
    try:
        def act():
            for i in range(6):
                loud.send(0, body(0, i, 64))
                time.sleep(0.1)

        rec = on_air("ch0-spectrum", 0, 2.5, act, gain=15.7)
        frames = [f for f in lora_sdr.decode(rec, 7) if f.ok]
        check = lora_sdr.check_spectrum(rec, frames)
    finally:
        loud.close()
    clip = float(np.mean(np.abs(np.r_[rec.iq.real, rec.iq.imag]) > 126))
    result = {
        "frames": len(frames),
        "snr_db": round(float(np.median([f.snr_db for f in frames])), 1) if frames else None,
        "occupied_99_hz": round(check.occupied_hz),
        "centre_offset_hz": round(check.centre_hz),
        "adjacent_dbc": check.adjacent_dbc,
        "adjacent_visible_down_to_dbc": check.adjacent_floor_dbc,
        "clipped_fraction": clip,
    }
    record["sdr_spectrum"] = result
    assert len(frames) == 6 and clip < 1e-4, result
    assert 115e3 <= check.occupied_hz <= 135e3, result
    assert abs(check.centre_hz) < 5e3, result
    # Not above what the SDR can see: no frame energy in a neighbouring session channel.
    for side in (-1, 1):
        a = check.adjacent_dbc[side]
        assert a is None or a < -30, result
