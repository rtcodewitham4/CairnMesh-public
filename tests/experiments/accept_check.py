"""Does a clean-air link test (acceptance) predict which nodes end UNCONFIRMED under 30 % loss? Dormant network."""
import sys, json
from concurrent.futures import ProcessPoolExecutor

from cairnmesh import stress_bench as s, session_bench as sb
from cairnmesh import wake_bench as wb
from cairnmesh.controller import DEFAULT_CONFIRM_WINDOW


def one(args):
    kind, seed, drop = args
    rng, clock, network, _ = wb._build(kind, seed, {"idle_timeout": 60.0, "lease": 15})
    controller = network.controller(network.sessions, clock=clock, timeout=8.0, max_retries=2, confirm_window=DEFAULT_CONFIRM_WINDOW)
    network.sessions.attach(controller)
    ledger = s.Ledger(network)
    gw = network.sessions
    depth = wb._depths(network.medium, network.controller_id)
    far = sorted(n for n, d in depth.items() if n in network.devices and d >= 2)
    rows = []
    for target in rng.sample(far, min(2, len(far))):
        network.medium.drop = 0.0
        test = gw.test_link(target, rounds=20)
        sb.run(clock, network, controller, 300.0, lambda: test.finished is not None)
        acc = dict(verdict=test.verdict, loss=test.loss, breaks=test.breaks, margin=test.session_margin,
                   sf=test.sf, p95=test.rtt(0.95), worst=test.worst, hops=len(test.margins), stopped=test.stopped)
        network.medium.drop = drop
        start = len(ledger.outcomes)
        for value in range(6):
            gw.open_session(target)  # like the TUI
            ledger.command(clock, network, controller, target, value)
        outs = ledger.outcomes[start:]
        rows.append(dict(seed=seed, target=target, acc=acc,
                         statuses=[o[2] for o in outs], direct=sum(1 for o in outs if o[2] == "done" and not o[4])))
        gw.close_session(target)
    return rows


if __name__ == "__main__":
    seeds = int(sys.argv[1]); drop = float(sys.argv[2])
    out = {}
    for kind in ("random", "grid"):
        with ProcessPoolExecutor() as pool:
            out[kind] = [r for rr in pool.map(one, [(kind, sd, drop) for sd in range(seeds)]) for r in rr]
        print(kind, "done", flush=True)
    json.dump(out, open(f"/tmp/cairnmesh-experiments/accept_{seeds}_{drop}.json", "w"))
