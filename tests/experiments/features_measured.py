"""The acceptance rule computed from what the gateway measured (scan book + probe links), against outcomes. Dormant, 30 % loss."""
import sys, json
from concurrent.futures import ProcessPoolExecutor

from cairnmesh import stress_bench as s, session_bench as sb, wake_bench as wb
from cairnmesh.controller import DEFAULT_CONFIRM_WINDOW
from cairnmesh.radio_model import sensitivity_dbm

FLOOR = 7.5  # SNR limit at SF7 is -7.5 dB: margin = snr + 7.5


def true_margin(medium, a, b):
    return medium.median_rssi(a, b) - sensitivity_dbm(7, medium.signal.noise_floor)


def graph(gw):
    edges = {}
    def put(a, b, m):
        key = (min(a, b), max(a, b))
        edges[key] = min(edges.get(key, m), m)
    for node, known in gw.nodes.items():
        for hop in known.neighbours:
            put(node, hop.node_id, hop.link.snr_db + FLOOR)
        path = (gw.controller_id, *known.route, node)
        if known.links and len(known.links) == len(path) - 1:
            for (a, b), link in zip(zip(path, path[1:]), known.links):
                put(a, b, link.snr_db + FLOOR)
    return edges


def one(args):
    kind, seed, drop = args
    rng, clock, network, controller0 = wb._build(kind, seed, {"idle_timeout": 60.0, "lease": 15}, discovery=True)
    gw = network.sessions
    controller = network.controller(gw, clock=clock, timeout=8.0, max_retries=2, confirm_window=DEFAULT_CONFIRM_WINDOW)
    gw.attach(controller)
    ledger = s.Ledger(network)
    medium = network.medium
    gw.scan()
    sb.run(clock, network, controller, 300.0, lambda: gw.scan_state is None)
    depth = wb._depths(medium, network.controller_id)
    far = sorted(n for n, d in depth.items() if n in network.devices and d >= 2)
    rows = []
    for target in rng.sample(far, min(2, len(far))):
        medium.drop = 0.0
        session, _ = sb.opened(clock, network, controller, target, 60.0)
        feat = dict(seed=seed, target=target, opened=session.state == "open", book=len(gw.nodes))
        if feat["opened"]:
            path = (network.controller_id, *session.route, target)
            edges = graph(gw)
            for (a, b), link in zip(zip(path, path[1:]), session.links):
                key = (min(a, b), max(a, b)); edges[key] = link.snr_db + FLOOR  # the fresh probe wins
            e = lambda a, b: edges.get((min(a, b), max(a, b)), -99.0)
            nodes = set(n for k in edges for n in k)
            margins = [link.snr_db + FLOOR for link in session.links]
            detours = [sum(1 for c in nodes if c not in (a, b) and e(a, c) >= 4.0 and e(c, b) >= 4.0) for a, b in zip(path, path[1:])]
            tmargins = [true_margin(medium, a, b) for a, b in zip(path, path[1:])]
            tnodes = list(network.devices) + [network.controller_id]
            tdetours = [sum(1 for c in tnodes if c not in (a, b) and true_margin(medium, a, c) >= 4.0 and true_margin(medium, c, b) >= 4.0) for a, b in zip(path, path[1:])]
            feat.update(hops=len(path) - 1, margins=margins, detours=detours, tmargins=tmargins, tdetours=tdetours)
        medium.drop = drop
        start = len(ledger.outcomes)
        for value in range(6):
            gw.open_session(target)
            ledger.command(clock, network, controller, target, value)
        feat["statuses"] = [o[2] for o in ledger.outcomes[start:]]
        rows.append(feat)
        gw.close_session(target)
    return rows


if __name__ == "__main__":
    seeds = int(sys.argv[1]); first = int(sys.argv[2])
    out = {}
    for kind in ("random", "grid"):
        with ProcessPoolExecutor() as pool:
            out[kind] = [r for rr in pool.map(one, [(kind, sd, 0.30) for sd in range(first, first + seeds)]) for r in rr]
        print(kind, "done", flush=True)
    json.dump(out, open(f"/tmp/cairnmesh-experiments/measured_{first}_{seeds}.json", "w"))
