"""Per controller transmission: lost outbound, lost inbound (and on the last hop into the gateway), or answered. 30 % loss."""
import sys
from collections import Counter
from concurrent.futures import ProcessPoolExecutor

from cairnmesh import robustness_bench as rb, session_bench as sb
from cairnmesh.device import Device
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
    sends, heard, replies_in = [], [], []  # (t, target, seq) ; device handled ; gateway got reply
    orig_send = gw.send

    def send(data):
        plain = bytes(data)
        if plain and plain[0] == FrameKind.REQUEST:
            sends.append((clock.now, plain[2], plain[3]))
        return orig_send(data)

    gw.send = send
    for node, device in network.devices.items():
        orig = device._handle_request

        def handle(frame, orig=orig, node=node):
            heard.append((clock.now, node, frame.sequence))
            return orig(frame)

        device._handle_request = handle
    orig_deliver = gw._deliver

    def deliver(plain):
        if plain and plain[0] == FrameKind.REPLY:
            replies_in.append((clock.now, plain[1], plain[3]))
        return orig_deliver(plain)

    gw._deliver = deliver
    for target in targets[:2]:
        sb.opened(clock, network, controller, target, 60.0)
        for value in range(8):
            gw.open_session(target)
            future = gw.request(target, 3, value)
            sb.run(clock, network, controller, rb.COMMAND_WAIT, future.done)
            sb.run(clock, network, controller, 0.3)
        gw.close_session(target)
    log = network.medium.log
    gid = network.controller_id
    near = set(network.medium.neighbours(gid))
    out = Counter()
    for k, (t, node, seq) in enumerate(heard):
        nxt = min([t2 for t2, n2, s2 in heard[k + 1:] if n2 == node and s2 == seq] + [t + 15.0])
        if any(t <= tr <= nxt and n == node and s2 == seq for tr, n, s2 in replies_in):
            out["reply_back"] += 1
            continue
        out["reply_lost"] += 1
        last = any(t <= ts <= nxt and data and (data[0] & 0x1F) == FrameKind.REPLY and data[1] == node
                   and data[3] == seq and sender in near and sender != node for ts, sender, data in log)
        direct = node in near
        out["lost_last_hop" if last else ("lost_target_next_to_gw" if direct else "lost_earlier")] += 1
    mine = [(ts, data[2], data[3]) for ts, sender, data in log
            if sender == gid and data and (data[0] & 0x1F) == FrameKind.REQUEST]
    groups = []
    for ts, node, seq in mine:
        if groups and groups[-1][1] == node and groups[-1][2] == seq and ts - groups[-1][3] <= 3.0:
            groups[-1][3] = ts
        else:
            groups.append([ts, node, seq, ts])
    for start, node, seq, end in groups:
        got = any(start <= th <= end + 15.0 and n == node and s2 == seq for th, n, s2 in heard)
        out["out_ok" if got else "out_lost"] += 1
    return out


if __name__ == "__main__":
    seeds = int(sys.argv[1]); drop = float(sys.argv[2]); ack = sys.argv[3] == "on"
    for kind in ("random40", "grid40"):
        total = Counter()
        with ProcessPoolExecutor() as pool:
            for c in pool.map(one, [(kind, s, drop, ack) for s in range(seeds)]):
                total.update(c)
        n = total["reply_back"] + total["reply_lost"]
        m = total["out_ok"] + total["out_lost"]
        print(kind, "gateway sends", m, f"outbound lost {total['out_lost']/max(1,m):.1%}", end=" | ")
        print(kind, "device answers", n, dict(total), {k: f"{total[k]/n:.1%}" for k in ("reply_lost", "lost_last_hop", "lost_earlier")}, flush=True)
