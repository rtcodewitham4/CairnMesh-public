"""Flooded heartbeats vs HELLO + reports: ``python -m cairnmesh.liveness_demo``.

Each run scatters 60 devices (900 × 900, radio range 150, 5 % loss), lets
the liveness scheme warm up, switches 3 reachable devices off and keeps
going, while the controller sends one request every 15 s to a device that
still has a radio path.

* heartbeat: devices flood a heartbeat every interval; the controller calls
  a node offline after ``miss_limit`` intervals without one;
* hello: one-hop HELLO every interval; the controller calls a node offline
  when a LOST report arrives;
* none: requests only, the baseline for channel use.

A heartbeat can also flag devices behind a cut (silence covers them), so
its "发现" may exceed "可发现".

"可发现" counts switched-off devices with at least one neighbour that can
still reach the controller; a device behind a cut cannot be reported by
anyone (its neighbours' reports cannot get out), so it is left out. A false
alarm is a node called offline while it is on and still reachable. Runs go
in parallel processes and take well under a minute.
"""

from __future__ import annotations

import random
import statistics
import sys
from typing import Optional

from . import simpool
from .events import HeartbeatReceived, NodeReported
from .frames import ReportEvent
from .mesh_demo import KEY, _Clock
from . import mesh_sim
from .mesh_sim import MeshNetwork, scatter


def run(
    mode: str,
    interval: float,
    seed: int,
    *,
    miss_limit: int = 5,
    relay_options: Optional[dict] = None,
) -> dict:
    rng = random.Random(seed)
    positions = scatter(60, 900.0, rng)
    clock = _Clock()
    if mode == "none":
        liveness = {}
    elif mode == "heartbeat":
        liveness = dict(heartbeat_interval=interval)
    else:
        liveness = dict(hello_interval=interval, watch_options=dict(miss_limit=miss_limit))
    network = MeshNetwork(
        positions,
        key=KEY,
        controller_position=(450.0, 450.0),
        radio_range=150.0,
        drop=0.05,
        relay_options=relay_options,
        seed=seed,
        clock=clock,
        **liveness,
    )
    controller = network.controller(network.gateway, clock=clock, timeout=8.0, max_retries=2, confirm_window=None)
    reachable = [node for node in positions if network.reachable(node)]
    victims = set(random.Random(seed + 1000).sample(reachable, 3))
    kill_at = (miss_limit + 1) * interval
    end = kill_at + (miss_limit + 3) * interval

    offline: dict[int, float] = {}
    detected: dict[int, float] = {}
    false_alarms = 0
    last_beat = {node: 0.0 for node in reachable}

    def call_offline(node: int) -> None:
        nonlocal false_alarms
        if node in offline:
            return
        offline[node] = clock.now
        if node in victims and clock.now >= kill_at:
            detected.setdefault(node, clock.now - kill_at)
        elif network.reachable(node):
            false_alarms += 1

    def on_event(event) -> None:
        if isinstance(event, HeartbeatReceived) and event.node_id in last_beat:
            last_beat[event.node_id] = clock.now
            offline.pop(event.node_id, None)
        elif isinstance(event, NodeReported):
            if event.event == ReportEvent.LOST:
                call_offline(event.node_id)
            else:
                offline.pop(event.node_id, None)

    controller.events.subscribe(on_event)
    pick = random.Random(seed + 2000)
    futures = []
    detectable = 0
    next_request = interval  # leave the first interval to the warm-up
    while clock.now < end:
        clock.now += 0.01
        if kill_at <= clock.now < kill_at + 0.01:
            for node in victims:
                network.set_power(node, False)
            detectable = sum(
                any(network.reachable(n) for n in network.medium.neighbours(v) if n in network.devices)
                for v in victims
            )
        network.tick(clock.now)
        controller.tick(clock.now)
        if mode == "heartbeat" and clock.now > miss_limit * interval:
            for node, seen in last_beat.items():
                if clock.now - seen > miss_limit * interval:
                    call_offline(node)
        if clock.now >= next_request:
            next_request += 15.0
            # Only nodes that still have a radio path: switching victims off
            # may cut others away, and asking those measures nothing.
            node = pick.choice([n for n in reachable if network.reachable(n)])
            if controller.pending_count(node) == 0:
                futures.append(controller.request(node, 3, 1))

    done = [f.result(timeout=0) for f in futures if f.done()]
    return {
        "airtime": network.medium.stats["transmissions"] * network.medium.airtime / end,
        "detected": len(detected),
        "detectable": detectable,
        "latency": statistics.mean(detected.values()) if detected else float("nan"),
        "false_alarms": false_alarms,
        "requests": sum(o.ok for o in done) / len(done) if done else float("nan"),
    }


#: (label, mode, interval s, miss_limit)
CONFIGS = [
    ("基线:只发请求", "none", 60.0, 5),
    ("心跳泛洪", "heartbeat", 30.0, 3),
    ("心跳泛洪", "heartbeat", 60.0, 3),
    ("心跳泛洪", "heartbeat", 60.0, 5),
    ("HELLO+报告", "hello", 60.0, 3),
    ("HELLO+报告(默认)", "hello", 60.0, 5),
    ("HELLO+报告", "hello", 30.0, 5),
]


def main(seeds: int = 4, *, disc: bool = False) -> None:
    """``disc``: the old disc model instead of the link budget (``--disc``)."""

    jobs = [(config, seed) for config in CONFIGS for seed in range(seeds)]
    with simpool.pool(initializer=_use_disc if disc else None) as pool:
        results = list(pool.map(_job, jobs))
    header = (
        f"{'方案':<18}{'间隔 s':>7}{'判定次数':>9}{'信道占用':>9}"
        f"{'发现/可发现':>12}{'发现耗时 s':>11}{'误报':>6}{'请求送达':>9}"
    )
    print(header)
    print("-" * len(header.encode("gbk")))
    for index, (label, _, interval, miss_limit) in enumerate(CONFIGS):
        rows = results[index * seeds : (index + 1) * seeds]
        latencies = [r["latency"] for r in rows if r["detected"]]
        found = f"{sum(r['detected'] for r in rows)}/{sum(r['detectable'] for r in rows)}"
        print(
            f"{label:<18}{interval:>7.0f}{miss_limit:>9}"
            f"{statistics.mean(r['airtime'] for r in rows):>9.0%}"
            f"{found:>12}"
            f"{(statistics.mean(latencies) if latencies else float('nan')):>11.0f}"
            f"{sum(r['false_alarms'] for r in rows):>6}"
            f"{statistics.mean(r['requests'] for r in rows):>9.0%}"
        )


def _use_disc() -> None:
    mesh_sim.DEFAULT_SIGNAL = None


def _job(job) -> dict:
    (_, mode, interval, miss_limit), seed = job
    return run(mode, interval, seed, miss_limit=miss_limit)


if __name__ == "__main__":
    main(disc="--disc" in sys.argv[1:])
