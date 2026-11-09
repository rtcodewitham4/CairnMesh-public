"""Does the link test's verdict match the real cause? random40 / grid40, awake network."""
import sys
from collections import Counter
from concurrent.futures import ProcessPoolExecutor

from cairnmesh import robustness_bench as rb, session_bench as sb


def one(args):
    kind, seed, case = args
    drop = 0.30 if case == "loss30" else 0.0
    clock, network, controller, targets, where = rb._setup(kind, seed, drop)
    if case == "weak9":
        network.medium.radio_range *= 10 ** (-9.0 / (10 * network.medium.signal.exponent))
    gw = network.sessions
    out = []
    for target in targets[:2]:
        test = gw.test_link(target, rounds=20)
        sb.run(clock, network, controller, 400.0, lambda: test.finished is not None)
        w = test.weakest
        out.append(dict(verdict=test.verdict, loss=test.loss, breaks=test.breaks, weakest=w and w[2],
                        p50=test.rtt(0.5), p95=test.rtt(0.95), worst=test.worst, stopped=test.stopped,
                        took=(test.finished or clock.now) - test.started))
        gw.close_session(target)
    return out


if __name__ == "__main__":
    seeds = int(sys.argv[1])
    for kind in ("random40", "grid40"):
        for case in ("clean", "loss30", "weak9"):
            with ProcessPoolExecutor() as pool:
                rows = [r for rr in pool.map(one, [(kind, s, case) for s in range(seeds)]) for r in rr]
            v = Counter(r["verdict"] for r in rows)
            loss = sum(r["loss"] or 0 for r in rows) / len(rows)
            took = sorted(r["took"] for r in rows)[len(rows) // 2]
            stopped = sum(bool(r["stopped"]) for r in rows)
            thin = 0
            over = sum(1 for r in rows if r["p95"] and r["p95"] > r["worst"])
            print(f"{kind:8} {case:6} good_but_thin={thin} p95_over_worst={over}", end=" ")
            print(f"{kind:6} {case:6} n={len(rows)} {dict(v)} mean_loss={loss:.2f} median_took={took:.0f}s stopped={stopped}", flush=True)
