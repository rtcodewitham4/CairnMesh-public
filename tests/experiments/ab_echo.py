"""End-to-end A/B of the gateway echo: command outcomes, latency, airtime. Awake network (robustness_bench setup)."""
import sys
from collections import Counter
from concurrent.futures import ProcessPoolExecutor

from cairnmesh import robustness_bench as rb, session_bench as sb


def one(args):
    kind, seed, drop, ack = args
    clock, network, controller, targets, where = rb._setup(kind, seed, drop)
    gw = network.sessions
    if not ack:
        for r in list(network.relays.values()) + [a.relay for a in gw._arms]:
            r.last_hop_ack = False
        gw.model.last_hop_ack = False
        gw._models.clear()
    out, times = Counter(), []
    tx0 = None
    for target in targets[:2]:
        sb.opened(clock, network, controller, target, 60.0)
        if tx0 is None:
            tx0 = network.medium.stats["transmissions"]
        for value in range(8):
            gw.open_session(target)
            future = gw.request(target, 3, value)
            sb.run(clock, network, controller, rb.COMMAND_WAIT, future.done)
            o = future.result(timeout=0) if future.done() else None
            if o is None:
                out["no_outcome"] += 1
            else:
                out[o.status + ("/q" if o.queried else "")] += 1
                if o.status == "done" and not o.queried:
                    times.append(o.elapsed)
            sb.run(clock, network, controller, 0.3)
        gw.close_session(target)
    out["tx"] = network.medium.stats["transmissions"] - tx0
    return out, times


if __name__ == "__main__":
    seeds = int(sys.argv[1])
    for drop in (0.0, 0.10, 0.30):
        for kind in ("random40", "grid40"):
            row = []
            for ack in (False, True):
                total, times = Counter(), []
                with ProcessPoolExecutor() as pool:
                    for c, t in pool.map(one, [(kind, s, drop, ack) for s in range(seeds)]):
                        total.update(c); times += t
                times.sort()
                n = sum(v for k, v in total.items() if k != "tx")
                p = lambda q: times[min(len(times) - 1, int(q * len(times)))] if times else float("nan")
                row.append(f"{'echo' if ack else 'blind'}: direct {total['done']}/{n} unconf {total['unconfirmed/q']} notrun {total['not_run/q']} p50 {p(.5):.2f} p95 {p(.95):.2f} tx/cmd {total['tx']/n:.1f}")
            print(f"{kind:8} loss {drop:.0%} | " + " | ".join(row), flush=True)
