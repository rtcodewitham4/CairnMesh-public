"""An RTL-SDR as an independent observer of the bench's air.

The board's radios report what they think they sent; this module checks it
from outside: it records the channel with ``rtl_sdr`` and decodes every LoRa
packet in the recording (preamble, sync word, header, payload, CRC), times
it on the air, measures its frequency, and looks at its spectrum. It knows
the SX126x's PHY only through what the bench's own frames showed on
2026-10-26 (the conventions below decode them to the bytes sent, header
checksum and CRC included).

    .venv/bin/python firmware/tools/lora_sdr.py listen 0 5 --sf 7     # record channel 0 for 5 s, decode
    .venv/bin/python firmware/tools/lora_sdr.py capture 433500000 5 cap.u8
    .venv/bin/python firmware/tools/lora_sdr.py decode cap.u8 --sf 7 --spectrum

A recording is the ``rtl_sdr`` file (unsigned 8-bit I, Q) plus a ``.json``
beside it with the SDR's tuning. The channel sits ``OFFSET`` above the SDR's
centre, away from the DC spike, with both 250 kHz neighbours clear of it too.
Needs numpy (``pip install -e .[sdr]``) and the ``rtl-sdr`` tools.

The SDR's own frequency error (about +1 ppm on the bench's dongle) is
measured against the bench radios, whose crystals a spectrum analyzer
showed to be accurate: ``ppm_from`` on channel 0 frames, then
``Frame.corrected_hz`` for the others.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np

FS = 2_000_000
BW = 125_000
OFFSET = 375_000
SYNC_WORD = 0x12  # register 0x1424 (sx126x.DefaultSyncWord): symbols 8 and 16 after the preamble
FULL_SCALE = 127.5

# The channel table of cmd/gateway/main.go (spec 15.3), provisional there too.
CHANNEL0_HZ = 433_500_000
SESSION_BASE_HZ = 444_000_000
SESSION_STEP_HZ = 250_000
LAST_CHANNEL = 31


def channel_hz(channel: int) -> int:
    if channel == 0:
        return CHANNEL0_HZ
    if 1 <= channel <= LAST_CHANNEL:
        return SESSION_BASE_HZ + (channel - 1) * SESSION_STEP_HZ
    raise ValueError(f"no channel {channel}")


def low_data_rate(sf: int, bw: int = BW) -> bool:
    """The SX1262 rule of sx126x.go / airtime.py: on when a symbol lasts 16 ms or more."""

    return (1 << sf) * 1000 >= 16 * bw


# ---------------------------------------------------------------- recording


def available() -> Optional[str]:
    """Why the SDR cannot be used here, or None."""

    if shutil.which("rtl_sdr") is None:
        return "no rtl_sdr (apt install rtl-sdr)"
    try:
        out = subprocess.run(["rtl_eeprom"], capture_output=True, timeout=10)
    except subprocess.TimeoutExpired:
        return "rtl_eeprom hung"
    err = out.stderr.decode(errors="replace")  # it prints the EEPROM's raw bytes too
    if "Found 0 device" in err or "No supported devices" in err:
        return "no RTL-SDR plugged in"
    return None


@dataclass
class Recording:
    iq: np.ndarray
    tuned_hz: float  # the SDR's centre
    fs: float = FS
    offset: float = OFFSET  # where the channel sits, relative to the centre

    @property
    def channel_hz(self) -> float:
        return self.tuned_hz + self.offset


def load(path: Path) -> Recording:
    path = Path(path)
    meta = json.loads(path.with_suffix(".json").read_text())
    raw = np.fromfile(path, np.uint8).astype(np.float32) - FULL_SCALE
    iq = (raw[0::2] + 1j * raw[1::2]).astype(np.complex64)
    return Recording(iq, meta["tuned_hz"], meta["fs"], meta["offset"])


class Capture:
    """``rtl_sdr`` recording ``channel_hz`` for ``seconds`` into ``path``, in the background.

    ``start()`` returns once samples flow, so whatever the caller does next
    is on the recording; ``wait()`` returns the ``Recording``.
    """

    def __init__(self, channel_hz: float, seconds: float, path: Path, *, gain: float = 30.0,
                 offset: float = OFFSET, fs: int = FS) -> None:
        self.path = Path(path)
        self.seconds = seconds
        self.tuned_hz = int(channel_hz - offset)
        self.offset = channel_hz - self.tuned_hz
        self.fs = fs
        self.gain = gain
        self.proc: Optional[subprocess.Popen] = None
        self.log: list[str] = []

    def start(self) -> "Capture":
        self.path.with_suffix(".json").write_text(json.dumps(
            {"tuned_hz": self.tuned_hz, "fs": self.fs, "offset": self.offset, "gain": self.gain,
             "started": time.strftime("%Y-%m-%dT%H:%M:%S")}))
        self.proc = subprocess.Popen(
            ["rtl_sdr", "-f", str(self.tuned_hz), "-s", str(self.fs), "-g", str(self.gain),
             "-n", str(int(self.fs * self.seconds)), str(self.path)],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        end = time.monotonic() + 10
        assert self.proc.stderr is not None
        while time.monotonic() < end:
            line = self.proc.stderr.readline()
            if not line:
                break
            self.log.append(line.rstrip())
            if line.startswith("Reading samples"):
                return self
        self.proc.kill()
        raise RuntimeError("rtl_sdr did not start:\n" + "\n".join(self.log))

    def wait(self) -> Recording:
        assert self.proc is not None
        _, err = self.proc.communicate(timeout=self.seconds + 15)
        self.log += err.splitlines()
        if self.proc.returncode not in (0, None):
            raise RuntimeError("rtl_sdr failed:\n" + "\n".join(self.log))
        return load(self.path)

    def __enter__(self) -> "Capture":
        return self.start()

    def __exit__(self, *exc) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait()


# ---------------------------------------------------------------- the PHY's coding


def whitening(n: int) -> list[int]:
    """The payload's whitening bytes (LFSR x^8+x^6+x^5+x^4+1 from 0xFF); the CRC is not whitened."""

    r, out = 0xFF, []
    for _ in range(n):
        out.append(r)
        r = ((r << 1) | (((r >> 7) ^ (r >> 5) ^ (r >> 4) ^ (r >> 3)) & 1)) & 0xFF
    return out


def payload_crc(payload: bytes) -> int:
    """CRC-16 (0x1021, from 0) of all bytes but the last two, then XOR with those two."""

    c = 0
    for b in payload[:-2]:
        c ^= b << 8
        for _ in range(8):
            c = ((c << 1) ^ 0x1021) & 0xFFFF if c & 0x8000 else (c << 1) & 0xFFFF
    if len(payload) >= 2:
        c ^= payload[-1] ^ (payload[-2] << 8)
    elif payload:
        c ^= payload[-1]
    return c


def header_nibbles(length: int, cr: int, has_crc: bool) -> list[int]:
    """The explicit header's five nibbles: length (high, low), CR and CRC flag, 5-bit checksum."""

    n0, n1, n2 = length >> 4, length & 0xF, (cr << 1) | int(has_crc)
    a = [(n0 >> i) & 1 for i in range(4)]
    b = [(n1 >> i) & 1 for i in range(4)]
    c = [(n2 >> i) & 1 for i in range(4)]
    c4 = a[3] ^ a[2] ^ a[1] ^ a[0]
    c3 = a[3] ^ b[3] ^ b[2] ^ b[1] ^ c[0]
    c2 = a[2] ^ b[3] ^ b[0] ^ c[3] ^ c[1]
    c1 = a[1] ^ b[2] ^ b[0] ^ c[2] ^ c[1] ^ c[0]
    c0 = a[0] ^ b[1] ^ c[3] ^ c[2] ^ c[1] ^ c[0]
    return [n0, n1, n2, c4, c3 << 3 | c2 << 2 | c1 << 1 | c0]


def _codeword(nibble: int, cr: int) -> list[int]:
    """Hamming code of a nibble, bits in air order: data bits 0..3, then parity (cr 1..4 = 4/5..4/8)."""

    d = [(nibble >> i) & 1 for i in range(4)]
    if cr == 1:
        return d + [d[0] ^ d[1] ^ d[2] ^ d[3]]
    p = [d[0] ^ d[1] ^ d[2], d[1] ^ d[2] ^ d[3], d[0] ^ d[1] ^ d[3], d[0] ^ d[2] ^ d[3]]
    return d + p[:cr]


_CODEBOOK = {cr: [_codeword(n, cr) for n in range(16)] for cr in (1, 2, 3, 4)}


def _nibble(bits: list[int], cr: int) -> tuple[int, bool]:
    """The nibble of a received codeword and whether it was a valid one (4/7, 4/8 correct one error)."""

    book = _CODEBOOK[cr]
    dist = [sum(x != y for x, y in zip(bits, cw)) for cw in book]
    best = min(dist)
    if best == 0:
        return dist.index(0), True
    if cr >= 3 and best == 1 and dist.count(1) == 1:
        return dist.index(1), False
    return bits[0] | bits[1] << 1 | bits[2] << 2 | bits[3] << 3, False


def _gray(s: int) -> int:
    return s ^ (s >> 1)


def _ungray(g: int) -> int:
    s = 0
    while g:
        s ^= g
        g >>= 1
    return s


def _deinterleave(values: list[int], sf_app: int, cw_len: int) -> list[list[int]]:
    """``cw_len`` symbols of ``sf_app`` bits → ``sf_app`` codewords of ``cw_len`` bits."""

    cws = [[0] * cw_len for _ in range(sf_app)]
    for i, g in enumerate(values):
        for j in range(sf_app):
            cws[(i - j - 1) % sf_app][i] = (g >> (sf_app - 1 - j)) & 1
    return cws


def _interleave(cws: list[list[int]], sf_app: int, cw_len: int) -> list[int]:
    out = []
    for i in range(cw_len):
        g = 0
        for j in range(sf_app):
            g |= cws[(i - j - 1) % sf_app][i] << (sf_app - 1 - j)
        out.append(g)
    return out


def _to_symbol(g: int, sf: int, reduced: bool) -> int:
    s = _ungray(g)
    return ((s << 2 if reduced else s) + 1) % (1 << sf)


def _from_symbol(k: int, sf: int, reduced: bool) -> int:
    s = (k - 1) % (1 << sf)
    return _gray((s + 2) // 4 % (1 << (sf - 2)) if reduced else s)


def encode(payload: bytes, sf: int, cr: int = 1, *, ldro: Optional[bool] = None) -> list[int]:
    """The data symbols (after the SFD) of an explicit-header packet with CRC."""

    if ldro is None:
        ldro = low_data_rate(sf)
    w = whitening(len(payload))
    body = []
    for i, b in enumerate(payload):
        x = b ^ w[i]
        body += [x & 0xF, x >> 4]
    crc = payload_crc(payload)
    body += [crc & 0xF, (crc >> 4) & 0xF, (crc >> 8) & 0xF, (crc >> 12) & 0xF]
    nibbles = header_nibbles(len(payload), cr, True) + body
    symbols = []
    first = sf - 2
    head, nibbles = nibbles[:first], nibbles[first:]
    head += [0] * (first - len(head))
    symbols += [_to_symbol(g, sf, True) for g in _interleave([_codeword(n, 4) for n in head], first, 8)]
    sf_app = sf - 2 if ldro else sf
    while nibbles:
        block, nibbles = nibbles[:sf_app], nibbles[sf_app:]
        block += [0] * (sf_app - len(block))
        cws = [_codeword(n, cr) for n in block]
        symbols += [_to_symbol(g, sf, ldro) for g in _interleave(cws, sf_app, 4 + cr)]
    return symbols


@dataclass
class Decoded:
    length: int
    cr: int
    has_crc: bool
    header_ok: bool
    payload: Optional[bytes] = None
    crc_ok: Optional[bool] = None
    fec_errors: int = 0  # codewords that were not valid ones (corrected where the code can)
    symbols_used: int = 0


def symbols_needed(length: int, sf: int, cr: int, has_crc: bool, ldro: bool) -> int:
    nibbles = 2 * length + 4 * has_crc - (sf - 7)
    sf_app = sf - 2 if ldro else sf
    blocks = max(0, -(-nibbles // sf_app))
    return 8 + blocks * (4 + cr)


def decode_symbols(symbols: list[int], sf: int, *, ldro: Optional[bool] = None) -> Decoded:
    if ldro is None:
        ldro = low_data_rate(sf)
    first = sf - 2
    if len(symbols) < 8:
        return Decoded(0, 0, False, False)
    fec = 0
    nibbles = []
    for cw in _deinterleave([_from_symbol(k, sf, True) for k in symbols[:8]], first, 8):
        n, ok = _nibble(cw, 4)
        nibbles.append(n)
        fec += not ok
    n0, n1, n2, c4, c30 = nibbles[:5]
    length, cr, has_crc = n0 << 4 | n1, n2 >> 1, bool(n2 & 1)
    header_ok = header_nibbles(length, cr, has_crc)[3:] == [c4, c30] and 1 <= cr <= 4
    out = Decoded(length, cr, has_crc, header_ok, fec_errors=fec, symbols_used=8)
    if not header_ok:
        return out
    need = symbols_needed(length, sf, cr, has_crc, ldro)
    if len(symbols) < need:
        return out
    sf_app = sf - 2 if ldro else sf
    nibbles = nibbles[5:]
    for i in range(8, need, 4 + cr):
        for cw in _deinterleave([_from_symbol(k, sf, ldro) for k in symbols[i:i + 4 + cr]], sf_app, 4 + cr):
            n, ok = _nibble(cw, cr)
            nibbles.append(n)
            fec += not ok
    w = whitening(length)
    payload = bytes((nibbles[2 * i] | nibbles[2 * i + 1] << 4) ^ w[i] for i in range(length))
    out.payload, out.fec_errors, out.symbols_used = payload, fec, need
    if has_crc:
        c = nibbles[2 * length:2 * length + 4]
        out.crc_ok = (c[0] | c[1] << 4 | c[2] << 8 | c[3] << 12) == payload_crc(payload)
    return out


# ---------------------------------------------------------------- waveforms


def _upchirp(N: int) -> np.ndarray:
    c = np.arange(N)
    return np.exp(2j * np.pi * (c * c / (2 * N) - c / 2))


def modulate(payload: bytes, sf: int, *, cr: int = 1, preamble: int = 8, os: int = FS // BW,
             cfo_bins: float = 0.0, sync: int = SYNC_WORD) -> np.ndarray:
    """A packet at ``os`` samples per chip, centred at 0 Hz (for the tests: the inverse of ``decode``)."""

    N = 1 << sf
    t = np.arange(N * os) / os

    def chirp(k: int, down: bool = False, part: float = 1.0) -> np.ndarray:
        tt = t[: int(N * os * part)]
        ph = tt * tt / (2 * N) + (k / N - 0.5) * tt
        ph -= np.where(tt >= N - k, tt - (N - k), 0)  # the frequency wraps from +BW/2 to -BW/2
        return np.exp(2j * np.pi * (-ph if down else ph))

    parts = [chirp(0)] * preamble + [chirp((sync >> 4) * 8), chirp((sync & 0xF) * 8)]
    parts += [chirp(0, True), chirp(0, True), chirp(0, True, 0.25)]
    parts += [chirp(k) for k in encode(payload, sf, cr)]
    x = np.concatenate(parts)
    return x * np.exp(2j * np.pi * cfo_bins / N * np.arange(len(x)) / os)


# ---------------------------------------------------------------- demodulation


@dataclass
class Frame:
    """One packet on the air. Times are seconds from the recording's first sample."""

    start: float  # the carrier's half-power rise
    end: float  # and its fall
    sf: int
    preamble: int  # up-chirps counted before the sync word
    sync: int  # the sync word read from its two symbols
    freq_hz: float  # where the SDR saw it (its own error included)
    level_dbfs: float
    snr_db: float
    decoded: Decoded
    symbols: list[int] = field(default_factory=list, repr=False)

    @property
    def airtime(self) -> float:
        return self.end - self.start

    @property
    def payload(self) -> Optional[bytes]:
        return self.decoded.payload

    @property
    def ok(self) -> bool:
        d = self.decoded
        return d.header_ok and d.payload is not None and d.crc_ok is not False and self.sync == SYNC_WORD

    def corrected_hz(self, sdr_ppm: float) -> float:
        return self.freq_hz / (1 + sdr_ppm * 1e-6)


def _lowpass(x: np.ndarray, fs: float, cutoff: float = 80e3, taps: int = 129) -> np.ndarray:
    n = np.arange(taps) - (taps - 1) / 2
    h = np.sinc(2 * cutoff / fs * n) * np.hamming(taps)
    h /= h.sum()
    size = len(x) + taps - 1
    nfft = 1 << (size - 1).bit_length()
    y = np.fft.ifft(np.fft.fft(x, nfft) * np.fft.fft(h, nfft))[:size]
    return y[(taps - 1) // 2:(taps - 1) // 2 + len(x)]


def _centred(x: float, N: int) -> float:
    return (x + N / 2) % N - N / 2


def bursts(rec: Recording, *, block: int = 200, rise: float = 4.0, min_seconds: float = 0.004) -> list[tuple[int, int]]:
    """Sample ranges where the channel's power stands ``rise`` times above its quiet level (coarse).

    The power is the channel's (mixed down, averaged over a chip), not the
    SDR's whole band: 12 dB more to detect with, a weak frame stays in one
    piece (2026-10-26: one at SF12 came out as 60 bits of 4 ms).
    """

    os = int(round(rec.fs / BW))
    block = max(1, block // os) * os
    n = len(rec.iq) // block * block
    t = np.arange(n)
    chips = (rec.iq[:n] * np.exp(-2j * np.pi * rec.offset / rec.fs * t)).reshape(-1, os).mean(1)
    e = (np.abs(chips) ** 2).reshape(-1, block // os).mean(1)
    on = e > np.percentile(e, 10) * rise
    d = np.diff(np.r_[0, on.astype(np.int8), 0])
    starts, ends = np.flatnonzero(d == 1) * block, np.flatnonzero(d == -1) * block
    spans: list[list[int]] = []
    for a, b in zip(starts, ends):
        if spans and a - spans[-1][1] < 0.0003 * rec.fs:  # a dip, not a gap between frames
            spans[-1][1] = b
        else:
            spans.append([a, b])
    return [(a, b) for a, b in spans if b - a >= min_seconds * rec.fs and a > 0 and b < n]


def _demodulate(rec: Recording, a: int, b: int, sf: int, ldro: Optional[bool]) -> Optional[Frame]:
    fs, N = rec.fs, 1 << sf
    os = int(round(fs / BW))
    margin = int(0.002 * fs)
    lo, hi = max(0, a - margin), min(len(rec.iq), b + margin)
    z = _lowpass(rec.iq[lo:hi] * np.exp(-2j * np.pi * rec.offset / fs * np.arange(lo, hi)), fs)
    # 0.2 ms: the noise of a weak frame no longer dips below half power, and
    # a centred average still crosses half power at the edge.
    env = np.convolve(np.abs(z) ** 2, np.ones(400) / 400, "same")
    inner = env[(a - lo) + margin:(b - lo) - margin]
    level = float(np.median(inner if len(inner) else env))
    # The carrier: the run above half its power around the burst's middle
    # (the margins may hold a neighbour's frame).
    below = np.flatnonzero(env <= level / 2)
    mid = (a + b) // 2 - lo
    before, after = below[below < mid], below[below > mid]
    s0 = int(before[-1]) + 1 if len(before) else 0
    s1 = int(after[0]) - 1 if len(after) else len(env) - 1
    U = _upchirp(N)

    def window(start: int, f: float) -> np.ndarray:
        idx = start + np.arange(N) * os
        if idx[0] < 0 or idx[-1] >= len(z):
            return np.zeros(N, complex)
        return z[idx] * np.exp(-2j * np.pi * f * (idx - s0) / os / N) * np.conj(U)

    def peak(v: np.ndarray) -> tuple[float, float]:
        F = np.abs(np.fft.fft(v)) ** 2
        k = int(np.argmax(F))
        p, q, r = F[k - 1], F[k], F[(k + 1) % N]
        den = p - 2 * q + r
        return k + (0.5 * (p - r) / den if den else 0.0), float(q)

    # Coarse: windows every symbol from half a symbol after the rise; the
    # first that hears a down-chirp better than an up-chirp is in the SFD.
    grid = s0 + N // 2 * os
    rows = []
    for i in range((s1 - grid) // (N * os)):
        w = window(grid + i * N * os, 0)
        rows.append((*peak(w), peak(w * U * U)[1]))
    first_down = next((i for i, r in enumerate(rows) if r[2] > r[1]), None)
    if first_down is None or first_down < 4:
        return None
    k_up = float(np.median([r[0] for r in rows[: first_down - 3]]))
    best = max(range(-4, 13), key=lambda q: peak(window(grid + first_down * N * os + q * N * os // 8, 0) * U * U)[1])
    st = grid + first_down * N * os + best * N * os // 8
    k_down = peak(window(st, 0) * U * U)[0]
    # An up-chirp's bin is f + d (d: how late the window is), a down-chirp's
    # f - d; the half-symbol ambiguity goes to the d that puts a symbol edge
    # near the rise (the SX1262's carrier rises ~0.1 ms before it).
    s = k_up + k_down + ((st - grid) // os) % N
    f = min((_centred(s / 2, N), _centred(s / 2 + N / 2, N)), key=lambda f: abs(_centred(k_up - f - N / 2, N)))
    edge = grid - int(round(((k_up - f) % N) * os))
    # Fine: CFO's fraction from the phase between preamble symbols, the edge
    # to 1/os chip from the sharpest preamble peak, then the SFD's residual.
    X = [np.fft.fft(window(edge + (i + 1) * N * os, 0)) for i in range(4)]
    k = int(np.argmax(np.abs(X[0])))
    frac = float(np.angle(sum(X[i + 1][k] * np.conj(X[i][k]) for i in range(3)))) / (2 * np.pi)
    f = frac + round(f - frac)
    edge = max(range(edge - os, edge + os + 1),
               key=lambda e: sum(abs(np.sum(window(e + (i + 1) * N * os, f))) ** 2 for i in range(3)))
    lowrate = low_data_rate(sf) if ldro is None else ldro

    def symbol(start: int, f: float) -> int:
        return int(np.argmax(np.abs(np.fft.fft(window(start, f)))))

    def attempt(f: float, edge: int) -> tuple[float, list[int], list[int], Decoded]:
        n = 0
        while edge + (n + 1) * N * os < len(z):
            w = window(edge + n * N * os, f)
            if np.abs(np.fft.fft(w * U * U)).max() > np.abs(np.fft.fft(w)).max():
                break
            n += 1
        pre = [symbol(edge + i * N * os, f) for i in range(n)]
        data0 = edge + int(round((n + 2.25) * N * os))
        symbols: list[int] = []

        def read(count: int) -> None:
            while len(symbols) < count and data0 + (len(symbols) + 1) * N * os <= len(z):
                symbols.append(symbol(data0 + len(symbols) * N * os, f))

        # The header says how many symbols follow; without one, read to the fall.
        read(8)
        decoded = decode_symbols(symbols, sf, ldro=ldro)
        if decoded.header_ok:
            read(symbols_needed(decoded.length, sf, decoded.cr, decoded.has_crc, lowrate))
        else:
            read(max(8, int((s1 - data0) / (N * os) + 0.5)))
        return f, pre, symbols, decode_symbols(symbols, sf, ldro=ldro)

    # The SFD's residual, then: up-chirps cannot tell f one bin up from the
    # edge one chip late (the down-chirps can: their bin is 2 × the error).
    n_sfd = len(attempt(f, edge)[1])
    kd = _centred(int(np.argmax(np.abs(np.fft.fft(window(edge + n_sfd * N * os, f) * U * U)))), N)
    if kd:
        f += kd / 2
        edge += int(round(kd / 2 * os))
    # Should a noisy SFD still leave f and the edge apart, every data symbol
    # moves by 2 bins per step: the header checksum and CRC pick the step.
    tried = [attempt(f + m, edge - m * os) for m in (0, -1, 1, -2, 2)]
    good = [t for t in tried if t[3].header_ok and t[3].crc_ok is not False and t[3].payload is not None
            and all(abs(_centred(k, N)) <= 1 for k in t[1][:-2])]
    f, pre, symbols, decoded = good[0] if good else tried[0]
    n = len(pre)
    sync = (round(_centred(pre[-2], N) / 8) & 0xF) << 4 | (round(_centred(pre[-1], N) / 8) & 0xF) if n >= 2 else -1
    # SNR in the channel: the frame's power against the quiet just around it.
    quiet = np.r_[env[: max(0, s0 - 200)], env[s1 + 200:]]
    noise = float(np.median(quiet)) if len(quiet) > 100 else float("nan")
    snr = 10 * np.log10(max(level - noise, 1e-12) / noise) if noise > 0 else float("nan")
    return Frame(
        start=(lo + s0) / fs,
        end=(lo + s1) / fs,
        sf=sf,
        preamble=n - 2,
        sync=sync,
        freq_hz=rec.channel_hz + f * BW / N,
        level_dbfs=10 * np.log10(level / FULL_SCALE ** 2),
        snr_db=float(snr),
        decoded=decoded,
        symbols=symbols,
    )


def decode(rec: Recording, sf: int, *, ldro: Optional[bool] = None) -> list[Frame]:
    """Every packet at ``sf`` in the recording, in time order (bursts that are no packet are left out)."""

    frames = []
    for a, b in bursts(rec, min_seconds=12 * (1 << sf) / BW):
        frame = _demodulate(rec, a, b, sf, ldro)
        if frame is not None:
            frames.append(frame)
    return frames


def ppm_from(frames: list[Frame], nominal_hz: float) -> float:
    """The SDR's frequency error measured on frames from radios known to be on ``nominal_hz``."""

    return float(np.median([(f.freq_hz - nominal_hz) / nominal_hz * 1e6 for f in frames]))


# ---------------------------------------------------------------- spectrum


def spectrum(rec: Recording, start: float, end: float, nfft: int = 4096) -> tuple[np.ndarray, np.ndarray]:
    """Welch power spectrum (linear, per bin) between two times; frequencies relative to the channel."""

    x = rec.iq[int(start * rec.fs):int(end * rec.fs)]
    x = x - x.mean()
    win = np.hanning(nfft)
    hops = range(0, len(x) - nfft, nfft // 2)
    psd = np.mean([np.abs(np.fft.fft(x[i:i + nfft] * win)) ** 2 for i in hops], axis=0)
    freqs = np.fft.fftfreq(nfft, 1 / rec.fs) - rec.offset
    order = np.argsort(freqs)
    return freqs[order], psd[order]


def band(freqs: np.ndarray, psd: np.ndarray, centre: float, width: float = BW) -> float:
    m = np.abs(freqs - centre) <= width / 2
    return float(psd[m].sum())


def occupied_bandwidth(freqs: np.ndarray, psd: np.ndarray, noise: np.ndarray, fraction: float = 0.99,
                       span: float = 4 * BW) -> float:
    """Width holding ``fraction`` of the power above the noise within ±span/2 of the channel."""

    m = np.abs(freqs) <= span / 2
    f, p = freqs[m], np.clip(psd[m] - noise[m], 0, None)
    c = np.cumsum(p) / p.sum()
    tail = (1 - fraction) / 2
    return float(f[np.searchsorted(c, 1 - tail)] - f[np.searchsorted(c, tail)])


@dataclass
class SpectrumCheck:
    occupied_hz: float
    # ±250 kHz neighbours: their power above the noise, relative to the
    # channel; None when it does not stand 1 dB above the SDR's own noise,
    # which then bounds it (adjacent_floor_dbc).
    adjacent_dbc: dict[int, Optional[float]]
    adjacent_floor_dbc: dict[int, float]
    centre_hz: float  # power-weighted centre, relative to the channel


def check_spectrum(rec: Recording, frames: list[Frame], spacing: float = SESSION_STEP_HZ) -> SpectrumCheck:
    """Occupied bandwidth and the ±1 neighbours' power during ``frames``, against the quiet between them."""

    on = [spectrum(rec, f.start + 0.002, f.end - 0.002) for f in frames if f.airtime > 0.01]
    freqs = on[0][0]
    psd = np.mean([p for _, p in on], axis=0)
    quiet_spans = [(a.end + 0.005, b.start - 0.005) for a, b in zip(frames, frames[1:]) if b.start - a.end > 0.02]
    noise = np.mean([spectrum(rec, s, e)[1] for s, e in quiet_spans], axis=0) if quiet_spans else np.zeros_like(psd)
    inband = band(freqs, psd, 0) - band(freqs, noise, 0)
    adj, floor = {}, {}
    for side in (-1, 1):
        p = band(freqs, psd, side * spacing) - band(freqs, noise, side * spacing)
        nf = band(freqs, noise, side * spacing)
        adj[side] = round(10 * np.log10(p / inband), 1) if p > 0.26 * nf else None
        floor[side] = round(10 * np.log10(nf / inband), 1)
    m = np.abs(freqs) <= BW
    w = np.clip(psd[m] - noise[m], 0, None)
    return SpectrumCheck(
        occupied_hz=occupied_bandwidth(freqs, psd, noise),
        adjacent_dbc=adj,
        adjacent_floor_dbc=floor,
        centre_hz=float((freqs[m] * w).sum() / w.sum()),
    )


# ---------------------------------------------------------------- command line


def _print(frames: list[Frame]) -> None:
    for f in frames:
        d = f.decoded
        body = d.payload.hex() if d.payload is not None else "-"
        print(f"F t={f.start * 1000:9.2f}ms air={f.airtime * 1000:8.2f}ms sf={f.sf} pre={f.preamble} "
              f"sync=0x{f.sync:02x} freq={f.freq_hz:.0f} snr={f.snr_db:.1f}dB level={f.level_dbfs:.1f}dBFS "
              f"len={d.length} cr=4/{4 + d.cr} hdr={'ok' if d.header_ok else 'BAD'} "
              f"crc={'ok' if d.crc_ok else 'BAD' if d.crc_ok is False else '-'} fec={d.fec_errors} {body}")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("capture", help="record a channel frequency")
    c.add_argument("freq_hz", type=float)
    c.add_argument("seconds", type=float)
    c.add_argument("out", type=Path)
    c.add_argument("--gain", type=float, default=30.0)
    d = sub.add_parser("decode", help="decode a recording")
    d.add_argument("path", type=Path)
    d.add_argument("--sf", type=int, action="append")
    d.add_argument("--spectrum", action="store_true")
    li = sub.add_parser("listen", help="record a bench channel and decode it")
    li.add_argument("channel", type=int)
    li.add_argument("seconds", type=float)
    li.add_argument("--sf", type=int, action="append")
    li.add_argument("--gain", type=float, default=30.0)
    li.add_argument("--out", type=Path, default=Path("/tmp/lora_sdr.u8"))
    a = p.parse_args()
    if a.cmd == "capture":
        Capture(a.freq_hz, a.seconds, a.out, gain=a.gain).start().wait()
        return 0
    if a.cmd == "listen":
        rec = Capture(channel_hz(a.channel), a.seconds, a.out, gain=a.gain).start().wait()
    else:
        rec = load(a.path)
    frames = sorted((f for sf in (a.sf or [7]) for f in decode(rec, sf)), key=lambda f: f.start)
    _print(frames)
    if getattr(a, "spectrum", False) and frames:
        print("S", check_spectrum(rec, frames))
    return 0


if __name__ == "__main__":
    sys.exit(main())
