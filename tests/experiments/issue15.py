"""A session whose path cannot work on the session channel (spec 15.3).

    PYTHONPATH=. .venv/bin/python tests/experiments/issue15.py [seeds]

A gateway with 2 radios (1 for reference), unplanned random 40 and planned
grid 40 (``radio_bench`` layouts), a session to the farthest node and a
command every 2 s for 120 s. Faults, injected in ``GeoMedium._copy_rssi``
(the simulator's two channels otherwise have the same link medians, so no
benchmark meets this): ``none``; ``mid``, one hop in the middle of the
opened path dead off channel 0; ``target``, every link of the target dead
off channel 0 (its antenna mistuned there, a multipath null). Prints, per
case: commands delivered, session breaks, radio switches, commands refused.
"""

from __future__ import annotations

import sys

from cairnmesh import simpool
from cairnmesh.controller import QueueFull
from cairnmesh.radio_bench import _network
from cairnmesh.session_bench import _far_targets, opened, run

GATEWAY = 0x42


def _inject(network, dead) -> None:
    """Links in ``dead`` (unordered pairs) carry nothing off channel 0."""

    medium = network.medium
    original = medium._copy_rssi

    def copy_rssi(sender, receiver, air):
        if air.channel != 0 and frozenset((sender, receiver)) in dead:
            return None
        return original(sender, receiver, air)

    medium._copy_rssi = copy_rssi


def one(args) -> dict:
    kind, seed, radios, fault = args
    clock, network, controller = _network(kind, seed, radios, False)
    target = _far_targets(network, 1)[0]
    dead: set = set()
    if fault == "target":
        dead |= {frozenset((target, n)) for n in [GATEWAY, *network.devices] if n != target}
    _inject(network, dead)
    session, _ = opened(clock, network, controller, target, 60.0)
    if session.state != "open":
        return {"skipped": 1}
    if fault == "mid":
        path = (GATEWAY, *session.route, target)
        i = len(path) // 2 - 1
        dead.add(frozenset(path[i : i + 2]))
    futures, refused = [], 0
    for _ in range(60):
        try:
            futures.append(network.sessions.request(target, 3, 1))
        except QueueFull:
            refused += 1
        run(clock, network, controller, 2.0)
    run(clock, network, controller, 60.0, lambda: all(f.done() for f in futures))
    results = [f.result(timeout=0) for f in futures if f.done()]
    return {
        "ok": sum(r.ok for r in results),
        "sent": 60,
        "broken": session.counts["broken"],
        "switches": session.counts["radio_switches"],
        "refused": refused,
        "banned": network.sessions.stats.get("radio_bans", 0),
    }


def main() -> None:
    seeds = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    cases = [(k, s, r, f) for f in ("none", "mid", "target") for r in (2, 1) for k in ("random40", "grid40") for s in range(1, seeds + 1)]
    with simpool.pool() as pool:
        rows = list(pool.map(one, cases))
    for (kind, seed, radios, fault), row in zip(cases, rows):
        if "skipped" in row:
            print(f"{fault:6} r{radios} {kind:8} s{seed}: no session")
            continue
        print(
            f"{fault:6} r{radios} {kind:8} s{seed}: 送达 {row['ok']:2}/{row['sent']}  断链 {row['broken']:2}  切射频 {row['switches']:2}"
            f"  拒收 {row['refused']:2}  禁用 {row['banned']}"
        )


if __name__ == "__main__":
    main()
