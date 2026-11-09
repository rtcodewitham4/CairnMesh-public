"""Layer 2 signals on accepted nodes: re-assessment at each command, first-hop loss estimate; against outcomes."""
import sys, json, math
from collections import Counter
from concurrent.futures import ProcessPoolExecutor

sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.abspath(__file__)))
import accept_validate as av
from cairnmesh import stress_bench as s, session_bench as sb, wake_bench as wb

PRE_ROUNDS, PRE_LOST = 5, 2
CASES = {"A30": (0.30, 0.0), "B50": (0.50, 0.0), "C30w9": (0.30, 9.0), "D0": (0.0, 0.0)}


def one(args):
    kind, seed, case = args
    drop, weaken = CASES[case]
    rng, clock, network, controller, entry, positions = av.build(kind, seed, {})
    gw = network.sessions
    depth = wb._depths(network.medium, network.controller_id)
    far = sorted(n for n, d in depth.items() if n in network.devices and d >= 2)
    targets = rng.sample(far, min(2, len(far)))
    gw.scan(); sb.run(clock, network, controller, 300.0, lambda: gw.scan_state is None)
    hop = {}  # target -> [attempts, failures]
    orig = gw._on_route_event
    def event(record, what):
        if record.sender == gw.controller_id and record.index == 0 and what in ("acked", "answered", "gave_up"):
            a = hop.setdefault(record.target, [0, 0])
            a[0] += record.sends
            a[1] += record.sends - (1 if what != "gave_up" else 0)
        return orig(record, what)
    gw._on_route_event = event
    for r in list(network.relays.values()) + [a.relay for a in gw._arms]:
        if r.route_event == orig:
            r.route_event = event
    rows = []
    for target in targets:
        network.medium.drop = 0.0
        gw.accept(target, rounds=20)
        sb.run(clock, network, controller, 400.0, lambda: target in gw.acceptance)
        if not gw.acceptance[target].passed:
            gw.close_session(target)
            continue
        if weaken:
            network.medium.radio_range *= 10 ** (-weaken / (10 * network.medium.signal.exponent))
        network.medium.drop = drop
        hop.pop(target, None)
        ledger = s.Ledger(network)
        fails, checks, phat, healthy = 0, 0, [], []
        for value in range(12):
            gw.open_session(target)
            sess = gw.sessions.get(target)
            if sess is not None and sess.state == "open":
                checks += 1
                fails += not gw.assess(target).passed
            h = gw.health.get(target)
            reasons = sorted(h.reasons) if h is not None else []
            pre = []
            healthy.append(dict(hist=reasons))
            ledger.command(clock, network, controller, target, value)
            a = hop.get(target, [0, 0])
            phat.append(1 - math.sqrt(max(0.0, 1 - a[1] / a[0])) if a[0] else None)
        if weaken:
            network.medium.radio_range *= 10 ** (weaken / (10 * network.medium.signal.exponent))
        h = gw.health.get(target)
        final = sorted(h.reasons) if h is not None else []
        rows.append(dict(final=final, seed=seed, target=target, statuses=[o[2] for o in ledger.outcomes], assess_checks=checks,
                         assess_fails=fails, phat=phat, hop=hop.get(target), healthy=healthy))
        gw.close_session(target)
    return rows


if __name__ == "__main__":
    seeds = int(sys.argv[1]); first = int(sys.argv[2])
    out = {}
    for case in CASES:
        for kind in ("random", "grid"):
            with ProcessPoolExecutor() as pool:
                out[f"{case}/{kind}"] = [r for rr in pool.map(one, [(kind, sd, case) for sd in range(first, first + seeds)]) for r in rr]
            print(case, kind, "done", flush=True)
    json.dump(out, open(f"/tmp/cairnmesh-experiments/monitor_{first}_{seeds}.json", "w"))
