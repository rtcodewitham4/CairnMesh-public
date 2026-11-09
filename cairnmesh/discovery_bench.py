"""Node discovery benchmarks (task C4; slow, not unit tests).

    python -m cairnmesh.discovery_bench scan [seeds]     # one scan: coverage, time and sends vs the model
    python -m cairnmesh.discovery_bench storm [seeds]    # every node starts at once: JOIN storm, rules on / off
    python -m cairnmesh.discovery_bench impact [seeds]   # a session's commands while a scan runs

Networks use the link-budget model (``--disc``: the old disc), the line
and ladder planned with ``DEPLOYMENT_MARGIN_DB`` per 80 m link.
Layouts: a 10-hop line, a 2×8 ladder, and mesh_demo's random density
(60 nodes in 900 m, radio range 150, gateway in the middle) at 30, 60 and
100 nodes. Only the gateway's connected component counts: an island that
cannot reach the gateway never shows up, and its floods stay on its own.

``scan``: no JOINs; the gateway is told the layout's size
(``expected_nodes``). Per layout and loss rate: share of reachable nodes
found in the first round and in the end, full rounds and one-by-one asks,
the first round's last answer against the model (``LatencyModel.scan``),
and sends against the no-loss count (``LatencyModel.scan_sends``). Reachable
means a chain of links that carry at least half the frames; nodes that
answered over weaker links are counted apart ("额外").

``storm``: every node starts at t = 0 and JOINs (``join_spread`` 30 s), as
after an area-wide power cut; with the storm rules (default) and without
(``join_quiet``/``join_by_scan``/``join_storm`` off). Share known, time
until the gateway knows every reachable node, JOINs and sends.

``impact``: a session to a far node, a command every 2 s, with and without a
scan running: delivery and median time against the model.

Reference results were recorded during design.
"""

from __future__ import annotations

import math
import random
import statistics
import sys
from typing import Callable, Optional

from .airtime import airtime_for
from .mesh_sim import MeshNetwork, ladder, line, scatter
from .radio_model import LogDistance, bench_signal, planned_range, seeded

KEY = bytes(range(16))
GATEWAY = 0x42
STEP = 0.005
#: Link model for every network built here; ``None`` (``--disc``) is the disc.
SIGNAL: Optional[LogDistance] = LogDistance()


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def layout(name: str, seed: int) -> tuple[dict, dict]:
    # Planned under the link budget: each 80 m link has the deployment margin.
    planned = {} if SIGNAL is None else {"radio_range": planned_range(80.0, model=SIGNAL)}
    if name == "line10":
        return line(10), planned
    if name == "ladder2x8":
        return ladder(8), planned
    count = int(name.removeprefix("random"))
    size = 900.0 * math.sqrt(count / 60)
    options = {"controller_position": (size / 2, size / 2), "radio_range": 150.0}
    return scatter(count, size, random.Random(seed)), options


def build(name: str, seed: int, drop: float, gateway: dict, discovery: dict):
    positions, options = layout(name, seed)
    clock = _Clock()
    network = MeshNetwork(
        positions,
        key=KEY,
        clock=clock,
        airtime=airtime_for(7),
        drop=drop,
        gateway_options={"expected_nodes": len(positions), **gateway},
        discovery_options=discovery,
        seed=seed,
        record=True,
        signal=seeded(SIGNAL, seed),
        **options,
    )
    controller = network.controller(network.sessions, clock=clock, confirm_window=None)
    network.sessions.attach(controller)
    reachable = [node for node in positions if network.reachable(node)]
    return clock, network, controller, reachable


def run(clock, network, controller, seconds: float, until: Optional[Callable[[], bool]] = None) -> None:
    end = clock.now + seconds
    while clock.now < end and not (until is not None and until()):
        clock.now += STEP
        network.tick(clock.now)
        controller.tick(clock.now)


def sends(network, nodes, since: float = 0.0, until: float = math.inf) -> int:
    """Transmissions by ``nodes`` (and the gateway) between two times."""

    from .secure import transmitter

    members = set(nodes) | {GATEWAY}
    return sum(1 for at, _, raw in network.medium.log if since <= at < until and transmitter(raw) in members)


def _mean(values) -> float:
    values = list(values)
    return statistics.mean(values) if values else float("nan")


# ------------------------------------------------------------------------ scan

def scan_one(name: str, seed: int, drop: float) -> Optional[dict]:
    clock, network, controller, reach = build(name, seed, drop, {}, {"join": False})
    if not reach:
        return None
    gateway = network.sessions
    run(clock, network, controller, 0.5)
    start = clock.now
    scan = gateway.scan()
    first_end = scan.ends
    model = scan.expected
    run(clock, network, controller, 900.0, lambda: scan.done)
    first = [node for node, at in scan.answered.items() if at <= first_end and node in reach]
    last = max((scan.answered[node] for node in first), default=start)
    found = [node for node in scan.answered if node in reach]
    theory = gateway.model.scan_sends(gateway.nodes[node].hops for node in found)
    return {
        "reach": len(reach),
        "first": len(first) / len(reach),
        "found": len(found) / len(reach),
        # Answered over a link weaker than "half the frames get through"
        # (link budget): found, but not counted as reachable.
        "extra": sum(node not in reach for node in scan.answered),
        "rounds": scan.rounds,
        "asked": len(scan.ids) - scan.rounds,
        "routed": scan.routed,
        "last/model": (last - start) / model,
        "duration": scan.finished_at - start,
        "sends": sends(network, reach, start),
        "theory": theory,
        "spread": scan.spread,
    }


def scan_bench(seeds: int = 5) -> None:
    print(f"{'布局':<10}{'丢包':>5}{'可达':>6}{'首轮':>8}{'最终':>8}{'整轮':>6}{'补问':>6}{'定向':>6}{'末答/模型':>10}{'用时 s':>8}{'发送':>7}{'理论':>7}{'额外':>6}")
    for name in ("line10", "ladder2x8", "random30", "random60", "random100"):
        for drop in (0.0, 0.05):
            rows = [row for seed in range(1, seeds + 1) if (row := scan_one(name, seed, drop)) is not None]
            mean = lambda key: _mean(row[key] for row in rows)  # noqa: E731
            print(
                f"{name:<10}{drop:>5.0%}{mean('reach'):>6.0f}{mean('first'):>8.1%}{mean('found'):>8.1%}"
                f"{mean('rounds'):>6.1f}{mean('asked'):>6.1f}{mean('routed'):>6.1f}{mean('last/model'):>10.2f}{mean('duration'):>8.0f}"
                f"{mean('sends'):>7.0f}{mean('theory'):>7.0f}{mean('extra'):>6.1f}",
                flush=True,
            )


# ----------------------------------------------------------------------- storm

RULES_OFF = (
    {"join_storm": None},
    {"join_quiet": None, "join_by_scan": False},
)


def storm_one(name: str, seed: int, rules: bool, drop: float = 0.05, horizon: float = 600.0) -> Optional[dict]:
    gateway_options, discovery_options = ({}, {}) if rules else RULES_OFF
    clock, network, controller, reach = build(name, seed, drop, gateway_options, discovery_options)
    if not reach:
        return None
    gateway = network.sessions
    known_at = None
    while clock.now < horizon:
        clock.now += STEP
        network.tick(clock.now)
        controller.tick(clock.now)
        if known_at is None and all(node in gateway.nodes for node in reach):
            known_at = clock.now
            break
    joins = sum(network.agents[node].stats["joins"] for node in reach)
    return {
        "reach": len(reach),
        "known": sum(node in gateway.nodes for node in reach) / len(reach),
        "time": known_at,
        "joins": joins,
        "sends": sends(network, reach),
    }


def storm_bench(seeds: int = 3) -> None:
    print(f"{'布局':<10}{'规则':>5}{'可达':>6}{'已知':>8}{'全部已知 s':>12}{'JOIN':>7}{'发送':>8}")
    for name in ("line10", "random30", "random60", "random100"):
        for rules in (False, True):
            rows = [row for seed in range(1, seeds + 1) if (row := storm_one(name, seed, rules)) is not None]
            times = [row["time"] for row in rows if row["time"] is not None]
            done = f"{_mean(times):.0f}（{len(times)}/{len(rows)}）"
            mean = lambda key: _mean(row[key] for row in rows)  # noqa: E731
            print(
                f"{name:<10}{'开' if rules else '关':>5}{mean('reach'):>6.0f}{mean('known'):>8.1%}{done:>12}"
                f"{mean('joins'):>7.0f}{mean('sends'):>8.0f}",
                flush=True,
            )


# ---------------------------------------------------------------------- impact

def impact_one(seed: int, scanning: bool, drop: float = 0.05) -> Optional[dict]:
    clock, network, controller, reach = build("random60", seed, drop, {}, {"join": False})
    gateway = network.sessions
    far = sorted(reach, key=lambda node: network.medium.distance(GATEWAY, node))[-1:]
    if not far:
        return None
    target = far[0]
    session = gateway.open_session(target)
    run(clock, network, controller, 20.0, lambda: session.state == "open")
    if session.state != "open":
        return None
    run(clock, network, controller, 1.0)
    if scanning:
        gateway.scan()
    outcomes = []
    for _ in range(20):
        future = gateway.request(target, 3, 1)
        run(clock, network, controller, 2.0)
        outcomes.append(future)
    run(clock, network, controller, 25.0, lambda: all(f.done() for f in outcomes))
    results = [f.result() for f in outcomes if f.done()]
    ok = [r for r in results if r.ok]
    return {
        "delivered": len(ok) / len(outcomes),
        "median/model": statistics.median(r.elapsed for r in ok) / session.latency.expected if ok else float("nan"),
        "hops": session.hops,
    }


def impact_bench(seeds: int = 5) -> None:
    print(f"{'扫描':<6}{'送达':>8}{'中位数/期望':>12}{'跳数':>6}")
    for scanning in (False, True):
        rows = [row for seed in range(1, seeds + 1) if (row := impact_one(seed, scanning)) is not None]
        mean = lambda key: _mean(row[key] for row in rows)  # noqa: E731
        print(f"{'有' if scanning else '无':<6}{mean('delivered'):>8.1%}{mean('median/model'):>12.2f}{mean('hops'):>6.1f}", flush=True)


def main(argv: list[str]) -> None:
    global SIGNAL
    argv, SIGNAL = bench_signal(argv)
    command = argv[0] if argv else "scan"
    seeds = int(argv[1]) if len(argv) > 1 else None
    if command == "scan":
        scan_bench(seeds or 5)
    elif command == "storm":
        storm_bench(seeds or 3)
    elif command == "impact":
        impact_bench(seeds or 5)
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
