"""Link budget for the simulated medium (task B1; PC only).

``LogDistance`` replaces the old "everything inside ``radio_range`` hears at
an RSSI linear in distance" disc with:

* **log-distance path loss**: the median RSSI drops ``10·n·log10(d)``;
* **log-normal shadowing**, fixed per link and the same both ways (walls and
  hills do not move between frames), drawn from ``seed`` and the two ids so it
  does not depend on the order frames are sent in;
* **per-frame fading**, a fresh Gaussian (in dB) for every copy of a frame;
* a **per-SF demodulation limit**: a copy is received when its SNR reaches
  the limit of the spreading factor it was sent with, so SF12 reaches further
  than SF7;
* an optional **capture effect**: of two overlapping frames the one at least
  ``capture_db`` stronger survives.

``radio_range`` keeps its meaning: it is where the median link (no shadowing,
no fading) is exactly at the SF ``reference_sf`` limit, so layouts built for
the disc model (``line``, ``ladder``, ``scatter``) stay comparable.

All numbers are **estimates**, not measurements of this project's hardware:

* noise floor −174 dBm/Hz + 10·log10(125 kHz) + 6 dB noise figure ≈ −117 dBm;
* SNR limits −7.5 dB (SF7) … −20 dB (SF12) in 2.5 dB steps, i.e. sensitivity
  ≈ −124.5 … −137 dBm at 125 kHz (SX1262 datasheet quotes −137 dBm at SF12);
* path-loss exponent 2.7 and shadowing σ 6 dB sit inside the 2–4 / 3.5–8 dB
  ranges published for outdoor LoRa links (exact paper values not checked);
  fading σ 2 dB and capture 6 dB are common simulation choices. Replace them
  once links are measured.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, replace
from typing import Optional

#: Demodulation SNR limit (dB) per spreading factor at any bandwidth (estimate).
SNR_LIMIT_DB = {7: -7.5, 8: -10.0, 9: -12.5, 10: -15.0, 11: -17.5, 12: -20.0}

#: Thermal noise in 125 kHz plus a 6 dB receiver noise figure (estimate).
NOISE_FLOOR_DBM = -117.0

#: Median margin a planned deployment leaves on each link (with σ 6 dB
#: shadowing about 95 % of such links really have a positive margin).
DEPLOYMENT_MARGIN_DB = 10.0


def sensitivity_dbm(sf: int, noise_floor: float = NOISE_FLOOR_DBM) -> float:
    """Weakest RSSI a frame sent at ``sf`` can be received at."""

    if sf not in SNR_LIMIT_DB:
        raise ValueError(f"spreading factor must be 7..12, got {sf}")
    return noise_floor + SNR_LIMIT_DB[sf]


@dataclass(frozen=True)
class LogDistance:
    """Log-distance path loss with shadowing and fading (all estimates)."""

    exponent: float = 2.7
    shadowing_db: float = 6.0
    fading_db: float = 2.0
    reference_sf: int = 7
    noise_floor: float = NOISE_FLOOR_DBM
    capture_db: Optional[float] = 6.0
    seed: int = 0
    #: Strongest RSSI reported (close radios saturate rather than grow forever).
    max_rssi: float = -30.0

    def shadowing(self, a: int, b: int, *, moves: tuple[int, int] = (0, 0)) -> float:
        """The fixed shadowing of the link between ``a`` and ``b`` (symmetric).

        ``moves``: how often the lower and the higher id were moved; each new
        place draws new terrain.
        """

        if self.shadowing_db <= 0:
            return 0.0
        low, high = sorted((a, b))
        key = f"{self.seed}:{low}:{high}" + (f":{moves[0]}:{moves[1]}" if any(moves) else "")
        return random.Random(key).gauss(0.0, self.shadowing_db)

    def median_rssi(
        self, distance: float, radio_range: float, a: int, b: int, *, shadowing: Optional[float] = None
    ) -> float:
        """RSSI of the ``a``–``b`` link before per-frame fading.

        ``shadowing`` is ``self.shadowing(a, b)`` when the caller has it cached.
        """

        if shadowing is None:
            shadowing = self.shadowing(a, b)
        ratio = max(distance, radio_range * 1e-3) / radio_range
        rssi = sensitivity_dbm(self.reference_sf, self.noise_floor) - 10 * self.exponent * math.log10(ratio)
        return min(self.max_rssi, rssi + shadowing)

    def frame_rssi(self, median: float, rng: random.Random) -> float:
        """One received copy: the link's median plus this frame's fading."""

        fade = rng.gauss(0.0, self.fading_db) if self.fading_db > 0 else 0.0
        return min(self.max_rssi, median + fade)

    def received(self, rssi: float, sf: int) -> bool:
        """Is a copy at ``rssi`` above the ``sf`` demodulation limit?"""

        return rssi >= sensitivity_dbm(sf, self.noise_floor)

    def reach(self, sf: int) -> float:
        """How many ``radio_range`` the median link reaches at ``sf``."""

        gain = sensitivity_dbm(self.reference_sf, self.noise_floor) - sensitivity_dbm(sf, self.noise_floor)
        return 10 ** (gain / (10 * self.exponent))


def planned_range(
    spacing: float, margin_db: float = DEPLOYMENT_MARGIN_DB, model: Optional[LogDistance] = None
) -> float:
    """``radio_range`` at which links ``spacing`` apart have a median margin of ``margin_db``.

    Models a planned deployment: whoever places the nodes checks each link has
    that much to spare at the reference SF (shadowing still varies per link).
    """

    model = LogDistance() if model is None else model
    return spacing * 10 ** (margin_db / (10 * model.exponent))


def bench_signal(argv: list[str]) -> tuple[list[str], Optional[LogDistance]]:
    """Take ``--disc`` out of a bench's arguments: ``None`` (the old disc), else the link budget."""

    rest = [arg for arg in argv if arg != "--disc"]
    return rest, (None if len(rest) != len(argv) else LogDistance())


def seeded(signal: Optional[LogDistance], seed: Optional[int]) -> Optional[LogDistance]:
    """``signal`` with shadowing drawn for ``seed`` (each layout seed gets its own terrain)."""

    return None if signal is None else replace(signal, seed=seed or 0)
