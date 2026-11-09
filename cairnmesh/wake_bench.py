"""Wake-up trade-offs (task D).

    python -m cairnmesh.wake_bench theory          # D6 by formula: sampling period vs current, life, wake time
    python -m cairnmesh.wake_bench life            # D6: lifetime by use pattern and LDO quiescent current
    python -m cairnmesh.wake_bench net [seeds]     # D2: network wake in the simulator vs the model
    python -m cairnmesh.wake_bench probe [seeds]   # D3: a session to one node by a directed wake probe
    python -m cairnmesh.wake_bench entry [seeds]   # D4: 1 / 3 / 6 nodes hear the controller; vs the queueing model
    python -m cairnmesh.wake_bench d5 [seeds]      # D5: scan, reboots, session, trace, a moving controller
    python -m cairnmesh.wake_bench c5 [seeds]      # C5: a relay dies mid-session; detour vs probe to everyone

``net``: 40 devices, unplanned (random in 550 m × 550 m) and planned (a
5 × 8 grid, 80 m apart, 10 dB margin per link); the controller walks up to a
random device (30–60 m away). The network first runs awake for 150 s with
HELLOs every 30 s (so relays know their neighbours, as after installation),
falls asleep when that lease ends, and is then woken with a 60 s lease.

Every number rests on ``energy.RadioCurrents`` / ``energy.Sniffer``
defaults, which are estimates not yet checked against the datasheet or a
measurement. The battery (three FR6 in series behind an
LDO, counted at 2500 mAh: the user's conservative figure) and the
visit rates are examples: the user has not set them yet (Q1).
"""

from __future__ import annotations

import math
import random
import sys
from collections import deque
from typing import Optional

from . import simpool
from .airtime import airtime_for, lora_airtime
from .energy import RadioCurrents, Sniffer, dormant_current, preamble_for, symbol_time, wake_charge, wake_hop_time

BATTERY_MAH = 2500.0
LEASE = 120.0
DEPTHS = (8, 17)


def theory() -> None:
    currents = RadioCurrents()
    print(f"{'侦听周期 T':>10}{'静默电流':>10}{'每次唤醒':>10}{'寿命 1 次/天':>14}{'寿命 24 次/天':>14}"
          + "".join(f"{f'唤醒 {d} 跳':>12}" for d in DEPTHS))
    for period in (0.25, 0.5, 1.0, 2.0, 4.0):
        sniffer = Sniffer(period=period)
        idle = dormant_current(sniffer, currents)
        wake = wake_charge(sniffer, LEASE, currents=currents)
        lives = []
        for per_day in (1, 24):
            mean = idle + wake * per_day / 86400
            lives.append(BATTERY_MAH * 3.6 / mean / 86400 / 365)  # mAh → C, years
        hop = wake_hop_time(sniffer)
        print(f"{period:>9.2f}s{idle * 1e6:>8.1f}µA{wake * 1000 / 3.6:>8.0f}µAh"
              f"{lives[0]:>12.1f} 年{lives[1]:>12.1f} 年" + "".join(f"{d * hop:>10.1f} s" for d in DEPTHS))


# --------------------------------------------------------------- D2 (sim)

KEY = bytes(range(16))
STEP = 0.005
WARM = 150.0
NET_LEASE = 60
#: Sampling period: 0.5 s (user, 2026-10-19).
PERIOD = 0.5
#: ``MeshRelay`` options for ``net`` / ``probe`` (``wake_backoff=3`` style arguments).
RELAY: dict = {}
#: ``Gateway`` options for ``probe`` (``gw.wake_window=5`` style arguments).
GATEWAY: dict = {}
#: ``MeshNetwork`` wake options for ``entry`` (``wk.period=0.25`` style arguments).
WAKE: dict = {}
#: Sealed WAKE_NET: 9 B header (holds the first 4 plain bytes) + the other 2 + 4 B MIC.
WAKE_BYTES = 6 - 4 + 9 + 4


def _layout(kind: str, rng: random.Random):
    from .mesh_sim import DEFAULT_SIGNAL, scatter
    from .radio_model import planned_range

    if kind == "random":
        return scatter(40, 550.0, rng), {}
    grid = {1 + row * 8 + col: (80.0 * col, 80.0 * row) for row in range(5) for col in range(8)}
    return grid, {"radio_range": planned_range(80.0, model=DEFAULT_SIGNAL)}


def _depths(medium, root: int) -> dict[int, int]:
    """Hops from ``root`` over links heard at least half the time."""

    depth, todo = {root: 0}, deque([root])
    while todo:
        node = todo.popleft()
        for other in medium.neighbours(node):
            if other not in depth:
                depth[other] = depth[node] + 1
                todo.append(other)
    return depth


def wake_once(args: tuple[str, int, dict]) -> dict:
    from .mesh_sim import MeshNetwork

    kind, seed, relay = args
    RELAY.update(relay)
    rng = random.Random(seed)
    positions, extra = _layout(kind, rng)
    near = positions[rng.choice(sorted(positions))]
    angle, reach = rng.uniform(0, 2 * math.pi), rng.uniform(30.0, 60.0)
    entry = (near[0] + reach * math.cos(angle), near[1] + reach * math.sin(angle))
    clock = _Clock()
    network = MeshNetwork(
        positions,
        key=KEY,
        clock=clock,
        airtime=airtime_for(7),
        seed=seed,
        hello_interval=30.0,
        controller_position=entry,
        wake_options={"period": PERIOD, "dormant": False},
        relay_options=dict(RELAY),
        **extra,
    )
    agents = network.wakers
    for agent in agents.values():
        agent.lease_until = WARM
    _run(network, clock, WARM + 5.0)
    asleep = sum(not agent.awake for agent in agents.values())
    meters = {node: network.medium.port(node).meter for node in positions}
    before = {node: meter.charge(clock.now) for node, meter in meters.items()}
    start = clock.now
    depth = _depths(network.medium, network.controller_id)
    model = queued_wake(network.medium, network.controller_id)
    network.waker.wake(NET_LEASE)
    _run(network, clock, start + 40.0)
    woke = {node: agent.woke_at - start for node, agent in agents.items() if (agent.woke_at or -1) >= start}
    _run(network, clock, start + NET_LEASE + 40.0)
    used = [meters[node].charge(clock.now) - before[node] for node in positions]
    idle = dormant_current(network.medium.port(1).sniffer) * (clock.now - start)
    relays = network.relays.values()
    return {
        "asleep": asleep,
        "reachable": [node for node in positions if node in depth],
        "depth": {node: d for node, d in depth.items() if node in positions},
        "woke": woke,
        "model": model,
        "awake_after": sum(agent.awake for agent in agents.values()),
        "copies": sum(relay.stats["wakes_relayed"] + relay.stats["wake_repeats"] for relay in relays),
        "lost": sum(watch.stats["lost"] for watch in network.watches.values()),
        "extra_charge": sum(used) / len(used) - idle,
        "repeats": sum(relay.stats["wake_repeats"] for relay in relays),
        "suppressed": sum(relay.stats["suppressed"] for relay in relays),
        "caller_repeats": network.relays[network.controller_id].stats["wake_repeats"],
    }


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _run(network, clock: _Clock, until: float) -> None:
    while clock.now < until:
        clock.now += STEP
        network.tick(clock.now)


def model_hop(rssi: float = -115.0) -> float:
    """Expected seconds per hop of a network wake (``MeshRelay`` defaults).

    The first neighbour to pass a copy on draws the backoff (0.05–1.0 s,
    70 % by closeness: a typical far link at ``rssi`` is near the short
    end, 30 % at random), then sends a preamble of one period and the frame.
    """

    low, high, weight = 0.05, 1.0, 0.7
    closeness = min(1.0, max(0.0, (rssi + 120.0) / 80.0))
    backoff = low + (high - low) * (weight * closeness + (1 - weight) * 0.5)
    return backoff + _wake_air()


def queued_wake(
    medium, root: int, *, backoff=(0.05, 1.0), weight: float = 0.7, sends: Optional[dict] = None
) -> dict[int, float]:
    """Expected wake time of every node, with queueing (task D4, the Q9 model).

    A deterministic walk over the links heard at least half the time: a
    node wakes when a neighbour's copy ends, passes it on after its expected
    backoff (by the RSSI of that copy), and waits while a neighbour is on
    the air, then for a mean backoff again (listen before talk). No fading,
    no collisions, one copy per node: what is left between this and the
    simulation is collisions and fading. ``sends`` collects each node's
    ``(start, end)`` on air.
    """

    import heapq

    low, high = backoff
    on_air = _wake_air()
    hears = {node: set(medium.neighbours(node)) for node in medium._ports}
    woke: dict[int, float] = {}
    sending: dict[int, tuple[float, float]] = {} if sends is None else sends
    queue: list[tuple[float, int]] = []

    def heard(sender: int, end: float) -> None:
        for other in hears[sender]:
            if other != root and other not in woke:
                woke[other] = end
                closeness = min(1.0, max(0.0, (medium.median_rssi(sender, other) + 120.0) / 80.0))
                share = weight * closeness + (1 - weight) * 0.5
                heapq.heappush(queue, (end + low + (high - low) * share, other))

    heard(root, on_air)
    while queue:
        at, node = heapq.heappop(queue)
        busy = [end for other, (start, end) in sending.items() if node in hears[other] and start <= at < end]
        if busy:
            heapq.heappush(queue, (max(busy) + (low + high) / 2, node))
            continue
        sending[node] = (at, at + on_air)
        heard(node, at + on_air)
    return woke


def _wake_air() -> float:
    preamble = preamble_for(Sniffer(period=PERIOD), 7)
    return lora_airtime(WAKE_BYTES, 7) + (preamble - 8) * symbol_time(7)


def net(seeds: int = 20) -> None:
    hop, first = model_hop(), _wake_air()
    print(f"模型：入口邻居 {first:.2f} s 后醒，之后每跳 {hop:.2f} s（退避 + 一个周期 {PERIOD:g} s 的前导码 + 帧）")
    sniffer = Sniffer(period=PERIOD)
    theory = wake_charge(sniffer, NET_LEASE, frame_air=lora_airtime(WAKE_BYTES, 7))
    for kind in ("random", "grid"):
        with simpool.pool() as pool:
            runs = list(pool.map(wake_once, [(kind, seed, RELAY) for seed in range(1, seeds + 1)]))
        reach = sum(len(run["reachable"]) for run in runs)
        missed = sum(len(set(run["reachable"]) - set(run["woke"])) for run in runs)
        extra = sum(len(set(run["woke"]) - set(run["reachable"])) for run in runs)
        by_depth: dict[int, list[tuple[float, float, float]]] = {}
        for run in runs:
            for node, at in run["woke"].items():
                if node in run["depth"] and node in run["model"]:
                    by_depth.setdefault(run["depth"][node], []).append(
                        (at, first + (run["depth"][node] - 1) * hop, run["model"][node])
                    )
        lasts = [max(run["woke"].values()) for run in runs if run["woke"]]
        deepest = [max(run["depth"][n] for n in run["woke"] if n in run["depth"]) for run in runs if run["woke"]]
        print(f"\n{'未规划随机 40' if kind == 'random' else '规划网格 40'}（{seeds} 个种子）")
        print(f"  入睡 {sum(r['asleep'] for r in runs)}/{40 * seeds}；可达 {reach}，唤醒 {reach - missed}"
              f"（{100 * (reach - missed) / reach:.1f} %），漏 {missed}；中位图之外也醒了 {extra}")
        print(f"  租约到期后仍醒着 {sum(r['awake_after'] for r in runs)}；误报失联 {sum(r['lost'] for r in runs)}；"
              f"控制端重发 {sum(r['caller_repeats'] for r in runs)} 次")
        print(f"  每次唤醒的长前导码帧 {sum(r['copies'] for r in runs) / seeds:.0f} 份（{40} 个节点；"
              f"其中重发 {sum(r['repeats'] for r in runs) / seeds:.0f}；听到 3 份而免发 {sum(r['suppressed'] for r in runs) / seeds:.1f}）")
        charge = sum(r["extra_charge"] for r in runs) / seeds
        print(f"  每节点每次唤醒多耗 {charge / 3.6 * 1000:.0f} µAh（理论 {theory / 3.6 * 1000:.0f} µAh：收 + 发一份 + 醒 {NET_LEASE} s）")
        print(f"  最后一个节点醒来：平均 {sum(lasts) / len(lasts):.1f} s，最深 {sum(deepest) / len(deepest):.1f} 跳"
              f"（模型 {first + (sum(deepest) / len(deepest) - 1) * hop:.1f} s）")
        print(f"  {'跳数':>4}{'节点':>6}{'实测平均':>10}{'逐跳公式':>10}{'偏差':>7}{'排队模型':>10}{'偏差':>7}{'各节点比值中位':>14}")
        for d in sorted(by_depth):
            rows = by_depth[d]
            mean = sum(r[0] for r in rows) / len(rows)
            formula = rows[0][1]
            queued = sum(r[2] for r in rows) / len(rows)
            ratios = sorted(r[0] / r[2] for r in rows)
            print(f"  {d:>4}{len(rows):>6}{mean:>9.2f}s{formula:>9.2f}s{100 * (mean - formula) / formula:>+6.0f}%"
                  f"{queued:>9.2f}s{100 * (mean - queued) / queued:>+6.0f}%{ratios[len(ratios) // 2]:>13.2f}")


def _build(kind: str, seed: int, gateway_options: Optional[dict] = None, discovery: bool = False):
    """A dormant network (after a 150 s awake warm-up) and the controller's entry point."""

    from .mesh_sim import MeshNetwork

    rng = random.Random(seed)
    positions, extra = _layout(kind, rng)
    near = positions[rng.choice(sorted(positions))]
    angle, reach = rng.uniform(0, 2 * math.pi), rng.uniform(30.0, 60.0)
    entry = (near[0] + reach * math.cos(angle), near[1] + reach * math.sin(angle))
    clock = _Clock()
    network = MeshNetwork(
        positions,
        key=KEY,
        clock=clock,
        airtime=airtime_for(7),
        seed=seed,
        hello_interval=30.0,
        controller_position=entry,
        wake_options={"period": PERIOD, "dormant": False},
        relay_options=dict(RELAY),
        gateway_options=gateway_options,
        discovery_options={} if discovery else None,
        **extra,
    )
    for agent in network.wakers.values():
        agent.lease_until = WARM
    _run(network, clock, WARM + 5.0)
    controller = None
    if network.sessions is not None:
        controller = network.controller(
            network.sessions, clock=clock, timeout=8.0, max_retries=2, confirm_window=None  # baselines: no Q18 query
        )
        network.sessions.attach(controller)
    return rng, clock, network, controller


def probe_once(args: tuple[str, int, dict, dict]) -> dict:
    kind, seed, relay, gateway_options = args
    RELAY.update(relay)
    GATEWAY.update(gateway_options)
    rng, clock, network, controller = _build(kind, seed, {"idle_timeout": 60.0, "lease": 15, **GATEWAY})
    depth = _depths(network.medium, network.controller_id)
    far = [node for node, d in depth.items() if node in network.devices and d >= 2]
    target = rng.choice(sorted(far))
    meters = {node: network.medium.port(node).meter for node in network.devices}
    for meter in meters.values():
        meter.charge(clock.now)  # close the warm-up
    awake0 = {node: meter.awake for node, meter in meters.items()}
    start = clock.now
    gateway = network.sessions
    session = gateway.open_session(target)

    def tick() -> None:
        clock.now += STEP
        network.tick(clock.now)
        controller.tick(clock.now)

    while clock.now < start + 60.0 and session.state != "open":
        tick()
    opened = clock.now - start if session.state == "open" else None
    commands = []
    if opened is not None:
        for value in range(5):
            future = gateway.request(target, 3, value)
            sent = clock.now
            while clock.now < sent + 30.0 and not future.done():
                tick()
            outcome = future.result(timeout=0)
            commands.append(outcome.elapsed if outcome.ok else None)
            until = clock.now + 3.0
            while clock.now < until:
                tick()
    route = set(session.route) | {target}
    model_open = gateway.model.probe(session.hops or depth[target], *gateway._probe_timing())
    model_command = gateway.model.command(session.hops).worst if session.hops else None
    used_until = clock.now
    # Idle: the session closes after ``idle_timeout``, its last keepalive's
    # lease (15 s + 5 s hold) runs out, everyone sleeps.
    while clock.now < used_until + 60.0 + 30.0:
        tick()
    for meter in meters.values():
        meter.charge(clock.now)
    awake = {node: meters[node].awake - awake0[node] for node in meters}
    return {
        "hops": session.hops or depth[target],
        "opened": opened,
        "model_open": model_open,
        "commands": commands,
        "model_command": model_command,
        "window": gateway._probe_timing()[0],
        "off_path": [awake[node] for node in awake if node not in route],
        "off_path_wake": [
            awake[node] / network.wakers[node].stats["woken"]
            for node in awake
            if node not in route and network.wakers[node].stats["woken"]
        ],
        "on_path": [awake[node] for node in route if node in awake],
        "woken": sum(agent.stats["woken"] for agent in network.wakers.values()),
        "awake_after": sum(agent.awake for agent in network.wakers.values()),
        "used": used_until - start,
        "probes": session.counts["probes"],
        "breaks": session.counts.get("broken", 0),
        "wakes_per_node": sum(agent.stats["woken"] for agent in network.wakers.values()) / len(network.wakers),
    }


def probe(seeds: int = 20) -> None:
    hold = 3.0
    print(f"侦听周期 T = {PERIOD} s")
    for kind in ("random", "grid"):
        with simpool.pool() as pool:
            runs = list(pool.map(probe_once, [(kind, seed, RELAY, GATEWAY) for seed in range(1, seeds + 1)]))
        ok = [run for run in runs if run["opened"] is not None]
        print(f"\n{'未规划随机 40' if kind == 'random' else '规划网格 40'}（{seeds} 个种子，目标为 ≥ 2 跳的随机节点）")
        print(f"  会话打开 {len(ok)}/{seeds}；平均 {sum(len(r['commands']) and r['hops'] for r in ok) / max(1, len(ok)):.1f} 跳")
        if ok:
            ratio = [r["opened"] / r["model_open"] for r in ok]
            print(f"  打开用时：平均 {sum(r['opened'] for r in ok) / len(ok):.1f} s，最长 {max(r['opened'] for r in ok):.1f} s；"
                  f"实测 / 模型上限 平均 {sum(ratio) / len(ratio):.2f}，最大 {max(ratio):.2f}")
            done = [t for r in ok for t in r["commands"] if t is not None]
            total = sum(len(r["commands"]) for r in ok)
            over = sum(t > r["model_command"] for r in ok for t in r["commands"] if t is not None)
            print(f"  打开后命令：送达 {len(done)}/{total}；平均 {sum(done) / max(1, len(done)):.2f} s，超模型最坏值 {over} 次")
        window = runs[0]["window"]
        off = [t for r in runs for t in r["off_path"]]
        each = [t for r in runs for t in r["off_path_wake"]]
        woken = sum(r["woken"] for r in runs) / seeds
        bound = window + hold + 1.0  # + its own long-preamble send
        print(f"  不在路径上的节点醒着：合计平均 {sum(off) / len(off):.1f} s；每被叫醒一次平均 {sum(each) / len(each):.1f} s，"
              f"最长 {max(each):.1f} s（窗口 {window:.1f} + 余量 {hold:.0f} + 发送 1 = {bound:.1f} s；超过的 {sum(t > bound for t in each)}/{len(each)}）")
        on = [t for r in ok for t in r["on_path"]]
        if on:
            print(f"  路径上的节点醒着：平均 {sum(on) / len(on):.1f} s（会话用了约 {sum(r['used'] for r in ok) / len(ok):.0f} s，空闲 60 s 后关闭）")
        print(f"  每次被叫醒的节点 {woken:.0f} 次（每节点 {sum(r['wakes_per_node'] for r in runs) / seeds:.1f} 次）；"
              f"每个会话探测 {sum(r['probes'] for r in runs) / seeds:.1f} 次；结束后仍醒着 {sum(r['awake_after'] for r in runs)}")


def entry_once(args: tuple[str, int, int, dict, dict]) -> Optional[dict]:
    """A network wake with the controller where exactly ``count`` nodes hear it."""

    from .mesh_sim import MeshNetwork

    kind, seed, count, relay, wake = args
    RELAY.update(relay)
    rng = random.Random(seed)
    positions, extra = _layout(kind, rng)
    clock = _Clock()
    network = MeshNetwork(
        positions,
        key=KEY,
        clock=clock,
        airtime=airtime_for(7),
        seed=seed,
        hello_interval=30.0,
        controller_position=(-1000.0, -1000.0),
        wake_options={"period": PERIOD, "dormant": False, **wake},
        relay_options=dict(RELAY),
        **extra,
    )
    medium, gateway = network.medium, network.controller_id
    xs = [x for x, _ in positions.values()]
    ys = [y for _, y in positions.values()]
    for _ in range(3000):
        spot = (rng.uniform(min(xs) - 150, max(xs) + 150), rng.uniform(min(ys) - 150, max(ys) + 150))
        network.move(gateway, spot)
        if len(medium.neighbours(gateway)) == count:
            break
    else:
        return None
    agents = network.wakers
    for agent in agents.values():
        agent.lease_until = WARM
    _run(network, clock, WARM + 5.0)
    depth = _depths(medium, gateway)
    model = queued_wake(medium, gateway)
    start = clock.now
    network.waker.wake(NET_LEASE)
    _run(network, clock, start + 40.0)
    woke = {node: agent.woke_at - start for node, agent in agents.items() if (agent.woke_at or -1) >= start}
    relays = network.relays.values()
    return {
        "reachable": [node for node in positions if node in depth],
        "depth": {node: d for node, d in depth.items() if node in positions},
        "woke": woke,
        "model": model,
        "copies": sum(r.stats["wakes_relayed"] + r.stats["wake_repeats"] for r in relays),
        "collisions": medium.stats["collisions"],
        "caller_repeats": network.relays[gateway].stats["wake_repeats"],
    }


def entry(seeds: int = 20) -> None:
    print(f"侦听周期 T = {PERIOD} s；模型 = 按中位链路图逐节点推算（含排队，不含碰撞与衰落）")
    for kind in ("random", "grid"):
        print(f"\n{'未规划随机 40' if kind == 'random' else '规划网格 40'}（{seeds} 个种子）")
        print(f"  {'入口':>4}{'可达':>6}{'唤醒':>8}{'最后醒来':>10}{'模型':>8}{'各节点 实测/模型':>18}{'长帧':>6}{'控制端重发':>10}")
        for count in (1, 3, 6):
            with simpool.pool() as pool:
                runs = [r for r in pool.map(entry_once, [(kind, seed, count, RELAY, WAKE) for seed in range(1, seeds + 1)]) if r]
            reach = sum(len(r["reachable"]) for r in runs)
            woke = sum(len(set(r["reachable"]) & set(r["woke"])) for r in runs)
            last = [max(r["woke"].values()) for r in runs if r["woke"]]
            last_model = [max(r["model"].values()) for r in runs if r["model"]]
            ratios = [r["woke"][n] / r["model"][n] for r in runs for n in r["woke"] if n in r["model"]]
            ratios.sort()
            print(f"  {count:>4}{reach:>6}{100 * woke / max(1, reach):>7.1f}%{sum(last) / len(last):>9.1f}s"
                  f"{sum(last_model) / len(last_model):>7.1f}s{f'{ratios[len(ratios) // 2]:.2f}（p90 {ratios[int(len(ratios) * 0.9)]:.2f}）':>18}"
                  f"{sum(r['copies'] for r in runs) / len(runs):>6.0f}{sum(r['caller_repeats'] for r in runs) / len(runs):>10.1f}"
                  f"  （{len(runs)} 个布局）")


def d5_once(args: tuple[str, int]) -> dict:
    """Existing features on a network that sleeps (task D5), one layout."""

    kind, seed = args
    rng, clock, network, controller = _build(kind, seed, {"idle_timeout": 60.0, "lease": 15}, discovery=True)
    gateway = network.sessions

    def run(seconds: float, until=None) -> None:
        end = clock.now + seconds
        while clock.now < end and not (until is not None and until()):
            clock.now += STEP
            network.tick(clock.now)
            controller.tick(clock.now)

    known_before = set(gateway.nodes)
    devices = sorted(network.devices)
    dead = rng.sample(devices, 2)
    rebooted = rng.sample([n for n in devices if n not in dead], 2)
    for node in dead:
        network.set_power(node, False)
    for node in rebooted:
        network.set_power(node, False)
        network.set_power(node, True)  # RAM lost while the network sleeps: it must join again
    asleep = sum(not network.wakers[n].awake for n in devices if n not in dead)
    start = clock.now
    reachable = {n for n in devices if n not in dead and network.reachable(n)}
    scan = gateway.scan()
    run(400.0, lambda: scan.done)
    scan_time = clock.now - start if scan.done else None
    answered = set(scan.answered)
    missing = (known_before | scan.mentioned) - answered
    rejoined = sum(network.agents[n].joined for n in rebooted)

    target = rng.choice(sorted(reachable - set(rebooted)))
    session = gateway.open_session(target)
    begun = clock.now
    run(60.0, lambda: session.state == "open")
    opened = clock.now - begun if session.state == "open" else None
    commands, traced = [], False
    if opened is not None:
        future = gateway.request(target, 3, 0)
        run(30.0, future.done)
        commands.append(future.result(timeout=0).ok)
        # The session can break while the command is out: it then goes by
        # flood and there is no delivery on the session to trace (random 5, 7).
        deliveries = gateway.deliveries(target)
        delivery = deliveries[-1] if deliveries else None
        if delivery is not None and gateway.trace(target):
            run(15.0, lambda: delivery.trace is not None)
            traced = delivery.trace is not None
        # The controller walks off 60 m mid-session.
        x, y = network.medium.port(network.controller_id).position
        angle = rng.uniform(0, 2 * math.pi)
        network.move(network.controller_id, (x + 60 * math.cos(angle), y + 60 * math.sin(angle)))
        for value in range(1, 6):
            future = gateway.request(target, 3, value)
            run(30.0, future.done)
            commands.append(future.result(timeout=0).ok)
            run(2.0)
    breaks = session.counts.get("broken", 0)
    gateway.close_session(target)
    run(400.0, lambda: all(not a.awake for n, a in network.wakers.items() if n not in dead))
    return {
        "asleep": asleep,
        "reachable": len(reachable),
        "found": len(answered & reachable),
        "dead_missing": len(set(dead) & missing),
        "false_missing": len(missing - set(dead)),
        "rebooted_found": len(set(rebooted) & answered),
        "rejoined": rejoined,
        "scan_time": scan_time,
        "opened": opened,
        "commands": commands,
        "traced": traced,
        "breaks": breaks,
        "all_asleep": all(not a.awake for n, a in network.wakers.items() if n not in dead),
        "settled": clock.now - start,
    }


def d5(seeds: int = 12) -> None:
    print(f"侦听周期 T = {PERIOD} s；每个布局：2 个节点断电、2 个节点在静默中重启，然后扫描、开会话、追踪、控制端走开 60 m 再发 5 条命令")
    for kind in ("random", "grid"):
        with simpool.pool() as pool:
            runs = list(pool.map(d5_once, [(kind, seed) for seed in range(1, seeds + 1)]))
        print(f"\n{'未规划随机 40' if kind == 'random' else '规划网格 40'}（{seeds} 个种子）")
        done = [r["scan_time"] for r in runs if r["scan_time"] is not None]
        print(f"  扫描（先全网唤醒）：完成 {len(done)}/{seeds}，平均 {sum(done) / max(1, len(done)):.0f} s；"
              f"找到可达节点 {sum(r['found'] for r in runs)}/{sum(r['reachable'] for r in runs)}")
        print(f"  断电的节点被列为未回答 {sum(r['dead_missing'] for r in runs)}/{2 * seeds}；其余被误列 {sum(r['false_missing'] for r in runs)}；"
              f"静默中重启的节点被扫到 {sum(r['rebooted_found'] for r in runs)}/{2 * seeds}（算作已入网 {sum(r['rejoined'] for r in runs)}）")
        opened = [r["opened"] for r in runs if r["opened"] is not None]
        print(f"  扫描后开会话：{len(opened)}/{seeds}，平均 {sum(opened) / max(1, len(opened)):.1f} s；追踪拿回 {sum(r['traced'] for r in runs)}/{len(opened)}")
        first = sum(r["commands"][0] for r in runs if r["commands"])
        moved = [ok for r in runs for ok in r["commands"][1:]]
        print(f"  命令：走开前 {first}/{len(opened)}，走开 60 m 后 {sum(moved)}/{len(moved)}（断链 {sum(r['breaks'] for r in runs)} 次）")
        print(f"  结束后全部回到静默 {sum(r['all_asleep'] for r in runs)}/{seeds}（平均 {sum(r['settled'] for r in runs) / seeds:.0f} s 后）")


def life() -> None:
    """Lifetime of a node at T = ``PERIOD`` by use pattern (D6; supply set by the user 2026-10-19).

    Three FR6 lithium cells in series behind an LDO, counted conservatively
    at ``BATTERY_MAH`` (user). Per-visit costs come from ``probe`` / ``d5``:
    a node off the session path wakes about twice for 7.5 s and sends one
    long preamble each time; a node on the path stays up about 60 s more;
    a scan wakes everyone for about 130 s.
    """

    sniffer = Sniffer(period=PERIOD)
    lead = preamble_for(sniffer, 7) * symbol_time(7) + lora_airtime(WAKE_BYTES, 7)
    print(f"侦听周期 T = {PERIOD} s，电池按 {BATTERY_MAH:.0f} mAh 计（三节 FR6 串联 + LDO，用户定的保守值），电流为估计值")
    patterns = (
        ("长期静默（无到访）", 0, 0, 0),
        ("每天 1 次会话", 1, 0, 0),
        ("每月 1 个繁忙日（200 次会话 + 2 次扫描）", 200 / 30, 0, 2 / 30),
        ("繁忙日里每次都在会话路径上", 200 / 30, 200 / 30, 2 / 30),
        ("每天 24 次会话", 24, 0, 0),
        ("每天 1 次扫描 + 24 次会话", 24, 0, 1),
    )
    quiescent = (0.0, 1e-6, 5e-6, 50e-6)
    print(f"  {'用法':<34}" + "".join(f"{f'LDO {q * 1e6:g} µA':>14}" for q in quiescent))
    for name, sessions, on_path, scans in patterns:
        cells = []
        for q in quiescent:
            c = RadioCurrents(regulator=q)
            awake = c.rx + c.mcu_run + c.regulator
            off = 2 * (7.5 * awake + lead * (c.tx + c.mcu_run))
            daily = dormant_current(sniffer, c) * 86400 + sessions * off + on_path * 60 * awake
            daily += scans * (130 * awake + 2 * lead * (c.tx + c.mcu_run))
            cells.append(BATTERY_MAH * 3.6 / daily / 365)
        print(f"  {name:<34}" + "".join(f"{years:>12.1f} 年" for years in cells))
    c = RadioCurrents()
    heavy = 200 * (2 * (7.5 * (c.rx + c.mcu_run) + lead * (c.tx + c.mcu_run)) + 60 * (c.rx + c.mcu_run))
    print(f"  一个繁忙日、每次都在路径上的节点：多耗约 {heavy / 3.6:.0f} mAh（电池的 {100 * heavy / 3.6 / BATTERY_MAH:.1f} %）")


def c5_once(args: tuple[str, int, bool]) -> Optional[dict]:
    """A session on a sleeping network loses a relay; recover with or without a detour (task C5)."""

    kind, seed, detours = args
    rng, clock, network, controller = _build(
        kind, seed, {"idle_timeout": None, "lease": 15, "detours": detours, **GATEWAY}, discovery=True
    )
    gateway = network.sessions

    def run(seconds: float, until=None) -> None:
        end = clock.now + seconds
        while clock.now < end and not (until is not None and until()):
            clock.now += STEP
            network.tick(clock.now)
            controller.tick(clock.now)

    scan = gateway.scan()
    run(400.0, lambda: scan.done)
    run(400.0, lambda: all(not a.awake for a in network.wakers.values()))  # back to sleep
    depth = _depths(network.medium, network.controller_id)
    far = [n for n, d in depth.items() if n in network.devices and d >= 3 and n in gateway.nodes]
    if not far:
        return None
    target = rng.choice(sorted(far))
    session = gateway.open_session(target)
    run(60.0, lambda: session.state == "open")
    if session.state != "open" or len(session.route) < 2:
        return None
    future = gateway.request(target, 3, 0)
    run(30.0, future.done)
    dead = session.route[len(session.route) // 2]
    network.set_power(dead, False)
    woken = sum(a.stats["woken"] + a.stats["woken_by_route"] for a in network.wakers.values())
    start = clock.now
    future = gateway.request(target, 3, 1)
    run(40.0, future.done)
    ok = future.result(timeout=0).ok
    run(40.0, lambda: session.state == "open" and dead not in session.route)
    recovered = session.state == "open" and dead not in session.route
    return {
        "ok": ok,
        "recovered": recovered,
        "time": clock.now - start if recovered else None,
        "woken": sum(a.stats["woken"] + a.stats["woken_by_route"] for a in network.wakers.values()) - woken,
        "detours": session.counts["detours"],
        "probes": session.counts["probes"],
    }


def c5(seeds: int = 12) -> None:
    print(f"侦听周期 T = {PERIOD} s；静默网络、先扫描一次；会话中途路径中间的中继断电，下一条命令触发断链")
    for kind in ("random", "grid"):
        print(f"\n{'未规划随机 40' if kind == 'random' else '规划网格 40'}（{seeds} 个种子）")
        for detours in (False, True):
            with simpool.pool() as pool:
                runs = [r for r in pool.map(c5_once, [(kind, s, detours) for s in range(1, seeds + 1)]) if r]
            times = [r["time"] for r in runs if r["time"] is not None]
            print(f"  {'定向重探' if detours else '全网重探'}：命令 {sum(r['ok'] for r in runs)}/{len(runs)}，"
                  f"绕开坏中继 {sum(r['recovered'] for r in runs)}/{len(runs)}，平均 {sum(times) / max(1, len(times)):.1f} s；"
                  f"恢复期间被叫醒 {sum(r['woken'] for r in runs) / len(runs):.0f} 节点次；"
                  f"定向探测 {sum(r['detours'] for r in runs) / len(runs):.1f} 次 / 会话")


def main(argv: list[str]) -> None:
    command = argv[0] if argv else "theory"
    if command == "theory":
        theory()
    elif command == "life":
        life()
    elif command == "d5":
        d5(int(argv[1]) if len(argv) > 1 else 12)
    elif command == "c5":
        for arg in argv[2:]:
            name, value = arg.split("=", 1)
            GATEWAY[name.removeprefix("gw.")] = float(value)
        c5(int(argv[1]) if len(argv) > 1 else 12)
    elif command in ("net", "probe", "entry"):
        rest = [arg for arg in argv[1:] if "=" not in arg]
        for arg in argv[1:]:
            if "=" in arg:
                name, value = arg.split("=", 1)
                if name.startswith("wk."):
                    WAKE[name[3:]] = float(value)
                elif name == "period":
                    global PERIOD
                    PERIOD = float(value)
                elif name.startswith("gw."):
                    GATEWAY[name[3:]] = value if name == "gw.wake" else int(value) if name == "gw.wake_lease" else float(value)
                else:
                    RELAY[name] = (0.05, float(value)) if name == "wake_backoff" else float(value)
        {"net": net, "probe": probe, "entry": entry}[command](int(rest[0]) if rest else 20)
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
