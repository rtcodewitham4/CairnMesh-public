"""Inbound give-ups: how often, and how long until the gateway notices the path is broken. Dormant network, TUI usage."""
import sys
from collections import Counter
from concurrent.futures import ProcessPoolExecutor

from cairnmesh import stress_bench as s, session_bench as sb, wake_bench as wb
from cairnmesh.controller import DEFAULT_CONFIRM_WINDOW
from cairnmesh.mesh import MeshRelay, _INBOUND, _kind
from cairnmesh.secure import open_sealed


def one(args):
    kind, seed, drop = args
    events = []  # (time, node, kind, gateway-ward hops left)
    orig = MeshRelay._route_broken

    def broken(self, flight):
        plain, _ = open_sealed(self._aead, flight.raw)
        if _kind(plain) in _INBOUND and flight.index:
            events.append((self.clock(), self.node_id, _kind(plain), len(flight.route) - 1 - flight.index))
        return orig(self, flight)

    MeshRelay._route_broken = broken
    rng, clock, network, _ = wb._build(kind, seed, {"idle_timeout": 60.0, "lease": 15})
    network.medium.drop = drop
    controller = network.controller(network.sessions, clock=clock, timeout=8.0, max_retries=2, confirm_window=DEFAULT_CONFIRM_WINDOW)
    network.sessions.attach(controller)
    ledger = s.Ledger(network)
    gw = network.sessions
    depth = wb._depths(network.medium, network.controller_id)
    far = sorted(n for n, d in depth.items() if n in network.devices and d >= 2)
    for target in rng.sample(far, min(2, len(far))):
        sb.opened(clock, network, controller, target, 60.0)
        for value in range(6):
            gw.open_session(target)
            ledger.command(clock, network, controller, target, value)
        gw.close_session(target)
    MeshRelay._route_broken = orig
    breaks = [t for t, what, n in gw.history if what.startswith("broken")]
    out = Counter()
    for t, node, k, left in events:
        out[f"giveup_kind{k}"] += 1
        later = [b - t for b in breaks if b >= t]
        delay = later[0] if later else None
        out["noticed<5s" if delay is not None and delay < 5 else ("noticed<30s" if delay is not None and delay < 30 else "noticed_late_or_never")] += 1
    out["commands"] = len(ledger.outcomes)
    out["unconfirmed"] = sum(1 for o in ledger.outcomes if o[2] == "unconfirmed")
    return out


if __name__ == "__main__":
    seeds = int(sys.argv[1])
    for kind in ("random", "grid"):
        total = Counter()
        with ProcessPoolExecutor() as pool:
            for c in pool.map(one, [(kind, sd, 0.30) for sd in range(seeds)]):
                total.update(c)
        print(kind, dict(sorted(total.items())), flush=True)
