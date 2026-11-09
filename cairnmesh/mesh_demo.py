"""Compare flooding strategies on random topologies: ``python -m cairnmesh.mesh_demo``.

Every row averages several seeds. Nodes are scattered at random in a square
with the controller in the middle; only nodes with some radio path to the
controller are asked. "顺序" sends one request at a time, "并发" sends one to
every node at once (the controller queues per node). Timing values are
simulator estimates, not measured airtime.
"""

from __future__ import annotations

import random
import statistics
import sys
from typing import Optional

from .controller import Controller
from . import mesh_sim
from .mesh_sim import MeshNetwork, scatter

KEY = bytes(range(16))

STRATEGIES: dict[str, dict] = {
    "朴素泛洪": dict(backoff=(0.0, 0.02), suppress_after=None, guided_replies=False, hop_retries=0),
    "仅退避": dict(suppress_after=None, guided_replies=False, hop_retries=0),
    "退避+抑制": dict(guided_replies=False, hop_retries=0),
    "受控泛洪(默认)": {},
}
"""Ablation: each row adds mechanisms; the last is ``MeshRelay``'s defaults."""


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _run(network: MeshNetwork, controller: Controller, clock: _Clock, futures, limit: float) -> None:
    end = clock.now + limit
    while clock.now < end and not (all(f.done() for f in futures) and network.quiet()):
        clock.now += 0.005
        network.tick(clock.now)
        controller.tick(clock.now)


def trial(
    count: int,
    size: float,
    options: dict,
    seed: int,
    *,
    concurrent: bool = False,
    radio_range: float = 150.0,
    drop: float = 0.05,
) -> Optional[dict]:
    """One topology, one strategy; ``None`` when no node can reach the controller."""

    rng = random.Random(seed)
    positions = scatter(count, size, rng)
    clock = _Clock()
    network = MeshNetwork(
        positions,
        key=KEY,
        controller_position=(size / 2, size / 2),
        radio_range=radio_range,
        drop=drop,
        relay_options=options,
        seed=seed,
        clock=clock,
    )
    controller = network.controller(network.gateway, clock=clock, timeout=8.0, max_retries=2, confirm_window=None)
    reachable = [node for node in positions if network.reachable(node)]
    if not reachable:
        return None
    if concurrent:
        futures = [controller.request(node, 3, 1) for node in reachable]
        _run(network, controller, clock, futures, limit=120.0)
    else:
        futures = []
        for node in reachable:
            futures.append(controller.request(node, 3, 1))
            _run(network, controller, clock, futures[-1:], limit=60.0)
    outcomes = [f.result(timeout=0) for f in futures if f.done()]
    ok = [o for o in outcomes if o.ok]
    runs = [network.devices[node].handled_count for node in reachable]
    return {
        "reachable": len(reachable),
        "delivered": len(ok) / len(reachable),
        "tx_per_request": network.medium.stats["transmissions"] / len(reachable),
        "collisions": network.medium.stats["collisions"] / len(reachable),
        "attempts": statistics.mean(o.attempts for o in ok) if ok else float("nan"),
        "duplicated": sum(1 for r in runs if r > 1),
        "elapsed": statistics.mean(o.elapsed for o in ok) if ok else float("nan"),
    }


def main(seeds: int = 3) -> None:
    # (label, devices, square size, concurrent load?)
    scenarios = [
        ("稀疏 30 节点", 30, 900.0, False),
        ("中等 60 节点", 60, 900.0, False),
        ("密集 100 节点", 100, 700.0, False),
        ("中等 60 节点", 60, 900.0, True),
    ]
    header = f"{'场景':<14}{'负载':<6}{'策略':<16}{'可达':>5}{'送达率':>8}{'发送/请求':>10}{'碰撞/请求':>10}{'尝试':>6}{'耗时 s':>8}{'重复执行':>8}"
    print(header)
    print("-" * len(header.encode("gbk")))
    for label, count, size, concurrent in scenarios:
        for name, options in STRATEGIES.items():
            rows = [
                r
                for seed in range(seeds)
                if (r := trial(count, size, options, seed, concurrent=concurrent)) is not None
            ]
            mean = {key: statistics.mean(r[key] for r in rows) for key in rows[0]}
            dup = sum(r["duplicated"] for r in rows)
            print(
                f"{label:<14}{'并发' if concurrent else '顺序':<6}{name:<16}"
                f"{mean['reachable']:>5.0f}{mean['delivered']:>8.0%}{mean['tx_per_request']:>10.1f}"
                f"{mean['collisions']:>10.1f}{mean['attempts']:>6.2f}{mean['elapsed']:>8.2f}{dup:>8}"
            )
        print()


if __name__ == "__main__":
    if "--disc" in sys.argv[1:]:  # the old disc model instead of the link budget
        mesh_sim.DEFAULT_SIGNAL = None
    main()
