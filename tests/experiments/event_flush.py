"""Module events held in a sleeping network, flushed when the gateway wakes it (EVENT, wire format v4).

    PYTHONPATH=. .venv/bin/python tests/experiments/event_flush.py SEEDS [NODES] [PER_NODE] [SPREAD_MAX ...]

Per seed and layout (random40 / grid40, link-budget model, dormant after the
wake bench's warm-up): ``NODES`` nodes each raise ``PER_NODE`` distinct
events while the network sleeps; the gateway then sends one WAKE_NET
(lease 60 s, as ``wake_bench net``). Per ``wake_spread`` upper bound: how
many events reach the controller within 60 / 120 / 300 s, when, how many
EVENT frames the nodes sent per event, and how many transmissions the
flush cost on air. Nodes the gateway cannot reach at all are left out.
"""

from __future__ import annotations

import sys

from cairnmesh import simpool
from cairnmesh import wake_bench as wb
from cairnmesh.events import NodeEvent


def once(args: tuple[str, int, int, int, float]) -> dict:
    kind, seed, nodes, per_node, spread = args
    rng, clock, network, _ = wb._build(kind, seed)
    controller = network.controller(network.gateway, clock=clock)
    got: dict[tuple[int, int], float] = {}
    controller.events.subscribe(
        lambda e: got.setdefault((e.node_id, e.event_id), clock.now) if isinstance(e, NodeEvent) else None
    )
    depth = wb._depths(network.medium, network.controller_id)
    reachable = sorted(node for node in network.nodes if node in depth)
    chosen = rng.sample(reachable, min(nodes, len(reachable)))
    for node in chosen:
        network.nodes[node].events.wake_spread = (1.0, spread)
        for k in range(per_node):
            assert network.nodes[node].notify(k + 1)
    wb._run(network, clock, clock.now + 10.0)
    start = clock.now
    on_air = network.medium.stats["transmissions"]
    network.waker.wake(wb.NET_LEASE)
    wb._run(network, clock, start + 300.0)
    times = sorted(t - start for t in got.values())
    raised = len(chosen) * per_node
    sent = sum(network.nodes[n].events.stats["events_sent"] for n in chosen)
    return {
        "raised": raised,
        "in60": sum(t <= 60 for t in times),
        "in120": sum(t <= 120 for t in times),
        "in300": len(times),
        "median": times[len(times) // 2] if times else None,
        "last": times[-1] if times else None,
        "frames_per_event": sent / raised if raised else 0.0,
        "tx": network.medium.stats["transmissions"] - on_air,
        "pending": sum(len(network.nodes[n].events.pending()) for n in chosen),
    }


def main(argv: list[str]) -> None:
    seeds = int(argv[0]) if argv else 8
    nodes = int(argv[1]) if len(argv) > 1 else 40
    per_node = int(argv[2]) if len(argv) > 2 else 1
    spreads = [float(a) for a in argv[3:]] or [15.0]
    jobs = [
        (kind, seed, nodes, per_node, spread)
        for spread in spreads
        for kind in ("random", "grid")
        for seed in range(1, seeds + 1)
    ]
    with simpool.pool() as pool:
        results = list(pool.map(once, jobs))
    print(f"nodes={nodes} per_node={per_node} seeds={seeds}")
    print("spread  layout  raised  ≤60s  ≤120s  ≤300s  median  last  frames/event  tx/run  pending")
    for spread in spreads:
        for kind in ("random", "grid"):
            rows = [r for job, r in zip(jobs, results) if job[0] == kind and job[4] == spread]
            total = lambda key: sum(r[key] for r in rows)  # noqa: E731
            medians = sorted(r["median"] for r in rows if r["median"] is not None)
            lasts = [r["last"] for r in rows if r["last"] is not None]
            print(
                f"{spread:6.0f}  {kind:6}  {total('raised'):6}  {total('in60'):4}  {total('in120'):5}  {total('in300'):5}"
                f"  {medians[len(medians) // 2] if medians else float('nan'):6.1f}"
                f"  {max(lasts) if lasts else float('nan'):5.1f}"
                f"  {total('frames_per_event') / len(rows):12.2f}  {total('tx') / len(rows):6.0f}  {total('pending')}"
            )


if __name__ == "__main__":
    main(sys.argv[1:])
