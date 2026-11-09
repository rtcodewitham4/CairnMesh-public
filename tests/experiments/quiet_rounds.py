"""Layer 3: how much later a confirming command gets its outcome when scheduled rounds stop waking the whole network.

    PYTHONPATH=. .venv/bin/python tests/experiments/quiet_rounds.py N [V0,V1,V2,V2J] [ABCD] [ordinary|important]

The commands are ordinary ones by default (ended UNCONFIRMED at the controller's 30 min
``confirm_limit``); ``important`` confirms on, the behaviour before that change.

Dormant network with node discovery (a node that boots sends a JOIN), per
seed and variant (``Controller.confirm.quiet_after``: V0 None = 2 min rounds
for 30 min; V1 0 = only when heard, else every 30 min; V2 600 s = 2 min
rounds for 10 min first; V2J = V2 with the boot wake JOIN, Q22):

* A: the target loses power 10–60 min right after the command goes out;
* B: every relay on the target's path loses power 10–60 min (the target
  stays on and says nothing: the gateway cannot hear that it is back);
* C: 30 % loss, 6 commands in a row (rounds fail by bad luck);
* D: the whole area loses power 10–60 min right after the command (every
  node boots at once: the JOIN storm, Q22's worst case).

Per run: when the outcome came (after the power came back for A / B),
wakes and charge per node until a fixed end.
"""

from __future__ import annotations

import json
import random
import sys

from cairnmesh import session_bench as sb
from cairnmesh import simpool
from cairnmesh import wake_bench as wb
from cairnmesh.controller import DEFAULT_CONFIRM_WINDOW

#: ``confirm_quiet_after``, ``join_wakes`` (Q22: a booting node JOINs at once with the long preamble)
VARIANTS = {"V0": (None, False), "V1": (0.0, False), "V2": (600.0, False), "V2J": (600.0, True)}
HORIZON = 3 * 3600.0
IMPORTANT = False


def once(args) -> dict:
    scenario, kind, seed, variant = args
    quiet_after, join_wakes = VARIANTS[variant]
    wb.RELAY["join_wakes"] = join_wakes
    rng, clock, network, _ = wb._build(kind, seed, {"idle_timeout": 60.0, "lease": 15}, discovery=True)
    network.medium.drop = 0.30 if scenario == "C" else 0.20
    controller = network.controller(
        network.sessions, clock=clock, timeout=8.0, max_retries=2, confirm_window=DEFAULT_CONFIRM_WINDOW,
        confirm_quiet_after=quiet_after,
    )
    gateway = network.sessions
    gateway.attach(controller)
    depth = wb._depths(network.medium, network.controller_id)
    far = sorted(node for node, d in depth.items() if node in network.devices and d >= 2)
    target = rng.choice(far)
    cut_rng = random.Random(f"{seed}-cut")  # the same cut for every variant
    meters = {node: network.medium.port(node).meter for node in network.devices}
    start = clock.now
    before = {node: meter.charge(clock.now) for node, meter in meters.items()}
    woken = sum(agent.stats["woken"] for agent in network.wakers.values())
    session, _ = sb.opened(clock, network, controller, target, 60.0)
    results = []
    for value in range(1 if scenario != "C" else 6):
        future = gateway.request(target, 3, value, important=IMPORTANT)
        sent = clock.now
        restored = None
        if scenario in ("A", "B", "D"):
            sb.run(clock, network, controller, cut_rng.uniform(0.0, 3.0))
            if scenario == "A":
                victims = [target]
            elif scenario == "B":
                victims = [n for n in session.route if n != target] or [target]
            else:
                victims = sorted(network.devices)
            for node in victims:
                network.set_power(node, False)
            sb.run(clock, network, controller, cut_rng.uniform(600.0, 3600.0), future.done)
            for node in victims:
                network.set_power(node, True)
            restored = clock.now
        sb.run(clock, network, controller, start + HORIZON - clock.now, future.done)
        outcome = future.result(timeout=0) if future.done() else None
        results.append({
            "status": outcome.status if outcome else "still_confirming",
            "queried": outcome.queried if outcome else True,
            "elapsed": clock.now - sent,
            "after_restore": None if restored is None or outcome is None else max(0.0, clock.now - restored),
            "done_before_restore": restored is not None and outcome is not None and clock.now <= restored,
        })
        if outcome is None:
            controller.abandon(target)
        sb.run(clock, network, controller, 1.0)
    sb.run(clock, network, controller, max(0.0, start + HORIZON - clock.now))
    used = [meter.charge(clock.now) - before[node] for node, meter in meters.items()]
    return {
        "scenario": scenario, "kind": kind, "seed": seed, "variant": variant, "results": results,
        "woken": sum(agent.stats["woken"] for agent in network.wakers.values()) - woken,
        "mean_C": sum(used) / len(used),
    }


def main(argv: list[str]) -> None:
    global IMPORTANT
    IMPORTANT = len(argv) > 3 and argv[3] == "important"
    seeds = int(argv[0]) if argv else 8
    scenarios = argv[2] if len(argv) > 2 else "ABCD"
    variants = argv[1].split(",") if len(argv) > 1 else list(VARIANTS)
    jobs = [(sc, kind, seed, v) for sc in scenarios for kind in ("random", "grid") for seed in range(1, seeds + 1) for v in variants]
    with simpool.pool() as pool:
        rows = list(pool.map(once, jobs))
    with open("/tmp/cairnmesh-experiments/quiet_rounds.json", "w", encoding="utf-8") as handle:
        json.dump(rows, handle)
    for sc in scenarios:
        for kind in ("random", "grid"):
            print(f"--- {sc} {kind}")
            for v in variants:
                runs = [r for r in rows if (r["scenario"], r["kind"], r["variant"]) == (sc, kind, v)]
                res = [x for r in runs for x in r["results"] if x["queried"]]
                late = sorted(x["after_restore"] for x in res if x["after_restore"] is not None)
                conf = sorted(x["elapsed"] for x in res)
                left = sum(x["status"] == "still_confirming" for x in res)
                unconf = sum(x["status"] == "unconfirmed" for x in res)
                mean = lambda xs: sum(xs) / len(xs) if xs else float("nan")
                print(
                    f"{v}: 查询得结论 {len(res)} 条，3 h 内没结论 {left}，未确认（到点结案）{unconf}；"
                    + (f"恢复后多久出结论 平均 {mean(late) / 60:.1f} / 最长 {late[-1] / 60:.1f} min；" if late else "")
                    + (f"首发到结论 平均 {mean(conf) / 60:.1f} / 最长 {conf[-1] / 60:.1f} min；" if conf and sc == "C" else "")
                    + f"全网唤醒 {mean([r['woken'] for r in runs]):.0f} 次 / 3 h，每节点 {mean([r['mean_C'] for r in runs]) / 3.6:.2f} mAh / 3 h"
                )


if __name__ == "__main__":
    main(sys.argv[1:])
