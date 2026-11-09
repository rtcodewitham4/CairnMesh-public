"""The controller's confirmation rules and the records of hearing nodes (layer 3).

``ConfirmPolicy`` holds only confirmation configuration and pure calculations:
it has no queue, lock, transport, clock, or event bus. ``HeardBook`` is the
single owner of the node and area "last heard" records used by those rules.

The controller keeps execution: queue changes, query ids, snapshots, events,
and retries stay there. In particular, this module must not import
``threading``, ``transport``, or ``events``.
"""

from __future__ import annotations

import math
from typing import Optional

#: Default time, from a request's first send, during which a request whose
#: retries got no answer is followed by status queries (Q18), before it goes
#: CONFIRMING (layer 3, spec 4.2).
DEFAULT_CONFIRM_WINDOW = 90.0

#: Layer 3 (Q21, 2026-10-21): after ``confirm_window`` a request still
#: without an answer is CONFIRMING and asked again in rounds: every
#: ``(fast interval, until age, slow interval)`` = 2 min for its first
#: 30 min, then every 30 min; at once (no sooner than ``CONFIRM_NUDGE_GAP``
#: after the last round) when its node is heard. In a silent network every
#: round wakes the path, so denser rounds cost battery.
DEFAULT_CONFIRM_SCHEDULE = (120.0, 1800.0, 1800.0)
CONFIRM_NUDGE_GAP = 10.0

#: From this age on, a round that would have to wake the whole sleeping
#: network waits the slow interval (2026-10-21, ``tests/experiments/quiet_rounds.py``,
#: 20 seeds × 2 layouts, dormant): target powered off 10–60 min, charge per
#: node 0.55 → 0.39 / 0.48 → 0.33 mAh in 3 h, outcome after power-up on
#: average 11.2 → 15.0 / 16.4 → 10.9 min (longest ≈ 28 min either way);
#: relays powered off: 2.2 → 6.8 min average, 17 → 28 min longest; 30 %
#: loss: unchanged (from 0 instead: up to 128 min, rounds failing by bad
#: luck then waited 30 min each).
DEFAULT_CONFIRM_QUIET_AFTER = 600.0

#: A node heard this recently is there: a round that got no answer is
#: followed after the fast interval, whatever its age (Q22: the round a JOIN
#: after an area power cut set off often drowned in the other nodes' JOINs,
#: and the slow interval then cost 30 min).
CONFIRM_HEARD_FRESH = 600.0

#: How long an ordinary (not ``important``) command is confirmed, from its first
#: send (the default is to be set from the
#: measurements in spec 4.2): a **hard** point on the wall clock, whatever the
#: quiet rounds say. Then the command ends ``UNCONFIRMED`` (it may have run; it
#: is never sent again by the controller) and the queue is free. ``important``
#: commands ignore it. ``None`` switches the limit off.
DEFAULT_CONFIRM_LIMIT = 1800.0

#: Asking stops at this share of the devices' record keep period: their
#: record may be gone after it, and a query would then wrongly say NOT_RUN.
#: 5 % covers the device clock's error.
RECORD_KEEP_MARGIN = 0.95


class ConfirmPolicy:
    """Configuration and pure decisions for status queries and confirmation."""

    def __init__(
        self,
        *,
        window: Optional[float],
        forever: bool,
        schedule: tuple[float, float, float],
        record_keep: Optional[float],
        quiet_after: Optional[float],
        limit: Optional[float],
    ) -> None:
        if limit is not None and window is not None and limit < window:
            raise ValueError(f"confirm_limit {limit:g} s is shorter than confirm_window {window:g} s")
        self.window = window
        self.forever = forever
        self.schedule = schedule
        self.record_keep = record_keep
        #: From this age on, a scheduled round that would have to wake the
        #: whole sleeping network (``round_wakes``, set by the gateway) waits
        #: the slow interval; hearing the node (``nudge``) still asks at once.
        #: ``None``: the schedule alone decides.
        self.quiet_after = quiet_after
        self.limit = limit

    def limit_at(
        self,
        first_sent: float,
        *,
        important: bool,
        confirming: bool,
    ) -> Optional[float]:
        """When an ordinary confirming command ends (a hard clock point), else ``None``."""

        if important or self.limit is None or not confirming:
            return None
        return first_sent + self.limit

    def limit_in(self, limit: Optional[float], now: float) -> Optional[float]:
        return None if limit is None else max(0.0, limit - now)

    def in_query_window(self, first_sent: float, now: float) -> bool:
        return self.window is not None and now < first_sent + self.window

    def enters_confirming(self, is_request: bool) -> bool:
        return self.forever and self.window is not None and is_request

    def keep_horizon(self) -> Optional[float]:
        """The age from which a device record may be gone, or ``None`` if records are kept forever."""

        return None if self.record_keep is None else self.record_keep * RECORD_KEEP_MARGIN

    def past_keep(self, age: float) -> bool:
        horizon = self.keep_horizon()
        return horizon is not None and age >= horizon

    def keep_cap(self, first_sent: float) -> Optional[float]:
        """The absolute end of asking, in the original ``first_sent + horizon`` form."""

        horizon = self.keep_horizon()
        return None if horizon is None else first_sent + horizon

    def next_round_due(
        self,
        *,
        first_sent: float,
        last_round: float,
        now: float,
        fresh: bool,
        quiet: bool,
        limit: Optional[float],
    ) -> float:
        """The next round's time; ``quiet`` is already the result of the caller's short-circuit hook."""

        fast, until, slow = self.schedule
        age = now - first_sent
        due = last_round + (fast if fresh or (age < until and not quiet) else slow)
        keep_cap = self.keep_cap(first_sent)  # never past the end of asking
        if keep_cap is not None:
            due = min(due, keep_cap)
        if limit is not None:
            # ``tick`` enforces the limit itself; this cap lets a scheduler sleep
            # exactly until the nearest deadline without missing it.
            due = min(due, limit)
        return due

    def slow_interval(self) -> float:
        return self.schedule[2]

    def nudged(self, deadline: float, now: float, last_round: float) -> float:
        return min(deadline, max(now, last_round + CONFIRM_NUDGE_GAP))

    def resume_due(self, now: float, last_round: float) -> float:
        return max(now, last_round + CONFIRM_NUDGE_GAP)


class HeardBook:
    """The only writer and reader of the node and area last-heard records."""

    def __init__(self) -> None:
        self._node: dict[int, float] = {}
        self._area = -math.inf

    def note(self, node: int, now: float) -> None:
        self._node[node] = now

    def note_area(self, now: float) -> None:
        self._area = now

    def last_heard(self, node: int, since: float) -> Optional[float]:
        """The node's last heard time, ignoring records from before the command was sent."""

        heard = self._node.get(node)
        return heard if heard is not None and heard >= since else None

    def heard_at(self, node: int, since: float) -> float:
        """The later of the node's and the area's last heard time (``-inf`` if neither)."""

        heard = self.last_heard(node, since)
        return max(heard if heard is not None else -math.inf, self._area)

    def fresh(self, node: int, since: float, now: float) -> bool:
        return now - self.heard_at(node, since) < CONFIRM_HEARD_FRESH
