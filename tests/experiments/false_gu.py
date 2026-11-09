"""Inbound give-ups: did the next node on the route actually get the frame (a lost acknowledgement) or not (a real loss)?"""
import sys
from collections import Counter
from concurrent.futures import ProcessPoolExecutor

from cairnmesh import stress_bench as s, session_bench as sb, wake_bench as wb
from cairnmesh.controller import DEFAULT_CONFIRM_WINDOW
from cairnmesh.mesh import MeshRelay, _INBOUND, _kind
from cairnmesh.secure import open_sealed


def one(args):
    kind, seed, drop = args
    gave = []  # (time, plain-key, next node)
    orig = MeshRelay._route_broken

    def broken(self, flight):
        plain, _ = open_sealed(self._aead, flight.raw)
        if _kind(plain) in _INBOUND and flight.index:
            gave.append((self.clock(), (_kind(plain), plain[1], plain[2], plain[3], plain[4]), flight.route[flight.index + 1]))
        return orig(self, flight)

    MeshRelay._route_broken = broken
    rng, clock, network, _ = wb._build(kind, seed, {"idle_timeout": 60.0, "lease": 15})
    network.medium.drop = drop
    controller = network.controller(network.sessions, clock=clock, timeout=8.0, max_retries=2, confirm_window=DEFAULT_CONFIRM_WINDOW)
    network.sessions.attach(controller)
    ledger = s.Ledger(network)
    gw = network.sessions
    seen = {}  # (node, key) -> True once the node logged the frame
    for node, relay in list(network.relays.items()) + [(network.controller_id, a.relay) for a in gw._arms]:
        orig_seen = relay._log_seen
        def logged(plain, route, index, rssi, tx=None, relay=relay, orig_seen=orig_seen, node=node):
            key = orig_seen(plain, route, index, rssi, tx)
            seen[(node, key)] = True
            return key
        relay._log_seen = logged
    depth = wb._depths(network.medium, network.controller_id)
    far = sorted(n for n, d in depth.items() if n in network.devices and d >= 2)
    for target in rng.sample(far, min(2, len(far))):
        sb.opened(clock, network, controller, target, 60.0)
        for value in range(6):
            gw.open_session(target)
            ledger.command(clock, network, controller, target, value)
        gw.close_session(target)
    MeshRelay._route_broken = orig
    out = Counter()
    for t, key, nxt in gave:
        out[f"kind{key[0]}_{'false(ack lost)' if (nxt, key) in seen else 'real'}"] += 1
    return out


if __name__ == "__main__":
    seeds = int(sys.argv[1])
    for kind in ("random", "grid"):
        total = Counter()
        with ProcessPoolExecutor() as pool:
            for c in pool.map(one, [(kind, sd, 0.30) for sd in range(seeds)]):
                total.update(c)
        print(kind, dict(sorted(total.items())), flush=True)
