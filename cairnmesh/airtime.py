"""LoRa time on air (Semtech AN1200.13 / SX1262 datasheet formula).

One formula serves the simulated medium, the relays' hop timeouts and the
gateway's latency model, so the model and the simulation can only disagree
about what they model (waiting, retries), never about airtime itself.

SF5 and SF6 use a different preamble rule on the SX1262 and are not covered.
"""

from __future__ import annotations

import math
from functools import partial
from typing import Callable

#: ``(length in bytes) -> seconds on air``.
Airtime = Callable[[int], float]


def lora_airtime(
    length: int,
    sf: int = 7,
    *,
    bandwidth: float = 125e3,
    coding_rate: int = 1,
    preamble: int = 8,
    crc: bool = True,
    explicit_header: bool = True,
    low_data_rate: "bool | None" = None,
) -> float:
    """Seconds a ``length``-byte LoRa payload occupies the channel.

    ``coding_rate`` 1..4 means 4/5..4/8. ``low_data_rate`` defaults to the
    SX1262 rule: on when a symbol lasts 16 ms or more (SF11/SF12 at 125 kHz).
    """

    if not 7 <= sf <= 12:
        raise ValueError(f"spreading factor must be 7..12, got {sf}")
    if not 1 <= coding_rate <= 4:
        raise ValueError(f"coding_rate must be 1..4 (4/5..4/8), got {coding_rate}")
    if length < 0:
        raise ValueError("length must not be negative")
    symbol = (1 << sf) / bandwidth
    if low_data_rate is None:
        low_data_rate = symbol >= 0.016
    bits = 8 * length - 4 * sf + 28 + 16 * crc - 20 * (not explicit_header)
    blocks = max(math.ceil(bits / (4 * (sf - 2 * low_data_rate))), 0)
    symbols = preamble + 4.25 + 8 + blocks * (coding_rate + 4)
    return symbols * symbol


def airtime_for(sf: int = 7, **options) -> Airtime:
    """``lora_airtime`` with everything but the length fixed."""

    return partial(lora_airtime, sf=sf, **options)
