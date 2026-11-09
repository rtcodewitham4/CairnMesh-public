"""The SDR observer's LoRa PHY (``firmware/tools/lora_sdr.py``), without the SDR.

The golden symbols are what the bench's SX1262s sent on 2026-10-26, read
by the SDR: they pin the coding conventions to the chip, not to this
module's own modulator. The rest is the modulator against the demodulator.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "firmware" / "tools"))
import lora_sdr as L  # noqa: E402

# SF7, CR 4/5, CRC on: b"SDRTEST\x00--------" from radio 0.
SF7_16 = [89, 13, 29, 13, 113, 29, 97, 41, 16, 22, 40, 86, 127, 101, 108, 113, 109, 66, 63, 44, 63, 61, 104, 85,
          3, 18, 4, 65, 29, 61, 83, 95, 35, 50, 70, 81, 92, 30]
# SF7, one byte (0xA0): the CRC of a payload shorter than two bytes.
SF7_1 = [17, 49, 1, 13, 25, 29, 5, 101, 126, 121, 96, 31, 1]
# SF12 on channel 31, low data rate on: 0xA7 01 02 ... 0F.
SF12_16 = [1445, 1521, 669, 365, 3649, 937, 3177, 1333, 105, 2249, 1705, 2865, 3281, 1165, 229, 305, 1017, 3609,
           2985, 3885, 3157, 937, 1633, 3149, 3797, 3061, 3789, 2937]


@pytest.mark.parametrize(
    "symbols, sf, payload",
    [
        (SF7_16, 7, b"SDRTEST\x00--------"),
        (SF7_1, 7, b"\xa0"),
        (SF12_16, 12, bytes([0xA7]) + bytes(range(1, 16))),
    ],
)
def test_chip_symbols_decode_to_the_bytes_sent(symbols, sf, payload):
    d = L.decode_symbols(symbols, sf)
    assert d.header_ok and d.cr == 1 and d.has_crc and d.length == len(payload)
    assert d.payload == payload and d.crc_ok and d.fec_errors == 0
    assert d.symbols_used == len(symbols) == L.symbols_needed(len(payload), sf, 1, True, L.low_data_rate(sf))


@pytest.mark.parametrize("symbols, sf, payload", [(SF7_16, 7, b"SDRTEST\x00--------"),
                                                  (SF12_16, 12, bytes([0xA7]) + bytes(range(1, 16)))])
def test_encoder_matches_the_chip_but_its_padding(symbols, sf, payload):
    """All blocks match; the last one holds the chip's padding bits, which are not zeros."""

    enc = L.encode(payload, sf)
    last = 4 + 1
    assert len(enc) == len(symbols)
    assert enc[: len(enc) - last] == symbols[: len(symbols) - last]


def test_a_flipped_bit_breaks_the_crc_or_the_parity():
    bad = list(SF7_16)
    bad[20] ^= 1  # one bin off: one bit of the Gray code
    d = L.decode_symbols(bad, 7)
    assert d.header_ok and (d.crc_ok is False or d.fec_errors > 0)


def test_header_corrects_a_wrong_symbol_not_half_of_them():
    """The header is 4/8 Hamming, interleaved: one wrong symbol is one bit per codeword."""

    bad = list(SF7_16)
    bad[0] = (bad[0] + 4 * 5) % 128
    d = L.decode_symbols(bad, 7)
    assert d.header_ok and d.payload == b"SDRTEST\x00--------" and d.fec_errors > 0
    bad[:4] = [1, 1, 1, 1]
    assert not L.decode_symbols(bad, 7).header_ok


def test_whitening_and_header_checksum_known_values():
    assert L.whitening(8) == [0xFF, 0xFE, 0xFC, 0xF8, 0xF0, 0xE1, 0xC2, 0x85]
    assert L.header_nibbles(16, 1, True) == [1, 0, 3, 1, 13]
    assert L.payload_crc(b"SDRTEST\x00--------") == 0xF641


def test_channel_table_matches_the_gateway():
    assert L.channel_hz(0) == 433_500_000
    assert L.channel_hz(1) == 444_000_000
    assert L.channel_hz(31) == 451_500_000
    with pytest.raises(ValueError):
        L.channel_hz(32)


def _recording(x, rng, *, amp=20.0, noise=3.0, lead=0.5, delay=0):
    pad = np.zeros(int(len(x) * lead) + delay, complex)
    sig = np.concatenate([pad, x, np.zeros(int(len(x) * lead), complex)]) * amp
    sig = sig * np.exp(2j * np.pi * L.OFFSET / L.FS * np.arange(len(sig)))
    sig = sig + noise * (rng.normal(size=len(sig)) + 1j * rng.normal(size=len(sig)))
    return L.Recording(sig.astype(np.complex64), L.CHANNEL0_HZ - L.OFFSET)


@pytest.mark.parametrize("sf", range(7, 13))
def test_modulated_frames_come_back(sf):
    rng = np.random.default_rng(sf)
    payload = bytes(rng.integers(0, 256, 12, dtype=np.uint8))
    cfo = float(rng.uniform(-4, 4))
    rec = _recording(L.modulate(payload, sf, cfo_bins=cfo), rng, delay=int(rng.integers(0, 16)))
    (frame,) = L.decode(rec, sf)
    assert frame.ok and frame.payload == payload
    assert frame.preamble == 8 and frame.sync == L.SYNC_WORD
    assert abs((frame.freq_hz - rec.channel_hz) - cfo * L.BW / (1 << sf)) < 0.05 * L.BW / (1 << sf)
    expected = len(L.modulate(payload, sf)) / L.FS
    assert abs(frame.airtime - expected) < 2 / L.BW


def test_two_frames_their_gap_and_a_long_preamble():
    rng = np.random.default_rng(7)
    a, b = L.modulate(b"first", 7), L.modulate(b"second", 7, preamble=200)
    gap = np.zeros(int(0.0015 * L.FS), complex)
    rec = _recording(np.concatenate([a, gap, b]), rng)
    first, second = L.decode(rec, 7)
    assert (first.payload, second.payload) == (b"first", b"second")
    assert second.preamble == 200
    assert abs((second.start - first.end) - 0.0015) < 0.0001


def test_spectrum_of_a_frame_is_its_bandwidth():
    rng = np.random.default_rng(3)
    x = L.modulate(bytes(range(40)), 7)
    quiet = np.zeros(len(x) // 2, complex)
    rec = _recording(np.concatenate([x, quiet, x]), rng)
    frames = L.decode(rec, 7)
    check = L.check_spectrum(rec, frames)
    assert len(frames) == 2
    assert 115e3 <= check.occupied_hz <= 135e3
    assert check.adjacent_dbc == {-1: None, 1: None}
