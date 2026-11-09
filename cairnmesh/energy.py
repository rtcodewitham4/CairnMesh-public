"""Radio power states, preamble sampling and energy metering (task D1; PC only).

A dormant radio sleeps and wakes every ``Sniffer.period`` for a channel
activity detection (CAD) of ``cad_time``; it hears a frame only when one of
those looks falls inside the frame's preamble and the CAD detects it
(``p_detect``). Senders that must reach dormant radios use a preamble longer
than the period (``preamble_for``). A radio that is awake listens all the
time, as the protocol has assumed so far.

``EnergyMeter`` integrates the time a radio spends in each state and turns it
into charge with ``RadioCurrents``. Every current here is an **estimate that
has not been checked against the datasheet or a measurement**: Semtech's
SX126x overview quotes "RX under 5 mA" and "25 mA TX at +14 dBm"; module
datasheets quote about 2 µA sleep with the configuration kept. The MCU's own
draw is a separate parameter (``mcu_sleep`` / ``mcu_run``), also an estimate.
``regulator`` is the supply's own quiescent draw, always on (the user's
supply, 2026-10-19: three FR6 lithium cells in series behind an LDO; 1 µA
assumes a nanopower LDO, an ordinary one draws tens of µA).
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Optional


def symbol_time(sf: int, bandwidth: float = 125e3) -> float:
    """One LoRa symbol in seconds."""

    return (1 << sf) / bandwidth


@dataclass(frozen=True)
class RadioCurrents:
    """Supply current per state in amperes (estimates, see the module docstring)."""

    sleep: float = 2e-6
    rx: float = 5e-3
    tx: float = 25e-3
    cad: float = 5e-3
    mcu_sleep: float = 1e-6
    mcu_run: float = 1e-3
    regulator: float = 1e-6


@dataclass(frozen=True)
class Sniffer:
    """Preamble sampling of a dormant radio.

    ``phase`` places this radio's looks (``phase + k·period``); ``cad_time``
    is how long one look lasts (the CAD needs that much preamble to see);
    ``p_detect`` the chance a look inside a preamble notices it;
    ``p_false`` the chance a look on an idle channel triggers anyway, which
    costs ``false_rx`` seconds of receiving.
    """

    period: float = 1.0
    phase: float = 0.0
    cad_time: float = 2 * symbol_time(7)
    p_detect: float = 0.99
    p_false: float = 0.001
    false_rx: float = 0.02

    def looks_within(self, start: float, end: float) -> list[float]:
        """Times this radio starts a look that ends by ``end``, from ``start`` on."""

        last = end - self.cad_time
        if last < start:
            return []
        first = math.ceil((start - self.phase) / self.period)
        stop = math.floor((last - self.phase) / self.period)
        return [self.phase + k * self.period for k in range(first, stop + 1)]

    def catches(self, start: float, preamble: float, rng: random.Random) -> Optional[float]:
        """When this radio notices a preamble on air from ``start`` (``None``: it slept through).

        A look must fit inside the preamble; each such look detects it
        independently with ``p_detect``.
        """

        for look in self.looks_within(start, start + preamble):
            if rng.random() < self.p_detect:
                return look + self.cad_time
        return None


def preamble_for(sniffer: Sniffer, sf: int = 7, margin: float = 1.0) -> int:
    """Preamble symbols that span one sampling period plus one look (``margin`` × that).

    Any radio sampling with ``sniffer.period`` then has at least one look
    inside it, wherever its phase.
    """

    span = (sniffer.period + sniffer.cad_time) * margin
    return max(8, math.ceil(span / symbol_time(sf)))


@dataclass
class EnergyMeter:
    """Time a radio spent in each state, and the charge that cost."""

    currents: RadioCurrents = field(default_factory=RadioCurrents)
    sniffer: Optional[Sniffer] = None
    #: Seconds awake (receiver on), transmitting, dormant, and receiving
    #: while dormant (after a CAD noticed a preamble).
    awake: float = 0.0
    tx: float = 0.0
    dormant: float = 0.0
    dormant_rx: float = 0.0
    wakeups: int = 0
    _since: float = 0.0
    _is_dormant: bool = False

    def start(self, now: float, dormant: bool = False) -> None:
        self._since, self._is_dormant = now, dormant

    @property
    def is_dormant(self) -> bool:
        return self._is_dormant

    def set_dormant(self, dormant: bool, now: float) -> None:
        self._close(now)
        if self._is_dormant and not dormant:
            self.wakeups += 1
        self._is_dormant = dormant

    def add_tx(self, seconds: float) -> None:
        self.tx += seconds

    def add_dormant_rx(self, seconds: float) -> None:
        self.dormant_rx += seconds

    def _close(self, now: float) -> None:
        span = max(0.0, now - self._since)
        if self._is_dormant:
            self.dormant += span
        else:
            self.awake += span
        self._since = max(self._since, now)

    def charge(self, now: float) -> float:
        """Coulombs used up to ``now`` (radio and MCU)."""

        self._close(now)
        c = self.currents
        # Awake: receiver on except while sending; the MCU runs throughout.
        total = max(0.0, self.awake - self.tx) * c.rx + self.tx * c.tx + self.awake * c.mcu_run
        total += (self.awake + self.dormant) * c.regulator
        if self.dormant:
            cad = false = 0.0
            if self.sniffer is not None:
                looks = self.dormant / self.sniffer.period
                cad = looks * self.sniffer.cad_time
                false = looks * self.sniffer.p_false * self.sniffer.false_rx
            busy = cad + false + self.dormant_rx
            total += max(0.0, self.dormant - busy) * (c.sleep + c.mcu_sleep)
            total += cad * (c.cad + c.mcu_sleep) + (false + self.dormant_rx) * (c.rx + c.mcu_run)
        return total

    def average_current(self, now: float, since: float = 0.0) -> float:
        """Mean current in amperes over ``[since, now]`` (meter started at ``since``)."""

        span = now - since
        return self.charge(now) / span if span > 0 else 0.0


# ------------------------------------------------------------- theory (D6)


def dormant_current(sniffer: Sniffer, currents: RadioCurrents = RadioCurrents()) -> float:
    """Mean current of a radio that only samples the channel (no traffic)."""

    looks = 1.0 / sniffer.period
    cad = looks * sniffer.cad_time
    false = looks * sniffer.p_false * sniffer.false_rx
    rest = max(0.0, 1.0 - cad - false)
    return (
        rest * (currents.sleep + currents.mcu_sleep)
        + cad * (currents.cad + currents.mcu_sleep)
        + false * (currents.rx + currents.mcu_run)
        + currents.regulator
    )


def wake_charge(
    sniffer: Sniffer,
    lease: float,
    *,
    sf: int = 7,
    frame_air: float = 0.04,
    currents: RadioCurrents = RadioCurrents(),
) -> float:
    """Coulombs one node spends on one network-wide wake (estimate).

    It hears the wake preamble from where a look caught it (half a period on
    average) to the end of the frame, passes it on once with a preamble of
    one period, then stays awake for ``lease`` seconds.
    """

    preamble = preamble_for(sniffer, sf) * symbol_time(sf)
    receive = sniffer.period / 2 + frame_air
    send = preamble + frame_air
    return (
        receive * (currents.rx + currents.mcu_run)
        + send * (currents.tx + currents.mcu_run)
        + lease * (currents.rx + currents.mcu_run)
    )


def wake_hop_time(sniffer: Sniffer, *, sf: int = 7, frame_air: float = 0.04, backoff: float = 0.15) -> float:
    """Worst time for a wake to cross one hop: listen-before-talk backoff, preamble, frame."""

    return backoff + preamble_for(sniffer, sf) * symbol_time(sf) + frame_air
