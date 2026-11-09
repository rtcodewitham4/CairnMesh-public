"""Why do dormant random40 commands end UNCONFIRMED? Per-command trace + counterfactuals."""
import sys, json
from collections import Counter
from concurrent.futures import ProcessPoolExecutor

from cairnmesh import stress_bench as s, session_bench as sb, robustness_bench as rb
from cairnmesh import wake_bench as wb
from cairnmesh.device import Device
from cairnmesh.controller import DEFAULT_CONFIRM_WINDOW
from cairnmesh.frames import QUERY_ACTION


def one(args):
    kind, seed, drop, gw_extra, confirm, wait, ctl = args
    seen = []  # (time, node, kind, id)
    orig = Device._handle_request

    def handle(self, frame):
        now = self.clock()
        if frame.action == QUERY_ACTION:
            seen.append((now, self.device_id, "q", frame.epoch << 8 | frame.value))
        else:
            seen.append((now, self.device_id, "r", frame.message_id))
        return orig(self, frame)

    Device._handle_request = handle
    rb.COMMAND_WAIT = wait
    rng, clock, network, _ = wb._build(kind, seed, {"idle_timeout": 60.0, "lease": 15, **gw_extra})
    network.medium.drop = drop
    if ctl.get("_blind"):
        gwx = network.sessions
        for r in list(network.relays.values()) + [a.relay for a in gwx._arms]:
            r.last_hop_ack = False
        gwx.model.last_hop_ack = False
        gwx._models.clear()
    if ctl.get("_noise"):
        # every link loses _noise dB: shrink the reference range (median RSSI scales with log(distance / range))
        network.medium.radio_range *= 10 ** (-ctl["_noise"] / (10 * network.medium.signal.exponent))
    controller = network.controller(network.sessions, clock=clock, timeout=8.0, max_retries=2, confirm_window=confirm, **{k: v for k, v in ctl.items() if not k.startswith('_')})
    network.sessions.attach(controller)
    ledger = s.Ledger(network)
    gw = network.sessions
    depth = wb._depths(network.medium, network.controller_id)
    far = sorted(n for n, d in depth.items() if n in network.devices and d >= 2)
    rows = []
    for target in rng.sample(far, min(2, len(far))):
        sb.opened(clock, network, controller, target, 60.0)
        for value in range(6):
            t0 = clock.now
            h0 = len(gw.history)
            sess = gw.sessions.get(target)
            state0 = getattr(sess, "state", None)
            if ctl.get("_tui"):
                gw.open_session(target)  # like the TUI's ensure_session
            ledger.command(clock, network, controller, target, value)
            node, ident, status, result, queried, elapsed = ledger.outcomes[-1]
            ev = Counter(e for _, e, n in gw.history[h0:] if n in (target, 0xFF))
            reached = [x for x in seen if x[1] == target and x[2] == "r" and x[3] == ident and x[0] >= t0]
            queries = [x for x in seen if x[1] == target and x[2] == "q" and x[3] == ident and x[0] >= t0]
            sess = gw.sessions.get(target)
            rows.append(dict(
                seed=seed, target=target, depth=depth[target], status=status, queried=queried,
                reached=len(reached), first_reach=(reached[0][0] - t0) if reached else None,
                queries_heard=len(queries), state0=str(state0),
                opens=ev.get("open", 0), probes=ev.get("probe", 0) + ev.get("detour", 0), wakes=ev.get("wake", 0),
                sf=getattr(sess, "sf", None),
            ))
        network.sessions.close_session(target)
    Device._handle_request = orig
    return rows, dict(network.medium.stats)


VARIANTS = {
    "base": (0.30, {}, DEFAULT_CONFIRM_WINDOW, 100.0, {}),
    "drop0": (0.0, {}, DEFAULT_CONFIRM_WINDOW, 100.0, {}),
    "slow": (0.30, {"max_sf": 12, "sf_margin": 8.0}, DEFAULT_CONFIRM_WINDOW, 100.0, {"max_retry_window": 100.0}),
    "longer": (0.30, {}, 115.0, 125.0, {}),
    "n6": (0.0, {}, DEFAULT_CONFIRM_WINDOW, 130.0, {"_tui": True, "_noise": 6.0}),
    "n6_sf12": (0.0, {"max_sf": 12}, DEFAULT_CONFIRM_WINDOW, 130.0, {"_tui": True, "_noise": 6.0, "max_retry_window": 100.0}),
    "n9": (0.0, {}, DEFAULT_CONFIRM_WINDOW, 130.0, {"_tui": True, "_noise": 9.0}),
    "n9_sf12": (0.0, {"max_sf": 12}, DEFAULT_CONFIRM_WINDOW, 130.0, {"_tui": True, "_noise": 9.0, "max_retry_window": 100.0}),
    "tui": (0.30, {}, DEFAULT_CONFIRM_WINDOW, 100.0, {"_tui": True}),
    "s_off": (0.30, {"silent_reprobe": None}, DEFAULT_CONFIRM_WINDOW, 100.0, {"_tui": True}),
    "s_1": (0.30, {"silent_reprobe": 1}, DEFAULT_CONFIRM_WINDOW, 100.0, {"_tui": True}),
    "s_2": (0.30, {"silent_reprobe": 2}, DEFAULT_CONFIRM_WINDOW, 100.0, {"_tui": True}),
    "tui_blind": (0.30, {}, DEFAULT_CONFIRM_WINDOW, 100.0, {"_tui": True, "_blind": True}),
    "tui_longer": (0.30, {}, 115.0, 125.0, {"_tui": True}),
    "tui_slow": (0.30, {"max_sf": 12, "sf_margin": 8.0}, DEFAULT_CONFIRM_WINDOW, 130.0, {"_tui": True, "max_retry_window": 100.0}),
}

if __name__ == "__main__":
    kind = sys.argv[1]
    seeds = int(sys.argv[2])
    names = sys.argv[3:] or list(VARIANTS)
    out = {}
    with ProcessPoolExecutor() as pool:
        for name in names:
            drop, gw, confirm, wait, ctl = VARIANTS[name]
            res = list(pool.map(one, [(kind, seed, drop, gw, confirm, wait, ctl) for seed in range(seeds)]))
            rows = [r for rr, _ in res for r in rr]
            stats = Counter()
            for _, st in res:
                stats.update(st)
            out[name] = dict(rows=rows, stats=dict(stats))
            c = Counter((r["status"], r["queried"]) for r in rows)
            print(name, len(rows), dict(c), flush=True)
    json.dump(out, open(f"/tmp/cairnmesh-experiments/{kind}_{'_'.join(names)}.json", "w"))
