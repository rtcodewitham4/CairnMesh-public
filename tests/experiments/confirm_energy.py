"""Layer 3: what one command kept CONFIRMING for an hour costs a dormant network.

    PYTHONPATH=. .venv/bin/python tests/experiments/confirm_energy.py N [ordinary|important]

The command is an ordinary one by default (the controller ends it at its 30 min
``confirm_limit``, so the hour costs about half); ``important`` keeps confirming (the earlier behaviour).

Per seed, the same dormant network twice: a far target is switched off,
then (a) a command is sent to it and stays confirming for an hour, (b) no
command. The difference is the cost of the query rounds (every 2 min for
30 min, then every 30 min): wakes and charge per node.
"""

from __future__ import annotations

import json
import sys

from cairnmesh import simpool
from cairnmesh import wake_bench as wb
from cairnmesh.controller import DEFAULT_CONFIRM_WINDOW

HOUR = 3600.0
IMPORTANT = False


def once(args) -> dict:
    kind, seed, command = args
    rng, clock, network, _ = wb._build(kind, seed, {"idle_timeout": 60.0, "lease": 15})
    controller = network.controller(
        network.sessions, clock=clock, timeout=8.0, max_retries=2, confirm_window=DEFAULT_CONFIRM_WINDOW
    )
    network.sessions.attach(controller)
    depth = wb._depths(network.medium, network.controller_id)
    far = sorted(node for node, d in depth.items() if node in network.devices and d >= 2)
    target = rng.choice(far)
    network.set_power(target, False)
    meters = {node: network.medium.port(node).meter for node in network.devices if node != target}
    before = {node: meter.charge(clock.now) for node, meter in meters.items()}
    woken = sum(agent.stats["woken"] for agent in network.wakers.values())
    future = network.sessions.request(target, 3, 1, important=IMPORTANT) if command else None
    end = clock.now + HOUR
    while clock.now < end:
        clock.now += wb.STEP
        network.tick(clock.now)
        controller.tick(clock.now)
    used = {node: meter.charge(clock.now) - before[node] for node, meter in meters.items()}
    item = controller.confirming()
    return {
        "kind": kind,
        "seed": seed,
        "command": command,
        "hops": depth[target],
        "rounds": item[0].rounds if item else None,
        "still_confirming": bool(item),
        "done": future.done() if future else None,
        "woken": sum(agent.stats["woken"] for agent in network.wakers.values()) - woken,
        "mean_C": sum(used.values()) / len(used),
        "max_C": max(used.values()),
    }


def main(argv: list[str]) -> None:
    global IMPORTANT
    IMPORTANT = len(argv) > 1 and argv[1] == "important"
    seeds = int(argv[0]) if argv else 8
    jobs = [(kind, seed, command) for kind in ("random", "grid") for seed in range(1, seeds + 1) for command in (True, False)]
    with simpool.pool() as pool:
        rows = list(pool.map(once, jobs))
    with open("/tmp/cairnmesh-experiments/confirm_energy.json", "w", encoding="utf-8") as handle:
        json.dump(rows, handle)
    for kind in ("random", "grid"):
        pairs = [(a, b) for a in rows for b in rows
                 if a["kind"] == b["kind"] == kind and a["seed"] == b["seed"] and a["command"] and not b["command"]]
        extra_wakes = sum(a["woken"] - b["woken"] for a, b in pairs) / len(pairs)
        extra_mean = sum(a["mean_C"] - b["mean_C"] for a, b in pairs) / len(pairs) / 3.6  # C / 3.6 = mAh
        extra_max = sum(a["max_C"] - b["max_C"] for a, b in pairs) / len(pairs) / 3.6
        rounds = sum(a["rounds"] or 0 for a, _ in pairs) / len(pairs)
        print(f"{kind}: {len(pairs)} 种子，确认中 1 h 查询 {rounds:.1f} 轮；多唤醒 {extra_wakes:.0f} 次（全网合计），"
              f"每节点平均多耗 {extra_mean:.3f} mAh，最耗电节点多耗 {extra_max:.3f} mAh；"
              f"仍在确认中 {sum(a['still_confirming'] for a, _ in pairs)}/{len(pairs)}")


if __name__ == "__main__":
    main(sys.argv[1:])
