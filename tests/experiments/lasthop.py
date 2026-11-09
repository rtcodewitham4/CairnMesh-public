"""The last relay's own record of each REPLY into the gateway: sends, acked by the echo / gave up, and did the gateway get it."""
import sys
from collections import Counter
from concurrent.futures import ProcessPoolExecutor

from cairnmesh import robustness_bench as rb, session_bench as sb
from cairnmesh.frames import FrameKind


def one(args):
    kind, seed, drop, ack = args
    clock, network, controller, targets, where = rb._setup(kind, seed, drop)
    gw = network.sessions
    if not ack:
        for r in list(network.relays.values()) + [a.relay for a in gw._arms]:
            r.last_hop_ack = False
        gw.model.last_hop_ack = False
        gw._models.clear()
    got, asked = set(), set()
    orig_send = gw.send
    def send(data):
        plain = bytes(data)
        if plain and plain[0] == FrameKind.REQUEST:
            asked.add((plain[2], plain[3]))
        return orig_send(data)
    gw.send = send
    orig = gw._deliver
    def deliver(plain):
        if plain and plain[0] == FrameKind.REPLY:
            got.add((plain[1], plain[3]))
        return orig(plain)
    gw._deliver = deliver
    out = Counter()
    for target in targets[:2]:
        sb.opened(clock, network, controller, target, 60.0)
        for value in range(8):
            gw.open_session(target)
            future = gw.request(target, 3, value)
            sb.run(clock, network, controller, rb.COMMAND_WAIT, future.done)
            sb.run(clock, network, controller, 0.3)
        gw.close_session(target)
    records = network.route_records()
    G = network.controller_id
    for node, log in records.items():
        if node == G:
            continue
        for key, rec in log.items():
            if rec.kind != FrameKind.REPLY or rec.index != len(rec.route) - 2 or rec.target != G:
                continue
            if (rec.sender, rec.sequence) not in asked:
                continue  # a keepalive's answer: the gateway keeps those to itself
            arrived = (rec.sender, rec.sequence) in got
            out[f"{rec.state}/{'got' if arrived else 'lost'}"] += 1
            out[f"sends{min(rec.sends, 5)}"] += 1
    out["echoed"] = sum(a.relay.stats["echoed"] for a in gw._arms)
    return out


if __name__ == "__main__":
    seeds = int(sys.argv[1]); drop = float(sys.argv[2]); ack = sys.argv[3] == "on"
    for kind in ("random40", "grid40"):
        total = Counter()
        with ProcessPoolExecutor() as pool:
            for c in pool.map(one, [(kind, s, drop, ack) for s in range(seeds)]):
                total.update(c)
        print(kind, dict(sorted(total.items())), flush=True)
