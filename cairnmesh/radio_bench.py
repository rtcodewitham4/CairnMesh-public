"""Gateway radio count, task B4.5 (slow; not unit tests).

    python -m cairnmesh.radio_bench [seeds] [scan|alarm …]

Unplanned random 40 (550 m square, gateway in the middle) and planned 5 × 8
grid (``session_bench a5`` layouts), always awake, no extra loss. A gateway
with 1 or 2 radios: radio 0 keeps the common channel, the second carries
the session on a channel of its own (task Q12; one session at a time, user
2026-10-20, so a third radio has nothing to do); "阻塞" means one
gateway radio sending deafens the others (``GeoMedium.site_blocking``: the
isolation of a real board is not measured yet, the two rows bound it).

``scan``: after a first scan (the node book), a session to the farthest
node, a second scan started 4 s after it opens, a command every 2 s (20):
delivered, median and p95 latency, and the second scan's coverage of
reachable nodes (answered, plus the session's path when on a channel of its
own) and duration; "不开会话": the same scans with no session.

``alarm``: a session to the farthest node with a command every 2 s, and
every 3 s a heartbeat flooded by a random reachable node off the session's
path (10): commands delivered, heartbeats heard by the controller.

Reference results are in ``protocol_spec.md`` 15.3.
"""

from __future__ import annotations

import random
import statistics
import sys
from typing import Optional

from . import simpool
from .events import HeartbeatReceived
from .session_bench import _a5_layout, _far_targets, build, opened, run

#: ``(radios, site blocking)`` compared.
SETUPS = ((1, False), (2, False), (2, True))
LAYOUTS = ("random40", "grid40")


def _network(kind: str, seed: int, radios: int, blocking: bool):
    positions, extra, _, where = _a5_layout(kind, seed)
    clock, network, controller = build(
        positions,
        seed=seed,
        gateway_options={"idle_timeout": None, "radios": max(1, radios), "expected_nodes": len(positions)},
        controller_position=where,
        discovery_options={"join": False},
        **extra,
    )
    network.medium.site_blocking = blocking
    return clock, network, controller


def _commands(clock, network, controller, targets, count: int, gap: float = 2.0) -> list:
    futures = []
    for _ in range(count):
        for target in targets:
            futures.append(network.sessions.request(target, 3, 1))
        run(clock, network, controller, gap)
    run(clock, network, controller, 30.0, lambda: all(f.done() for f in futures))
    return futures


def _outcome(futures) -> tuple[int, list[float]]:
    results = [f.result(timeout=0) for f in futures if f.done()]
    return sum(r.ok for r in results), [r.elapsed for r in results if r.ok]


def scan_one(args) -> Optional[dict]:
    kind, seed, radios, blocking = args
    clock, network, controller = _network(kind, seed, radios, blocking)
    first = network.sessions.scan()  # the node book a running gateway has
    run(clock, network, controller, 300.0, lambda: first.done)
    target = _far_targets(network, 1)[0]
    session, _ = opened(clock, network, controller, target, 60.0)
    if session.state != "open":
        return None
    run(clock, network, controller, 3.0)
    path = {*session.route, target} if session.radio else set()
    if not radios:
        network.sessions.close_session(target)  # the reference: a scan on its own
    run(clock, network, controller, 1.0)
    scan = network.sessions.scan()
    futures = _commands(clock, network, controller, [target] if radios else [], 20)
    run(clock, network, controller, 300.0, lambda: scan.done)
    reach = {n for n in network.devices if network.reachable(n)}
    heard = set(scan.answered) | path
    ok, times = _outcome(futures)
    return {
        "ok": ok,
        "sent": len(futures),
        "times": times,
        "coverage": len(heard & reach) / len(reach),
        "duration": (scan.finished_at or clock.now) - scan.started,
        "closed": bool(radios) and session.state == "closed",
    }


def alarm_one(args) -> Optional[dict]:
    kind, seed, radios, blocking = args
    clock, network, controller = _network(kind, seed, radios, blocking)
    target = _far_targets(network, 1)[0]
    session, _ = opened(clock, network, controller, target, 60.0)
    if session.state != "open":
        return None
    run(clock, network, controller, 3.0)
    heard = []
    controller.events.subscribe(lambda e: heard.append(e) if isinstance(e, HeartbeatReceived) else None)
    rng = random.Random(seed)
    path = {*session.route, target}
    off = sorted(n for n in network.devices if n not in path and network.reachable(n))
    futures, beats = [], 0
    for step in range(30):
        if step % 2 == 0:
            futures.append(network.sessions.request(target, 3, 1))
        if step % 3 == 0:
            network.devices[rng.choice(off)].send_heartbeat()
            beats += 1
        run(clock, network, controller, 1.0)
    run(clock, network, controller, 30.0, lambda: all(f.done() for f in futures))
    ok, times = _outcome(futures)
    return {"ok": ok, "sent": len(futures), "times": times, "beats": len(heard), "beats_sent": beats}


def _p95(times: list[float]) -> float:
    times = sorted(times)
    return times[max(0, int(len(times) * 0.95) - 1)] if times else float("nan")


def _summary(rows: list[dict], skipped: int) -> str:
    ok = sum(r["ok"] for r in rows)
    sent = sum(r["sent"] for r in rows)
    times = [t for r in rows for t in r["times"]]
    median = statistics.median(times) if times else float("nan")
    text = "" if not sent else f"送达 {ok}/{sent}（{100 * ok / max(1, sent):.1f} %） 中位 {median:.2f} s p95 {_p95(times):.2f} s"
    if "coverage" in rows[0]:
        text += (
            f" 扫描覆盖 {100 * statistics.mean(r['coverage'] for r in rows):.1f} %"
            f" 用时 {statistics.mean(r['duration'] for r in rows):.0f} s 会话被关 {sum(r['closed'] for r in rows)}"
        )
    if "beats" in rows[0]:
        text += f" 心跳收到 {sum(r['beats'] for r in rows)}/{sum(r['beats_sent'] for r in rows)}"
    if skipped:
        text += f"（{skipped} 个种子开不了会话）"
    return text


def main(argv: list[str]) -> None:

    seeds = int(argv[0]) if argv else 12
    scenarios = {"scan": scan_one, "alarm": alarm_one}
    chosen = [name for name in argv[1:] if name in scenarios] or list(scenarios)
    with simpool.pool() as pool:
        for name in chosen:
            print(f"{name}（{seeds} 个种子）", flush=True)
            for kind in LAYOUTS:
                for radios, blocking in ((0, False), *SETUPS) if name == "scan" else SETUPS:
                    jobs = [(kind, seed, radios, blocking) for seed in range(1, seeds + 1)]
                    results = list(pool.map(scenarios[name], jobs))
                    rows = [r for r in results if r is not None]
                    label = f"{radios} 个射频{'、阻塞' if blocking else ''}" if radios else "不开会话"
                    summary = _summary(rows, len(results) - len(rows)) if rows else "无"
                    print(f"  {kind:>9} {label:<10} {summary}", flush=True)


if __name__ == "__main__":
    main(sys.argv[1:])
