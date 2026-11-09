"""Routed-session benchmarks (slow; not unit tests).

    python -m cairnmesh.session_bench sweep [seeds]     # robustness over seeds (≈ 2 min for 40)
    python -m cairnmesh.session_bench measure           # model vs simulation, acceptance style (≈ 10 min)
    python -m cairnmesh.session_bench trace             # cost of the trace policies (≈ 5 min)
    python -m cairnmesh.session_bench a5 [seeds] [flood] [relay.x=…] [gw.x=…]   # A5 tuning table (≈ 3 min)

``sweep``, per seed: a session on a 2×5 ladder survives a relay switched
off mid-path (request delivered once, session reopened around it; with
``--signal`` often ``dead_skipped`` instead: the next relay overhears and
takes over, the route is left as it was); the keepalive notices a dead first
relay within ``LatencyModel.detection`` (or ``keepalive_skipped``: nothing
breaks, a request still gets through); on a
5-hop line at 5 % loss, 20 requests are all delivered, executed at most
once, and the 19th fastest stays within the model's worst case.

``measure``: 17-hop line and mesh_demo's random layouts (60 nodes in 900 m,
100 in 700 m, gateway in the middle, the 3 farthest reachable nodes), no
loss and 5 % loss: delivery, simulated / theoretical latency (theory at the
actual loss rate), share over the worst case, transmissions within 2h + 4,
duplicates, breaks, hops, session set-up time.

``trace``: 5- and 17-hop lines at 5 % loss, a command every ~1 s, with
auto trace off / policy A (faults only, the default) / also after repeats.

``a5`` (the tuning table): planned 17-hop
line (target at its end), unplanned random 40 (550 m square, gateway in the
middle) and planned 5 × 8 grid (``wake_bench`` layouts), 0 and 5 % loss,
``seeds`` seeds, 3 farthest targets × 10 commands each on an always-awake
network: delivered, breaks, set-up time, latency median / p95, sends per
command. ``flood``: the same commands with no session (plain floods).
``relay.name=value`` / ``gw.name=value`` set ``MeshRelay`` / ``Gateway``
options (Python literals), e.g. ``gw.session_sf=False``.

Reference results were recorded on 2026-10-18.
Networks use the link-budget model (``radio_model.LogDistance``, task B1),
and the line and ladder are planned with a ``DEPLOYMENT_MARGIN_DB`` median
margin per 80 m link; ``--disc`` (anywhere in the arguments) runs the old
disc model on the old geometry instead.
"""

from __future__ import annotations

import random
import statistics
import sys
from collections import Counter
from typing import Callable, Optional

from . import simpool
from .airtime import airtime_for
from .mesh_sim import MeshNetwork, scatter
from .radio_model import LogDistance, bench_signal, planned_range, seeded

KEY = bytes(range(16))
GATEWAY = 0x42
STEP = 0.005
LINE = {node: (80.0 * node, 0.0) for node in range(1, 6)}
LINE17 = {node: (80.0 * node, 0.0) for node in range(1, 18)}
LADDER = {**LINE, **{10 + node: (80.0 * node, 50.0) for node in range(1, 6)}}
SIZES = {60: 900.0, 100: 700.0}
#: Link model for every network built here; ``None`` (``--disc``) is the disc.
SIGNAL: Optional[LogDistance] = LogDistance()
#: ``MeshRelay`` options for every network (``--widest``: the old probe metric).
RELAY: dict = {}
#: Hand-built layouts, planned under the link budget (``planned_range``).
PLANNED = (LINE, LINE17, LADDER)


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def build(positions, *, gateway_options: Optional[dict] = None, confirm_window: Optional[float] = None, **options):
    """``confirm_window=None``: no status query (Q18), so baselines measure each command's own retry window."""

    clock = Clock()
    if SIGNAL is not None and any(positions is layout for layout in PLANNED):
        options.setdefault("radio_range", planned_range(80.0, model=SIGNAL))
    network = MeshNetwork(
        positions,
        key=KEY,
        clock=clock,
        airtime=airtime_for(7),
        gateway_options={} if gateway_options is None else gateway_options,
        signal=seeded(SIGNAL, options.get("seed")),
        relay_options={**RELAY, **options.pop("relay_options", {})},
        **options,
    )
    controller = network.controller(
        network.sessions, clock=clock, timeout=8.0, max_retries=2, confirm_window=confirm_window
    )
    network.sessions.attach(controller)
    return clock, network, controller


def run(clock, network, controller, seconds: float, until: Optional[Callable[[], bool]] = None) -> None:
    end = clock.now + seconds
    while clock.now < end and not (until is not None and until()):
        clock.now += STEP
        network.tick(clock.now)
        controller.tick(clock.now)


def opened(clock, network, controller, target: int, limit: float = 20.0):
    session = network.sessions.open_session(target)
    start = clock.now
    run(clock, network, controller, limit, lambda: session.state == "open")
    taken = clock.now - start
    run(clock, network, controller, 0.5)
    return session, taken


def record_executions(network, target: int) -> list[int]:
    """Message ids the device actually executed (to catch duplicates)."""

    executed: list[int] = []
    device = network.devices[target]
    original = device.perform_action

    def perform(frame, original=original):
        executed.append(frame.message_id)
        return original(frame)

    device.perform_action = perform
    return executed


# --------------------------------------------------------------------- sweep


def sweep(seeds: int = 40) -> None:
    result = Counter()
    for seed in range(1, seeds + 1):
        clock, network, controller = build(LADDER, seed=seed)
        session, _ = opened(clock, network, controller, 5)
        if session.state != "open":
            result["open_fail"] += 1
            continue
        dead = session.route[len(session.route) // 2]
        network.set_power(dead, False)
        future = network.sessions.request(5, 3, 1)
        run(clock, network, controller, 30.0, future.done)
        ok = future.result(timeout=0).ok and network.devices[5].handled_count == 1
        run(clock, network, controller, 20.0, lambda: session.state == "open")
        good = ok and session.state == "open" and dead not in session.route
        # A later relay overheard the frame and took over (hop skip): it got
        # through, but the session still names the dead relay.
        skipped = ok and session.state == "open" and dead in session.route
        result["dead_ok" if good else "dead_skipped" if skipped else "dead_fail"] += 1
        if not (good or skipped):
            print("dead relay fail, seed", seed, future.result(timeout=0), network.sessions.history[-5:])

        clock, network, controller = build(LADDER, seed=seed, gateway_options={"idle_timeout": None})
        session, _ = opened(clock, network, controller, 5)
        if session.state == "open":
            gateway = network.sessions
            bound = gateway.model.detection(session.hops, gateway.keepalive_interval, gateway.skip_reprobe)
            off = clock.now
            network.set_power(session.route[0], False)
            run(clock, network, controller, 30.0, lambda: session.state != "open")
            broken = [t for t, event, _ in network.sessions.history if event.startswith("broken")]
            if broken and broken[0] - off <= bound + 0.1:
                result["keepalive_ok"] += 1
            elif not broken and session.state == "open":
                future = network.sessions.request(5, 3, 1)
                run(clock, network, controller, 30.0, future.done)
                # Nothing broke because the relay after it hears the gateway.
                result["keepalive_skipped" if future.result(timeout=0).ok else "keepalive_fail"] += 1
            else:
                result["keepalive_fail"] += 1

        clock, network, controller = build(LINE, seed=seed, drop=0.05)
        session, _ = opened(clock, network, controller, 5)
        if session.state != "open":
            result["line_open_fail"] += 1
            continue
        executed = record_executions(network, 5)
        times = []
        for value in range(20):
            future = network.sessions.request(5, 3, value)
            run(clock, network, controller, 30.0, future.done)
            outcome = future.result(timeout=0)
            times.append(outcome.elapsed if outcome.ok else float("inf"))
            run(clock, network, controller, 0.3)
        # The model of the SF the session ran at (task Q11: it may have slowed down).
        worst = network.sessions._model_for(session.sf).command(session.hops).worst
        delivered = sum(t != float("inf") for t in times)
        result["requests"] += 20
        result["delivered"] += delivered
        result["once" if len(executed) == len(set(executed)) else "DUPLICATE_EXECUTION"] += 1
        result["p95_ok" if sorted(times)[18] <= worst else "p95_over"] += 1
    print(dict(result))


# ------------------------------------------------------------------- measure


def measure_one(name, positions, targets_of, drop, seeds, requests=20, **options) -> None:
    rows = []
    for seed in seeds:
        clock, network, controller = build(
            positions(seed), seed=seed, drop=drop, gateway_options={"idle_timeout": None}, **options
        )
        for target in targets_of(network):
            executed = record_executions(network, target)
            session, taken = opened(clock, network, controller, target, 90.0)
            if session.state != "open":
                rows.append(dict(open=False))
                continue
            estimate = network.sessions.model.command(session.hops, drop)
            for value in range(requests):
                before = network.medium.stats["transmissions"]
                future = network.sessions.request(target, 3, value)
                run(clock, network, controller, 30.0, future.done)
                outcome = future.result(timeout=0)
                rows.append(
                    dict(
                        open=True,
                        hops=session.hops,
                        ok=outcome.ok,
                        rtt=outcome.elapsed,
                        expected=estimate.expected,
                        worst=estimate.worst,
                        tx=network.medium.stats["transmissions"] - before,
                        opened=taken,
                    )
                )
                run(clock, network, controller, 0.3)
            rows[-1]["dup"] = len(executed) != len(set(executed))
            rows[-1]["broken"] = session.counts["broken"]
            network.sessions.close_session(target)
    ok = [r for r in rows if r.get("ok")]
    total = [r for r in rows if r.get("open")]
    ratio = sorted(r["rtt"] / r["expected"] for r in ok)
    print(
        f"{name} drop {drop}: not opened {sum(not r['open'] for r in rows)} | delivered {len(ok)}/{len(total)}"
        f" | sim/theory median {statistics.median(ratio):.2f} p95 {ratio[int(len(ratio) * 0.95) - 1]:.2f}"
        f" | over worst {sum(r['rtt'] > r['worst'] for r in ok)}"
        f" | tx<=2h+4 {sum(r['tx'] <= 2 * r['hops'] + 4 for r in ok)}/{len(ok)}"
        f" | duplicates {sum(r.get('dup', False) for r in rows)} | breaks {sum(r.get('broken', 0) for r in rows)}"
        f" | hops {dict(sorted(Counter(r['hops'] for r in total).items()))}"
        f" | set-up {statistics.mean(r['opened'] for r in total):.1f} s"
    )


def _far_targets(network, k: int = 3) -> list[int]:
    reachable = [node for node in network.devices if network.reachable(node)]
    reachable.sort(key=lambda node: -network.medium.distance(GATEWAY, node))
    return reachable[:k]


def measure() -> None:
    for drop in (0.0, 0.05):
        measure_one("line17", lambda seed: LINE17, lambda network: [17], drop, range(1, 6))
    for count in (60, 100):
        middle = (SIZES[count] / 2, SIZES[count] / 2)
        for drop in (0.0, 0.05):
            measure_one(
                f"random{count}",
                lambda seed, count=count: scatter(count, SIZES[count], random.Random(seed)),
                _far_targets,
                drop,
                range(1, 4),
                controller_position=middle,
            )


# --------------------------------------------------------------------- trace


def trace_cost() -> None:
    for name, positions, target in (("line5", LINE, 5), ("line17", LINE17, 17)):
        for label, options in (
            ("no trace", {"auto_trace": False}),
            ("policy A", {}),
            ("also after repeats", {"trace_repeats": True}),
        ):
            times, ok, transmissions, traces, count = [], 0, 0, 0, 0
            for seed in range(1, 6):
                clock, network, controller = build(
                    positions, seed=seed, drop=0.05, gateway_options={"idle_timeout": None, **options}
                )
                opened(clock, network, controller, target)
                before = network.medium.stats["transmissions"]
                for value in range(20):
                    future = network.sessions.request(target, 3, value)
                    run(clock, network, controller, 30.0, future.done)
                    outcome = future.result(timeout=0)
                    count += 1
                    if outcome.ok:
                        ok += 1
                        times.append(outcome.elapsed)
                    run(clock, network, controller, 1.0)
                transmissions += network.medium.stats["transmissions"] - before
                traces += network.sessions.stats["traces"]
            times.sort()
            print(
                f"{name} {label}: delivered {ok}/{count} median {statistics.median(times):.2f} s"
                f" p95 {times[int(len(times) * 0.95) - 1]:.2f} s tx/command {transmissions / count:.1f} traces {traces}"
            )


# ------------------------------------------------------------------------ a5


def _a5_layout(kind: str, seed: int):
    from .wake_bench import _layout

    if kind == "line17":
        return LINE17, {}, [17], (0.0, 0.0)
    positions, extra = _layout("random" if kind == "random40" else "grid", random.Random(seed))
    gateway = (275.0, 275.0) if kind == "random40" else (280.0, 160.0)
    return positions, extra, None, gateway


def _a5_one(args) -> list[tuple]:
    kind, seed, drop, relay, gateway_options, flood = args
    RELAY.clear()
    RELAY.update(relay)
    positions, extra, targets, where = _a5_layout(kind, seed)
    clock, network, controller = build(
        positions, seed=seed, drop=drop, gateway_options={"idle_timeout": None, **gateway_options},
        controller_position=where, **extra,
    )
    if targets is None:
        targets = _far_targets(network, 3)
    rows = []
    for target in targets:
        if flood:
            broken, taken = 0, 0.0
        else:
            session, taken = opened(clock, network, controller, target, 60.0)
            if session.state != "open":
                rows.append((False, 0, 0, 0.0, [], 0.0))
                continue
        ok, times, sends = 0, [], 0
        for value in range(10):
            before = network.medium.stats["transmissions"]
            future = network.sessions.request(target, 3, value)
            run(clock, network, controller, 30.0, future.done)
            outcome = future.result(timeout=0)
            ok += outcome.ok
            if outcome.ok:
                times.append(outcome.elapsed)
            sends += network.medium.stats["transmissions"] - before
            run(clock, network, controller, 0.3)
        if not flood:
            broken = session.counts["broken"]
            network.sessions.close_session(target)
        rows.append((True, ok, broken, taken, times, sends / 10))
    return rows


def a5(seeds: int = 12, flood: bool = False, relay: Optional[dict] = None, gateway: Optional[dict] = None) -> None:

    relay, gateway = relay or {}, gateway or {}
    print(f"{'纯泛洪' if flood else '会话'}；MeshRelay {relay or '默认'}；Gateway {gateway or '默认'}；{seeds} 个种子")
    with simpool.pool() as pool:
        for kind in ("line17", "random40", "grid40"):
            for drop in (0.0, 0.05):
                jobs = [(kind, seed, drop, relay, gateway, flood) for seed in range(1, seeds + 1)]
                rows = [row for result in pool.map(_a5_one, jobs) for row in result]
                opened_rows = [row for row in rows if row[0]]
                times = sorted(t for row in opened_rows for t in row[4])
                ok = sum(row[1] for row in opened_rows)
                total = 10 * len(rows)
                print(
                    f"  {kind:>9} 丢包 {drop:.0%}: 送达 {ok}/{total}（{100 * ok / total:.1f} %） 断链 {sum(r[2] for r in opened_rows)}"
                    f" 开会话 {statistics.mean(r[3] for r in opened_rows):.1f} s"
                    f" 时延中位 {statistics.median(times):.2f} s p95 {times[int(len(times) * 0.95) - 1]:.2f} s"
                    f" 每条命令发送 {statistics.mean(r[5] for r in opened_rows):.1f}",
                    flush=True,
                )


def main(argv: list[str]) -> None:
    global SIGNAL
    argv, SIGNAL = bench_signal(argv)
    if "--widest" in argv:
        argv = [arg for arg in argv if arg != "--widest"]
        RELAY["probe_metric"] = "widest"
    command = argv[0] if argv else "sweep"
    if command == "sweep":
        sweep(int(argv[1]) if len(argv) > 1 else 40)
    elif command == "measure":
        measure()
    elif command == "trace":
        trace_cost()
    elif command == "a5":
        import ast

        options = {"relay": {}, "gw": {}}
        for arg in argv[1:]:
            if "=" in arg:
                name, value = arg.split("=", 1)
                group, key = name.split(".", 1)
                options[group][key] = ast.literal_eval(value)
        rest = [arg for arg in argv[1:] if "=" not in arg]
        seeds = int(rest[0]) if rest and rest[0].isdigit() else 12
        a5(seeds, "flood" in rest, options["relay"], options["gw"])
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
