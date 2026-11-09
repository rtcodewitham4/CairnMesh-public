"""Link QA: acceptance rules and link-test/health state.

The functions here judge snapshots supplied by :class:`Gateway`; they do not
read a clock, send frames, or call back into gateway state.  The two books in
this module own their mutable tables and receive only a clock and an outbound
event sink.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable, Iterator, Mapping, Optional

from .frames import LinkQuality
from .latency import SF_STEP_DB, margin_db
from .session_book import CLOSED, OPEN

if TYPE_CHECKING:  # pragma: no cover
    from .session_book import Session

#: Acceptance rule (``Gateway.assess``), designed for 30 % interference (user
#: 2026-10-21): up to ``ACCEPT_SHORT_HOPS`` hops each hop needs ``(margin dB,
#: detours)`` = ``ACCEPT_SHORT``, longer paths ``ACCEPT_LONG``; a detour is a
#: third node linking both ends with ``ACCEPT_DETOUR_DB`` or more. Fitted on
#: 96 seeds × 2 targets × 2 layouts (dormant network, 30 % loss, 6 commands
#: each), then checked on fresh seeds: computed from the gateway's own
#: measurements, accepted nodes 0 unconfirmed of 1284 commands, rejected
#: ones 101 (``protocol_spec.md`` 13.3).
ACCEPT_SHORT_HOPS = 5
ACCEPT_SHORT = (4.0, 1)
ACCEPT_LONG = (6.0, 2)
ACCEPT_DETOUR_DB = 4.0

#: A link test's verdicts (``LinkTest.verdict``).
LINK_GOOD, LINK_WEAK_SIGNAL, LINK_LOSSY, LINK_DOWN = "good", "weak_signal", "lossy", "down"


@dataclass
class LinkTest:
    """Keepalive round trips on one target's session: how well it talks (``Gateway.test_link``).

    A keepalive is the one exchange that runs nothing on the device, so the
    test can repeat it freely. The path's per-hop margins come from the
    probe that opened it; with the loss they tell a link too weak for its
    SF (more margin needed: a relay, or a slower setting) from one that is
    strong enough but loses frames anyway (interference, collisions: a
    slower setting does not help, more time does).
    """

    target: int
    rounds: int
    started: float
    #: Wait after each round before the next.
    gap: float
    #: ``sf_margin`` when it started: the margin a hop is expected to keep.
    margin_needed: float
    #: The common setting's SF, which the probes' margins are measured against.
    base_sf: int = 7
    #: One per keepalive: ``(sent_at, round trip or None for a miss, SF)``.
    results: list[tuple[float, Optional[float], int]] = field(default_factory=list)
    pending: Optional[tuple[int, int]] = None
    sent_at: float = 0.0
    next_at: float = 0.0
    #: Last round finished or path opened: the test gives up after ``Gateway.test_patience`` without.
    progress_at: float = 0.0
    breaks: int = 0
    #: The path (gateway … target) and each hop's margin above the base SF's limit, at the latest round.
    path: tuple[int, ...] = ()
    margins: tuple[float, ...] = ()
    #: Different paths the rounds went over (a break re-probes).
    paths: int = 0
    #: The session's SF at the latest round.
    sf: int = 7
    expected: float = 0.0
    worst: float = 0.0
    finished: Optional[float] = None
    #: Why it stopped before ``rounds`` (empty when it ran them all).
    stopped: str = ""

    @property
    def done(self) -> int:
        return len(self.results)

    @property
    def answered(self) -> int:
        return sum(rtt is not None for _, rtt, _ in self.results)

    @property
    def loss(self) -> Optional[float]:
        return None if not self.results else 1.0 - self.answered / self.done

    def rtt(self, q: float) -> Optional[float]:
        """The ``q`` quantile (0–1) of the answered round trips."""

        times = sorted(rtt for _, rtt, _ in self.results if rtt is not None)
        if not times:
            return None
        return times[min(len(times) - 1, math.ceil(q * len(times)) - 1 if q > 0 else 0)]

    @property
    def weakest(self) -> Optional[tuple[int, int, float]]:
        """``(sender, receiver, margin)`` of the path's weakest hop."""

        if not self.margins:
            return None
        k = min(range(len(self.margins)), key=self.margins.__getitem__)
        return self.path[k], self.path[k + 1], self.margins[k]

    @property
    def session_margin(self) -> Optional[float]:
        """The weakest hop's margin at the SF the session runs at (each step down adds ``SF_STEP_DB``).

        The session already slows down until this is ``margin_needed``;
        it stays below only when ``max_sf`` capped it. Measured: judged on
        the SF7 margin instead, random 40 under 30 % SNR-blind loss came
        out "weak signal" 18 times of 24 (its links are thin at SF7, but
        the session had slowed down to cover that).
        """

        weakest = self.weakest
        return None if weakest is None else weakest[2] + SF_STEP_DB * (self.sf - self.base_sf)

    @property
    def verdict(self) -> str:
        if not self.answered:
            return LINK_DOWN
        margin = self.session_margin
        lossy = self.loss > 0.05 or self.breaks > 0
        if margin is not None and margin < self.margin_needed and lossy:
            return LINK_WEAK_SIGNAL
        if lossy:
            return LINK_LOSSY
        return LINK_GOOD


@dataclass(frozen=True)
class HopCheck:
    """One hop of a path under acceptance (``Gateway.assess``)."""

    sender: int
    receiver: int
    #: SNR above the SF7 limit, as the probe that opened the path measured it.
    margin_db: float
    #: Other nodes ``c`` the gateway knows to link both ends with ``ACCEPT_DETOUR_DB`` dB or more.
    detours: int
    ok: bool


@dataclass
class Acceptance:
    """Is a node deployed well enough that every command to it ends with a certain outcome?

    Designed for 30 % interference (user 2026-10-21): every hop has the margin
    or a detour; long paths need more of both. ``passed`` is the verdict;
    ``reasons`` and ``remedies`` say what failed and what to change on site.
    """

    target: int
    at: float
    passed: bool
    path: tuple[int, ...] = ()
    hops: tuple[HopCheck, ...] = ()
    test: Optional[LinkTest] = None
    reasons: tuple[str, ...] = ()
    remedies: tuple[str, ...] = ()
    #: Worth a look but not a failure (a link test that lost a round or two).
    warnings: tuple[str, ...] = ()
    #: Loaded from a saved book: measured where the gateway stood then.
    saved: bool = False


#: Share of frames the design expects lost to interference (user 2026-10-21).
DESIGN_INTERFERENCE = 0.30
#: The gateway's own hop: recent sends kept per node, how many before the
#: interference estimate counts, and its one-sided confidence (z). Measured:
#: 40 sends at 90 % flagged a fifth to a third of the commands under the
#: design's own 30 % loss.
HEALTH_WINDOW = 100
HEALTH_MIN_ATTEMPTS = 30
HEALTH_Z = 2.326  # 99 %
#: Opens in a row whose path fails acceptance, or probe rounds in a row that
#: fail, before they count (one alone is ordinary at 30 % loss).
HEALTH_REPEATS = 2


@dataclass
class Health:
    """A node in operation (layer 2): lasting signs that its deployment no longer holds.

    A maintenance notice, not a gate on commands. Measured (48 seeds, accepted
    nodes): under the design's 30 % loss no short-term signal tells the next
    command's fate (a pre-flight of 5 keepalives failed 53–66 % of commands
    that then ended certain), and a sudden change shows only once traffic
    meets it. What stays certain rests on acceptance (layer 1) and on asking
    until the node answers (layer 3).

    ``reasons`` maps a cause to what to tell the operator; empty means OK.
    Causes: ``path`` (``HEALTH_REPEATS`` opens in a row on a path that fails
    acceptance), ``unreachable`` (as many probe rounds in a row failed),
    ``interference`` (the gateway's own hop loses more than the design
    allows, with 99 % confidence).
    """

    target: int
    reasons: dict[str, str] = field(default_factory=dict)
    since: float = 0.0
    #: The gateway's own hop: one entry per send, ``True`` when it went through.
    attempts: deque = field(default_factory=lambda: deque(maxlen=HEALTH_WINDOW))
    #: Opens in a row on a failing path; probe rounds in a row that failed.
    bad_paths: int = 0
    failed_probes: int = 0

    @property
    def ok(self) -> bool:
        return not self.reasons

    def interference(self) -> Optional[tuple[float, float]]:
        """``(estimate, 99 % lower bound)`` of the per-frame loss on the gateway's hop.

        A send goes through when the frame and its acknowledgement both do:
        ``1 - (1 - p)²``; Wilson bound on the failure share, then inverted.
        """

        n = len(self.attempts)
        if n < HEALTH_MIN_ATTEMPTS:
            return None
        f = 1.0 - sum(self.attempts) / n
        z = HEALTH_Z
        low = (f + z * z / (2 * n) - z * math.sqrt(f * (1 - f) / n + z * z / (4 * n * n))) / (1 + z * z / n)
        loss = lambda share: 1.0 - math.sqrt(max(0.0, 1.0 - share))
        return loss(f), loss(max(0.0, low))


class HealthBook:
    """The gateway's per-node maintenance notices and its own-hop attempt window."""

    def __init__(self, clock: Callable[[], float], publish: Callable[[object], None]) -> None:
        self.clock = clock
        self._publish = publish
        self.health: dict[int, Health] = {}

    def _health(self, target: int) -> Health:
        health = self.health.get(target)
        if health is None:
            health = self.health[target] = Health(target, since=self.clock())
        return health

    def _warn(self, target: int, cause: str, text: str) -> None:
        health = self._health(target)
        if health.reasons.get(cause) == text:
            return
        was = health.ok
        health.reasons[cause] = text
        if was:
            health.since = self.clock()
        self._health_changed(health)

    def _clear(self, target: int, cause: str) -> None:
        health = self.health.get(target)
        if health is None or cause not in health.reasons:
            return
        del health.reasons[cause]
        if health.ok:
            health.since = self.clock()
        self._health_changed(health)

    def _health_changed(self, health: Health) -> None:
        from .events import HealthChanged

        self._publish(HealthChanged(node_id=health.target, ok=health.ok, reasons=tuple(health.reasons.values())))

    def _note_attempts(self, target: int, sends: int, through: bool) -> None:
        """The gateway's own hop: ``sends`` copies of one frame, the last one through or not."""

        health = self._health(target)
        health.attempts.extend([False] * (sends - 1) + [through])
        estimate = health.interference()
        if estimate is None:
            return
        loss, low = estimate
        if low > DESIGN_INTERFERENCE:
            self._warn(
                target, "interference",
                f"干扰超出设计：网关这一跳丢包约 {loss:.0%}（99 % 把握 > {DESIGN_INTERFERENCE:.0%}）",
            )
        elif loss <= DESIGN_INTERFERENCE:
            self._clear(target, "interference")


class LinkTestBook:
    """Running link tests and the tests that will end in an acceptance."""

    def __init__(
        self,
        clock: Callable[[], float],
        publish: Callable[[object], None],
        test_patience: float = 90.0,
    ) -> None:
        self.clock = clock
        self._publish = publish
        self.link_tests: dict[int, LinkTest] = {}
        self.test_patience = test_patience
        self._accepting: dict[int, LinkTest] = {}

    def _test_of(self, session: "Session") -> Optional[LinkTest]:
        test = self.link_tests.get(session.target)
        return test if test is not None and test.finished is None else None

    def _test_due(self, session: "Session", now: float) -> bool:
        test = self._test_of(session)
        return test is not None and test.pending is None and now >= test.next_at

    def _test_round(
        self,
        session: "Session",
        now: float,
        controller_id: int,
        floor: float,
        expected: float,
        margin: float,
    ) -> bool:
        """A keepalive went out: it is a round of the running test, if any."""

        test = self._test_of(session)
        if test is None or test.pending is not None:
            return False
        test.pending, test.sent_at = session.keepalive_id, now
        path = (controller_id, *session.route, session.target)
        if path != test.path:
            test.paths += 1
        test.path = path
        test.margins = tuple(margin_db(link.snr_db, floor) for link in session.links)
        test.sf = session.sf
        test.expected = expected
        test.worst = session.keepalive_deadline - now - margin
        return True

    def _test_result(
        self, session: "Session", now: float, answered: bool
    ) -> Optional[tuple[LinkTest, bool]]:
        test = self._test_of(session)
        if test is None or test.pending is None or test.pending != session.keepalive_id:
            return None
        test.results.append((test.sent_at, now - test.sent_at if answered else None, session.sf))
        test.pending = None
        test.progress_at = now
        test.next_at = now + test.gap
        if test.done >= test.rounds:
            return self.finish(test, now, "")
        return None

    def _test_tick(self, sessions: Mapping[int, "Session"], now: float) -> Iterator[tuple[LinkTest, bool]]:
        for target, test in self.link_tests.items():
            if test.finished is not None:
                continue
            session = sessions.get(target)
            if session is None or session.state == CLOSED:
                yield self.finish(test, now, "会话已关闭")
            elif now - test.progress_at > self.test_patience:
                yield self.finish(test, now, f"{self.test_patience:g} s 内没有完成一轮（路径打不开或一直断）")

    def finish(self, test: LinkTest, now: float, why: str) -> tuple[LinkTest, bool]:
        """Finish ``test``, publish its event, and say whether an acceptance follows."""

        from .events import LinkTestFinished

        test.finished, test.stopped, test.pending = now, why, None
        self._publish(LinkTestFinished(node_id=test.target, test=test))
        accepting = self._accepting.get(test.target) is test
        if accepting:
            del self._accepting[test.target]
        return test, accepting


class NotAccepted(RuntimeError):
    """A command to a node that has not passed acceptance (``Gateway.require_acceptance``)."""


@dataclass(frozen=True)
class PathView:
    """Read-only session snapshot for one acceptance decision.

    ``route`` excludes the gateway and target, and ``links`` is ordered outward
    from the gateway with one measured link per route hop.
    """

    target: int
    state: Optional[str]
    route: tuple[int, ...]
    links: tuple[LinkQuality, ...]


def assess(
    view: PathView,
    *,
    now: float,
    controller_id: int,
    floor: float,
    link_map: dict[tuple[int, int], float],
    has_neighbour_info: bool,
    test: Optional[LinkTest] = None,
) -> Acceptance:
    """Judge a read-only session snapshot by the acceptance rule (``ACCEPT_*``)."""

    target, state, route, links = view.target, view.state, view.route, view.links
    if state != OPEN or len(links) != len(route) + 1:
        why = "路径没打开" if test is None or not test.stopped else f"路径没打开（{test.stopped}）"
        return Acceptance(
            target, now, False, test=test, reasons=(why,),
            remedies=("先用 weak 看已测链路，在最弱处补中继，再重新验收",),
        )
    path = (controller_id, *route, target)
    margins = [margin_db(link.snr_db, floor) for link in links]
    known = dict(link_map)
    for (a, b), margin in zip(zip(path, path[1:]), margins):
        known[(min(a, b), max(a, b))] = margin  # the probe that opened the path is the freshest
    nodes = {node for pair in known for node in pair}

    def link(a: int, b: int) -> float:
        return known.get((min(a, b), max(a, b)), -math.inf)

    long = len(path) - 1 > ACCEPT_SHORT_HOPS
    need_db, need_detours = ACCEPT_LONG if long else ACCEPT_SHORT
    checks, reasons, remedies = [], [], []
    name = lambda node: "G" if node == controller_id else f"{node:02X}"
    for (a, b), margin in zip(zip(path, path[1:]), margins):
        detours = sum(
            1 for c in nodes
            if c not in (a, b) and link(a, c) >= ACCEPT_DETOUR_DB and link(c, b) >= ACCEPT_DETOUR_DB
        )
        ok = margin >= need_db or detours >= need_detours
        checks.append(HopCheck(a, b, margin, detours, ok))
        if not ok:
            reasons.append(f"{name(a)}→{name(b)} 余量 {margin:+.1f} dB、绕行 {detours} 条")
            remedies.append(
                f"在 {name(a)} 与 {name(b)} 之间补中继，放在到两端都测得 ≥ {ACCEPT_DETOUR_DB:g} dB 的位置"
                f"（中点不一定好：遮挡因地而异，补完先 scan 看读数）；或架高 / 挪近使这一跳 ≥ {need_db:g} dB"
                + (f"；这一跳要 {need_detours} 个这样的节点" if need_detours > 1 else "")
            )
    if long and reasons:
        remedies.append(
            f"路径 {len(path) - 1} 跳（> {ACCEPT_SHORT_HOPS} 跳）按更严的标准；把网关挪近、缩短路径也可以"
        )
    warnings = []
    if test is not None and test.verdict in (LINK_DOWN, LINK_WEAK_SIGNAL):
        reasons.append(f"通信质量测试 {test.answered}/{test.done}（{test.verdict}）")
        remedies.append("链路在当前速率下余量不够：按上面的位置补中继后重新验收")
    elif test is not None and test.verdict == LINK_LOSSY:
        # Measured: a round or a break lost in clean air did not predict
        # unconfirmed commands under 30 % loss (the per-hop rule does).
        warnings.append(f"通信质量测试 {test.answered}/{test.done}、断链 {test.breaks} 次：现场可能有干扰，留意")
    if reasons and not has_neighbour_info:
        remedies.append("节点簿里没有邻居信息：先 scan 再验收，绕行才算得全")
    return Acceptance(
        target, now, not reasons, path, tuple(checks), test, tuple(reasons), tuple(remedies), tuple(warnings),
    )
