"""Pure scan state, scheduling predicates and window arithmetic.

The ``Gateway`` still owns scan side effects: message numbering, frame
transmission, session recall and final publication.  This module contains
only the pieces whose results depend on their arguments and scan-local state.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from typing import Iterable, Optional, Sequence

from .frames import DISCOVER_SPREAD_UNIT, MAX_RELAYS
from .latency import LatencyModel


@dataclass
class Scan:
    """One DISCOVER scan: everyone first, then each known node that stayed silent."""

    started: float
    #: When the first round's answers should all be in (the latency model).
    ends: float
    spread: float
    #: What the model says the whole scan takes without loss (first round).
    expected: float
    #: Nodes known when the scan started.
    known: frozenset[int]
    #: ``(sequence, epoch)`` of every DISCOVER of this scan.
    ids: set = field(default_factory=set)
    answered: dict[int, float] = field(default_factory=dict)
    #: Nodes the answers name as neighbours.
    mentioned: set = field(default_factory=set)
    followups: deque = field(default_factory=deque)
    #: The node a second-round DISCOVER is out to, and until when to wait for it.
    asking: Optional[int] = None
    asking_until: float = 0.0
    finished_at: Optional[float] = None
    #: Rounds to everyone so far.
    rounds: int = 1
    #: Past the rounds to everyone: asking silent nodes one by one.
    closing: bool = False
    #: Task D5: the network was asleep and is being woken; the first round
    #: goes out then, sized for this many nodes not known yet.
    starts_at: Optional[float] = None
    unknown: int = 0
    #: Task Q12: nodes that were on a session's channel when the asking one
    #: by one began (alive, silent), and until when that waits for the
    #: sessions to come back to the common setting.
    away: frozenset = frozenset()
    recall_until: Optional[float] = None
    #: The one-by-one ask out now is a routed PROBE along a known path (if it
    #: stays unanswered, a DISCOVER flood to that node follows).
    asking_routed: bool = False
    #: ``(sequence, epoch)`` → node of every routed ask; how many went out.
    probes: dict = field(default_factory=dict)
    routed: int = 0

    @property
    def done(self) -> bool:
        return self.finished_at is not None

    def note_answer(self, node: int, now: float, mentioned: Iterable[int]) -> None:
        """Record one answer; the first answer from a node fixes its arrival time."""

        self.answered.setdefault(node, now)
        self.mentioned.update(mentioned)


@dataclass
class JoinStorm:
    """JOINs inside a sliding window; ``note`` reports whether their count is enough."""

    threshold: Optional[int]
    window: float
    joins: deque[float] = field(default_factory=deque)

    def note(self, now: float) -> bool:
        """Record a JOIN and report whether the window holds ``threshold`` JOINs."""

        if self.threshold is None:
            return False
        self.joins.append(now)
        while self.joins and self.joins[0] < now - self.window:
            self.joins.popleft()
        return len(self.joins) >= self.threshold

    def clear(self) -> None:
        self.joins.clear()


@dataclass(frozen=True)
class RoundPlan:
    """The arithmetic of one DISCOVER round to everyone."""

    spread: float
    units: int
    expected: float
    spreading: float


def round_plan(
    known_depths: Sequence[int],
    unknown: int,
    model: LatencyModel,
    probe_window: float,
    probe_backoff: float,
    factor: float,
    margin: float,
) -> RoundPlan:
    """Size a DISCOVER round exactly as the pre-split gateway did."""

    depths = list(known_depths) + [4] * max(unknown, 1 - len(known_depths))
    hops = max(known_depths, default=MAX_RELAYS + 1)
    spread = model.scan_spread(depths, factor=factor)
    units = max(1, min(0xFF, math.ceil(spread / DISCOVER_SPREAD_UNIT)))
    spread = units * DISCOVER_SPREAD_UNIT
    expected = model.scan(hops, probe_window, probe_backoff, spread)
    spreading = model.scan(hops, probe_window, probe_backoff, 0.0) + margin
    return RoundPlan(spread, units, expected, spreading)


def start_due(scan: Optional[Scan], enabled: bool, has_controller: bool) -> bool:
    """Whether the first tick should start a scan."""

    return scan is None and enabled and has_controller


def rescan_due(
    scan: Optional[Scan],
    node_count: int,
    now: float,
    has_sessions: bool,
    expected_nodes: int,
    rescan_after: Optional[float],
    rescans_done: int,
    rescans: int,
) -> bool:
    """Whether an incomplete finished scan is due for a follow-up scan."""

    return (
        scan is not None
        and scan.done
        and rescan_after is not None
        and node_count < expected_nodes
        and rescans_done < rescans
        and now >= scan.finished_at + rescan_after
        and not has_sessions
    )


def interval_due(
    scan: Optional[Scan],
    now: float,
    has_sessions: bool,
    scan_interval: Optional[float],
) -> bool:
    """Whether the periodic scan after a finished scan is due."""

    return (
        scan is not None
        and scan.done
        and scan_interval is not None
        and now >= scan.finished_at + scan_interval
        and not has_sessions
    )


def missing_nodes(scan: Scan, away: frozenset[int] | set[int]) -> list[int]:
    """Known or mentioned nodes that have neither answered nor are away."""

    return sorted((scan.known | scan.mentioned) - scan.answered.keys() - away)
