"""Acceptance end to end (Gateway.accept): accepted nodes under 30 % loss, and rejected ones after relays added where it says."""
import sys, json, math, random
from concurrent.futures import ProcessPoolExecutor

from cairnmesh import stress_bench as s, session_bench as sb, wake_bench as wb
from cairnmesh.airtime import airtime_for
from cairnmesh.controller import DEFAULT_CONFIRM_WINDOW


def build(kind, seed, extra_nodes):
    from cairnmesh.mesh_sim import MeshNetwork
    rng = random.Random(seed)
    positions, extra = wb._layout(kind, rng)
    near = positions[rng.choice(sorted(positions))]
    angle, reach = rng.uniform(0, 2 * math.pi), rng.uniform(30.0, 60.0)
    entry = (near[0] + reach * math.cos(angle), near[1] + reach * math.sin(angle))
    positions = {**positions, **extra_nodes}
    clock = wb._Clock()
    network = MeshNetwork(
        positions, key=wb.KEY, clock=clock, airtime=airtime_for(7), seed=seed, hello_interval=30.0,
        controller_position=entry, wake_options={"period": wb.PERIOD, "dormant": False},
        relay_options=dict(wb.RELAY), gateway_options={"idle_timeout": 60.0, "lease": 15},
        discovery_options={}, **extra,
    )
    for agent in network.wakers.values():
        agent.lease_until = wb.WARM
    wb._run(network, clock, wb.WARM + 5.0)
    controller = network.controller(network.sessions, clock=clock, timeout=8.0, max_retries=2, confirm_window=DEFAULT_CONFIRM_WINDOW)
    network.sessions.attach(controller)
    return rng, clock, network, controller, entry, positions


def trial(kind, seed, target, extra_nodes):
    rng, clock, network, controller, entry, positions = build(kind, seed, extra_nodes)
    gw = network.sessions
    gw.scan()
    sb.run(clock, network, controller, 300.0, lambda: gw.scan_state is None)
    network.medium.drop = 0.0
    test = gw.accept(target)
    sb.run(clock, network, controller, 400.0, lambda: target in gw.acceptance and gw.acceptance[target].test is test)
    acc = gw.acceptance.get(target)
    failing = [(h.sender, h.receiver) for h in acc.hops if not h.ok] if acc else []
    network.medium.drop = 0.30
    ledger = s.Ledger(network)
    for value in range(6):
        gw.open_session(target)
        ledger.command(clock, network, controller, target, value)
    statuses = [o[2] for o in ledger.outcomes]
    where = lambda n: entry if n == network.controller_id else positions[n]
    # The installer: tries spots around the middle, keeps the one reading best to both ends
    # (here the median link from the link model, as a site survey would show it).
    from cairnmesh.radio_model import sensitivity_dbm
    medium = network.medium
    floor = sensitivity_dbm(7, medium.signal.noise_floor)
    def survey(a, b, ids):
        (ax, ay), (bx, by) = where(a), where(b)
        spots = [(t, dx, dy) for t in (0.35, 0.5, 0.65)
                 for dx, dy in ((0, 0), (25, 0), (-25, 0), (0, 25), (0, -25), (18, 18), (-18, -18), (18, -18), (-18, 18))]
        best = (-1e9, None, None)
        for (t, dx, dy), node in zip(spots, ids):  # each spot its own terrain: its own id draws its own shadowing
            x, y = ax + t * (bx - ax) + dx, ay + t * (by - ay) + dy
            score = min(
                medium.signal.median_rssi(math.dist((x, y), (ax, ay)), medium.radio_range, node, a) - floor,
                medium.signal.median_rssi(math.dist((x, y), (bx, by)), medium.radio_range, node, b) - floor,
            )
            best = max(best, (score, node, (x, y)), key=lambda item: item[0])
        return best
    used = set(positions)
    free = [n for n in range(101, 250) if n not in used and n != network.controller_id]
    adds, reads = {}, []
    for k, (a, b) in enumerate(failing):
        read, node, spot = survey(a, b, free[k * 27:(k + 1) * 27])
        adds[node] = spot; reads.append(read)
    return dict(passed=bool(acc and acc.passed), reasons=list(acc.reasons) if acc else ["none"], statuses=statuses, adds=adds, reads=reads,
                hops=len(acc.path) - 1 if acc and acc.path else None)


def targets_of(kind, seed):
    rng, clock, network, controller, entry, positions = build(kind, seed, {})
    depth = wb._depths(network.medium, network.controller_id)
    far = sorted(n for n, d in depth.items() if n in network.devices and d >= 2)
    return rng.sample(far, min(2, len(far)))


def one(args):
    kind, seed = args
    out = []
    for target in targets_of(kind, seed):
        first = trial(kind, seed, target, {})
        row = dict(seed=seed, target=target, first=first, rounds=[])
        extra, result = {}, first
        k = 0
        while not result["passed"] and result["adds"] and k < 3:
            extra.update({int(n): p for n, p in result["adds"].items()})
            result = trial(kind, seed, target, extra)
            row["rounds"].append(dict(added=len(extra), reads=[round(x, 1) for x in result["reads"]], passed=result["passed"], statuses=result["statuses"], reasons=result["reasons"]))
            k += 1
        out.append(row)
    return out


if __name__ == "__main__":
    seeds = int(sys.argv[1]); first = int(sys.argv[2])
    res = {}
    for kind in ("random", "grid"):
        with ProcessPoolExecutor() as pool:
            res[kind] = [r for rr in pool.map(one, [(kind, sd) for sd in range(first, first + seeds)]) for r in rr]
        print(kind, "done", flush=True)
    json.dump(res, open(f"/tmp/cairnmesh-experiments/acceptval_{first}_{seeds}.json", "w"))
