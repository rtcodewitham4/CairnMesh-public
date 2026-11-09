"""Per target: path features at open (hops, weakest margin, skip links) and its 6 commands' outcomes. Dormant, 30 % loss, TUI usage."""
import sys, json
from concurrent.futures import ProcessPoolExecutor

from cairnmesh import stress_bench as s, session_bench as sb, wake_bench as wb
from cairnmesh.controller import DEFAULT_CONFIRM_WINDOW
from cairnmesh.radio_model import sensitivity_dbm


def margin(medium, a, b):
    return medium.median_rssi(a, b) - sensitivity_dbm(7, medium.signal.noise_floor)


def one(args):
    kind, seed, drop = args
    rng, clock, network, _ = wb._build(kind, seed, {"idle_timeout": 60.0, "lease": 15})
    controller = network.controller(network.sessions, clock=clock, timeout=8.0, max_retries=2, confirm_window=DEFAULT_CONFIRM_WINDOW)
    network.sessions.attach(controller)
    ledger = s.Ledger(network)
    gw = network.sessions
    medium = network.medium
    depth = wb._depths(medium, network.controller_id)
    far = sorted(n for n, d in depth.items() if n in network.devices and d >= 2)
    rows = []
    for target in rng.sample(far, min(2, len(far))):
        medium.drop = 0.0
        session, _ = sb.opened(clock, network, controller, target, 60.0)
        path = (network.controller_id, *session.route, target) if session.state == "open" else None
        medium.drop = drop
        start = len(ledger.outcomes)
        for value in range(6):
            gw.open_session(target)
            ledger.command(clock, network, controller, target, value)
        statuses = [o[2] for o in ledger.outcomes[start:]]
        feat = dict(seed=seed, target=target, depth=depth[target], statuses=statuses, opened=path is not None)
        if path:
            hops = len(path) - 1
            margins = [margin(medium, a, b) for a, b in zip(path, path[1:])]
            # skip links: node i+2 hears node i with a usable median margin (>= 0 dB at SF7)
            skips = [margin(medium, path[i], path[i + 2]) >= 0.0 for i in range(hops - 1)]
            # hops without any cover: neither skipped over from before nor able to be skipped
            uncovered = 0
            for i in range(hops):
                covered = (i >= 1 and skips[i - 1]) or (i + 1 < hops and skips[i])
                uncovered += not covered
            nodes = list(network.devices) + [network.controller_id]
            detours = []
            for a, b in zip(path, path[1:]):
                detours.append(sum(1 for c in nodes if c not in (a, b) and margin(medium, a, c) >= 4.0 and margin(medium, c, b) >= 4.0))
            feat.update(hops=hops, min_margin=min(margins), margins=margins, detours=detours, skips=sum(skips), uncovered=uncovered, sf=session.sf)
        rows.append(feat)
        gw.close_session(target)
    return rows


if __name__ == "__main__":
    seeds = int(sys.argv[1]); first = int(sys.argv[2]) if len(sys.argv) > 2 else 0
    out = {}
    for kind in ("random", "grid"):
        with ProcessPoolExecutor() as pool:
            out[kind] = [r for rr in pool.map(one, [(kind, sd, 0.30) for sd in range(first, first + seeds)]) for r in rr]
        print(kind, "done", flush=True)
    json.dump(out, open(f"/tmp/cairnmesh-experiments/features_{first}_{seeds}.json", "w"))
