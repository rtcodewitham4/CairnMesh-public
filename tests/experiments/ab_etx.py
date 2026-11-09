"""A/B of the withdrawn ``etx_interference`` relay option: dormant 30 % (TUI usage) and awake 0 % / 30 %.

Kept for the record: ``MeshRelay`` no longer has the option, so this does not run.
"""
import sys
from collections import Counter
from concurrent.futures import ProcessPoolExecutor

from cairnmesh import stress_bench as s, session_bench as sb, wake_bench as wb, robustness_bench as rb
from cairnmesh.controller import DEFAULT_CONFIRM_WINDOW


def dormant(args):
    kind, seed, p = args
    wb.RELAY.clear(); wb.RELAY["etx_interference"] = p
    rng, clock, network, _ = wb._build(kind, seed, {"idle_timeout": 60.0, "lease": 15})
    network.medium.drop = 0.30
    controller = network.controller(network.sessions, clock=clock, timeout=8.0, max_retries=2, confirm_window=DEFAULT_CONFIRM_WINDOW)
    network.sessions.attach(controller)
    ledger = s.Ledger(network)
    gw = network.sessions
    depth = wb._depths(network.medium, network.controller_id)
    far = sorted(n for n, d in depth.items() if n in network.devices and d >= 2)
    out, hops = Counter(), []
    for target in rng.sample(far, min(2, len(far))):
        session, _ = sb.opened(clock, network, controller, target, 60.0)
        if session.state == "open":
            hops.append(len(session.route) + 1)
        for value in range(6):
            gw.open_session(target)
            ledger.command(clock, network, controller, target, value)
        gw.close_session(target)
    for o in ledger.outcomes:
        out[(o[2] or "none") + ("/q" if o[4] else "")] += 1
    return out, hops


def awake(args):
    kind, seed, drop, p = args
    sb.RELAY.clear(); sb.RELAY["etx_interference"] = p
    clock, network, controller, targets, where = rb._setup(kind, seed, drop)
    gw = network.sessions
    out, times, hops = Counter(), [], []
    tx0 = None
    for target in targets[:2]:
        session, _ = sb.opened(clock, network, controller, target, 60.0)
        if session.state == "open":
            hops.append(len(session.route) + 1)
        if tx0 is None:
            tx0 = network.medium.stats["transmissions"]
        for value in range(8):
            gw.open_session(target)
            future = gw.request(target, 3, value)
            sb.run(clock, network, controller, rb.COMMAND_WAIT, future.done)
            o = future.result(timeout=0) if future.done() else None
            if o is None:
                out["none"] += 1
            else:
                out[o.status + ("/q" if o.queried else "")] += 1
                if o.status == "done" and not o.queried:
                    times.append(o.elapsed)
            sb.run(clock, network, controller, 0.3)
        gw.close_session(target)
    out["tx"] = network.medium.stats["transmissions"] - tx0
    return out, times, hops


if __name__ == "__main__":
    which = sys.argv[1]; seeds = int(sys.argv[2]); ps = [float(x) for x in sys.argv[3].split(",")]
    with ProcessPoolExecutor() as pool:
        if which == "dormant":
            for kind in ("random", "grid"):
                for p in ps:
                    total, hops = Counter(), []
                    for c, h in pool.map(dormant, [(kind, sd, p) for sd in range(seeds)]):
                        total.update(c); hops += h
                    n = sum(total.values())
                    print(f"dormant {kind:6} p={p:.2f}: direct {total['done']}/{n} confirmed {total['done/q']} notrun {total['not_run/q']} UNCONF {total['unconfirmed/q']} none {total['none']} hops {sum(hops)/max(1,len(hops)):.2f} (n={len(hops)})", flush=True)
        else:
            for drop in (0.0, 0.30):
                for kind in ("random40", "grid40"):
                    for p in ps:
                        total, times, hops = Counter(), [], []
                        for c, t, h in pool.map(awake, [(kind, sd, drop, p) for sd in range(seeds)]):
                            total.update(c); times += t; hops += h
                        times.sort()
                        n = sum(v for k, v in total.items() if k != "tx")
                        q = lambda x: times[min(len(times) - 1, int(x * len(times)))] if times else float("nan")
                        print(f"awake {kind:8} loss {drop:.0%} p={p:.2f}: direct {total['done']}/{n} unconf {total['unconfirmed/q']} notrun {total['not_run/q']} p50 {q(.5):.2f} p95 {q(.95):.2f} tx/cmd {total['tx']/n:.1f} hops {sum(hops)/max(1,len(hops)):.2f}", flush=True)
