"""Trace one deterministic ``issue15.py`` case through the gateway's diagnostics.

    PYTHONPATH=. .venv/bin/python tests/experiments/issue15_trace.py [none|mid] [seed] [radios] [strikes]

This runs the same random40 case as ``issue15.py`` without a process pool and
prints the opened path, measured hop margins, gateway history, all command
latencies, and the gateway's per-command events. It changes no protocol code.
"""

from __future__ import annotations

import statistics
import sys
from collections import Counter, deque

from cairnmesh.controller import QueueFull
from cairnmesh.radio_bench import _network
from cairnmesh.session_bench import _far_targets, opened, run

GATEWAY = 0x42


def _inject(network, dead) -> None:
    """Make ``dead`` links carry nothing off the common channel."""

    medium = network.medium
    original = medium._copy_rssi

    def copy_rssi(sender, receiver, air):
        if air.channel != 0 and frozenset((sender, receiver)) in dead:
            return None
        return original(sender, receiver, air)

    medium._copy_rssi = copy_rssi


def main() -> None:
    fault = sys.argv[1] if len(sys.argv) > 1 else "mid"
    seed = int(sys.argv[2]) if len(sys.argv) > 2 else 1
    radios = int(sys.argv[3]) if len(sys.argv) > 3 else 2
    strikes = int(sys.argv[4]) if len(sys.argv) > 4 else None
    if fault not in ("none", "mid"):
        raise SystemExit("fault must be none or mid")

    clock, network, controller = _network("random40", seed, radios, False)
    if strikes is not None:
        # Process-local A/B: 99 leaves the session channel assigned after two
        # quick breaks, as the implementation before the fix did.
        network.sessions.radio_strikes = strikes
    target = _far_targets(network, 1)[0]
    dead: set = set()
    if fault == "mid":
        # This assignment happens after the opening, exactly as issue15.py.
        pass
    _inject(network, dead)
    session, opened_for = opened(clock, network, controller, target, 60.0)
    if session.state != "open":
        raise SystemExit("session did not open")

    path = (GATEWAY, *session.route, target)
    opened_links = tuple(session.links)
    if fault == "mid":
        i = len(path) // 2 - 1
        dead.add(frozenset(path[i : i + 2]))

    # issue15.py keeps only 20 deliveries; keep all 60 for this diagnostic.
    session.deliveries = deque(maxlen=100)
    futures = []
    refused = 0
    for _ in range(60):
        try:
            futures.append(network.sessions.request(target, 3, 1))
        except QueueFull:
            refused += 1
        run(clock, network, controller, 2.0)
    run(clock, network, controller, 60.0, lambda: all(f.done() for f in futures))

    outcomes = [f.result(timeout=0) for f in futures if f.done()]
    print(
        f"case={fault} random40 seed={seed} radios={radios} "
        f"strikes={getattr(network.sessions, 'radio_strikes', 'n/a')} "
        f"opened={opened_for:.2f}s path={path} dead={sorted(tuple(sorted(edge)) for edge in dead)}"
    )
    print(f"session: radio={session.radio} sf={session.sf} counts={dict(session.counts)}")
    print(f"gateway stats: {dict(network.sessions.stats)}")
    floor = network.sessions.relay.probe_weak_snr
    for a, b, link in zip(path, path[1:], opened_links):
        margin = link.snr_db - floor
        print(f"opened hop {a:02X}->{b:02X}: snr={link.snr_db:.1f} dB margin={margin:.1f} dB")
    print(
        f"commands: done={sum(o.ok for o in outcomes)}/{len(futures)} "
        f"statuses={dict(Counter(o.status for o in outcomes))} refused={refused}"
    )
    elapsed = [o.elapsed for o in outcomes]
    if elapsed:
        ordered = sorted(elapsed)
        p95 = ordered[max(0, int(len(ordered) * 0.95) - 1)]
        print(
            f"latency: median={statistics.median(elapsed):.3f}s "
            f"p95={p95:.3f}s max={max(elapsed):.3f}s"
        )

    print("history:")
    for at, event, node in network.sessions.history:
        print(f"  {at:8.3f} {event:20} 0x{node:02X}")

    print("deliveries:")
    for index, delivery in enumerate(session.deliveries):
        if delivery.ok and not delivery.faulty and not delivery.anomalous:
            continue
        print(
            f"  #{index:02d} sent={delivery.sent_at:.3f} finished={delivery.finished_at} "
            f"ok={delivery.ok} fell_back={delivery.fell_back} "
            f"broken_at={delivery.broken_at} first_hop_sends={delivery.first_hop_sends}"
        )
        for at, event, node in delivery.events:
            where = "" if node is None else f" 0x{node:02X}"
            print(f"    {at:8.3f} {event}{where}")


if __name__ == "__main__":
    main()
