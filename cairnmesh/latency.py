"""Theoretical timing of a routed path (``protocol_spec.md`` section 11).

``Estimate``, ``LatencyModel`` and ``SF_STEP_DB`` live here, moved unchanged
from ``gateway.py``. ``Gateway`` keeps the per-SF model
cache (``_model_for``), derives its timeouts from the model and re-exports
these names under their old ones.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

from .airtime import Airtime
from .frames import MAX_NEIGHBOURS
from .mesh import route_hop_timeout

#: Secure envelope on top of a plain frame: FCNT 4 + TX 1 + MIC 4.
SEAL_OVERHEAD = 9


@dataclass(frozen=True)
class Estimate:
    """Seconds for one exchange: no loss, and one repeat on every hop."""

    expected: float
    worst: float


class LatencyModel:
    """Theoretical timing of a routed path (``protocol_spec.md`` section 11).

    Mirrors the relays' rules: every relay forwards after a uniform jitter of
    up to ``jitter``; a lost hop costs one hop timeout (``mesh.route_hop_timeout``:
    three airtimes, or the answer's way back within three hops of the
    target); a lost last hop into the gateway costs one hop timeout like any
    other when the gateway echoes it (``last_hop_ack``), else the blind
    repeat's gap (one airtime plus jitter). ``turnaround`` is the
    receive-to-transmit time per hop (radio switching, processing, and in the
    simulator its tick).
    """

    def __init__(
        self,
        airtime: Airtime,
        *,
        jitter: float,
        slack: float,
        answer: float,
        turnaround: float = 0.005,
        target: float = 5.0,
        retries: int = 2,
        app: int = 0,
        last_hop_ack: bool = True,
    ) -> None:
        self.airtime = airtime
        #: ``MeshRelay.last_hop_ack``: the hop into the gateway is acknowledged by its echo.
        self.last_hop_ack = last_hop_ack
        #: Bytes the end-to-end layer adds to a REQUEST and its REPLY (task Q14).
        self.app = app
        #: Repeats a hop makes before it gives up (``MeshRelay.route_retries``).
        self.retries = retries
        self.jitter = jitter
        self.slack = slack
        self.answer = answer
        self.turnaround = turnaround
        #: The real-time goal (a control target, not a pass mark).
        self.target = target

    def lengths(self, hops: int) -> dict[str, int]:
        """Sealed sizes of the routed frames on a path of ``hops`` hops."""

        trailer = (hops - 1) + 1
        return {
            "request": 7 + trailer + SEAL_OVERHEAD + self.app,
            "wake": 5 + trailer + SEAL_OVERHEAD,
            "reply": 6 + trailer + SEAL_OVERHEAD + self.app,
            "keepalive": 7 + trailer + SEAL_OVERHEAD,
        }

    def hop_timeout(self, length: int, remaining: int, answered: bool) -> float:
        """The relays' own rule (``mesh.route_hop_timeout``), plus their repeat spread (one airtime)."""

        air = self.airtime(length)
        return route_hop_timeout(air, self.jitter, self.slack, self.answer, remaining, answered) + air

    def repeat_gap(self, length: int) -> float:
        """Longest gap before the blind repeat of a frame into the gateway."""

        return self.airtime(length) + self.jitter

    def one_way(self, hops: int, length: int) -> float:
        """No-loss time for one routed frame to cross ``hops`` hops."""

        return hops * (self.airtime(length) + self.turnaround) + (hops - 1) * self.jitter / 2

    def exchange(self, hops: int, out_length: int, back_length: int, loss: float = 0.0) -> Estimate:
        """``loss``: chance a frame is lost on one hop; the expected time then
        counts each hop's first repeat (a hop repeats when its frame or the
        acknowledgement is lost: 1 - (1 - loss)²). The worst case does not
        depend on it: every hop repeats once."""

        clean = self.one_way(hops, out_length) + self.one_way(hops, back_length)
        out = [self.hop_timeout(out_length, left, True) for left in range(1, hops + 1)]
        if self.last_hop_ack:
            back = [self.hop_timeout(back_length, 0, False)] * hops
            gap = 0.0
        else:
            back = [self.hop_timeout(back_length, 0, False)] * (hops - 1)
            gap = self.repeat_gap(back_length)
        repeat = 1.0 - (1.0 - loss) ** 2
        expected = clean + repeat * (sum(out) + sum(back)) + loss * gap
        return Estimate(expected, clean + sum(out) + sum(back) + gap)

    def command(self, hops: int, loss: float = 0.0) -> Estimate:
        sizes = self.lengths(hops)
        return self.exchange(hops, sizes["request"], sizes["reply"], loss)

    def keepalive(self, hops: int, loss: float = 0.0) -> Estimate:
        sizes = self.lengths(hops)
        return self.exchange(hops, sizes["keepalive"], sizes["reply"], loss)

    def detection(self, hops: int, interval: float, skips: Optional[int] = None) -> float:
        """Worst time from a path breaking to the gateway noticing (keepalive only).

        With ``skips`` (``Gateway.skip_reprobe``) also when a dead first relay
        is covered by the next one: that many keepalives answered past it,
        each up to one interval after the last answer.
        """

        one = interval + self.keepalive(hops).worst
        # ``worst`` allows each hop one repeat; the hop before a dead relay
        # makes all of its ``retries`` before reporting (A5: 3, measured late).
        sizes = self.lengths(hops)
        longest = max(self.hop_timeout(sizes["keepalive"], left, True) for left in range(1, hops + 1))
        one += max(0, self.retries - 1) * longest
        return one * max(1, skips or 0)

    def probe(self, hops: int, window: float, backoff: float, lead: float = 0.0) -> float:
        """Longest a probe of ``hops`` hops takes to come back answered.

        Every relay waits up to ``backoff`` before passing it on; the target
        collects for ``window``; the answer travels back routed. A wake
        probe (task D3) and its answer add ``lead`` seconds of long preamble
        to every hop, and each relay may wait out one neighbour's copy too.
        """

        out = sum(
            backoff + self.airtime(7 + 3 * k + SEAL_OVERHEAD) + self.turnaround + 2 * lead
            for k in range(hops)
        )
        back = self.one_way(hops, 5 + 2 * hops + hops + SEAL_OVERHEAD) + hops * lead
        return out + window + back + self.slack

    @staticmethod
    def answer_length(hops: int, neighbours: int = MAX_NEIGHBOURS) -> int:
        """Sealed size of a DISCOVER_REPLY from ``hops`` hops out."""

        return 6 + 2 * hops + 3 * neighbours + (hops - 1) + 1 + SEAL_OVERHEAD

    def answer_busy(self, hops: int) -> float:
        """How long one scan answer keeps the gateway's neighbourhood busy.

        Every answer funnels through the last hops into the gateway; within
        three hops of it no two of them can be on air together (the hidden
        node between them), and the hop into the gateway goes twice (the
        gateway's echo, or the blind repeat).
        """

        return (min(hops, 3) + 1) * self.airtime(self.answer_length(hops))

    def scan_spread(self, hops: Iterable[int], factor: float = 2.0) -> float:
        """Answer window for a scan of nodes ``hops`` hops out (task C).

        The answers alone keep the gateway's side busy for the sum of their
        ``answer_busy``; answers at random times need room around that:
        ``factor`` times it (tuned against measurement).
        """

        return factor * sum(self.answer_busy(h) for h in hops)

    def scan(self, max_hops: int, window: float, backoff: float, spread: float) -> float:
        """No-loss time from sending a scan to the last answer coming in.

        The scan spreads like a probe to the furthest node (``max_hops``),
        each node collects for ``window``, answers at the latest after
        ``spread``, and the answer crosses ``max_hops`` hops back.
        """

        out = sum(backoff + self.airtime(8 + 3 * k + SEAL_OVERHEAD) + self.turnaround for k in range(max_hops))
        back = self.one_way(max_hops, self.answer_length(max_hops)) + self.repeat_gap(self.answer_length(1))
        return out + window + spread + back + self.slack

    @staticmethod
    def scan_sends(hops: Iterable[int]) -> int:
        """Transmissions of one scan without loss: one DISCOVER per node and the
        gateway's, plus each answer's hops and the blind repeat into the gateway."""

        hops = list(hops)
        return len(hops) + 1 + sum(h + 1 for h in hops)

    def relay_share(self, hops: int, interval: float) -> float:
        """Share of time a relay on the path transmits for the keepalive alone."""

        sizes = self.lengths(hops)
        return (self.airtime(sizes["keepalive"]) + self.airtime(sizes["reply"])) / interval

    def channel_share(self, hops: int, interval: float) -> float:
        """Keepalive airtime summed over every hop, per interval (upper bound on channel use)."""

        return hops * self.relay_share(hops, interval)


#: Each SF step down buys this much SNR (SX126x demodulation limits; ``_choose_sf``).
SF_STEP_DB = 2.5


def margin_db(snr_db: float, floor: float) -> float:
    """链路余量：测得 SNR 减去链路下限；正数表示还有余量，负数表示低于下限。"""

    return snr_db - floor
