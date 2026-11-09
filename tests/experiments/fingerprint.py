"""Every frame on air (time, sender, bytes) and every outcome, hashed: an optimisation must not change one bit."""
import hashlib, sys, json, tempfile
from concurrent.futures import ProcessPoolExecutor

from cairnmesh import stress_bench as s, session_bench as sb, wake_bench as wb, robustness_bench as rb
from cairnmesh.controller import DEFAULT_CONFIRM_WINDOW
from cairnmesh.events import HealthChanged


def digest(network, outcomes, extra=""):
    h = hashlib.sha256()
    for t, sender, data in network.medium.log:
        h.update(f"{t:.9f}|{sender}|".encode()); h.update(bytes(data))
    h.update(repr(outcomes).encode()); h.update(extra.encode())
    return h.hexdigest()[:16], len(network.medium.log)


def dormant(args):
    kind, seed = args
    rng, clock, network, _ = wb._build(kind, seed, {"idle_timeout": 60.0, "lease": 15}, discovery=True)
    network.medium.log = []
    network.medium.drop = 0.30
    controller = network.controller(network.sessions, clock=clock, timeout=8.0, max_retries=2, confirm_window=DEFAULT_CONFIRM_WINDOW)
    gw = network.sessions; gw.attach(controller)
    gw.scan(); sb.run(clock, network, controller, 300.0, lambda: gw.scan_state is None)
    ledger = s.Ledger(network)
    depth = wb._depths(network.medium, network.controller_id)
    far = sorted(n for n, d in depth.items() if n in network.devices and d >= 2)
    for target in rng.sample(far, min(2, len(far))):
        test = gw.accept(target, rounds=5)
        sb.run(clock, network, controller, 200.0, lambda: target in gw.acceptance)
        for value in range(4):
            gw.open_session(target)
            ledger.command(clock, network, controller, target, value)
        gw.close_session(target)
    return ("dormant", kind, seed) + digest(network, ledger.outcomes, repr(sorted((k, v.passed) for k, v in gw.acceptance.items())))


def awake(args):
    kind, seed = args
    clock, network, controller, targets, where = rb._setup(kind, seed, 0.30)
    ledger = s.Ledger(network)
    for target in targets[:2]:
        sb.opened(clock, network, controller, target, 60.0)
        for value in range(4):
            ledger.command(clock, network, controller, target, value)
        network.sessions.close_session(target)
    return ("awake", kind, seed) + digest(network, ledger.outcomes)


def chaos(args):
    kind, seed = args
    r = s.chaos_one((kind, seed))
    return ("chaos", kind, seed, json.dumps(r["counts"], sort_keys=True), r["commands"])


def digest_many(networks, outcomes, extra=""):
    """Hash several deterministic network logs and one shared outcome summary."""

    h = hashlib.sha256()
    frames = 0
    for index, network in enumerate(networks):
        for t, sender, data in network.medium.log:
            h.update(f"{index}|{t:.9f}|{sender}|".encode())
            h.update(bytes(data))
            frames += 1
    h.update(repr(outcomes).encode())
    h.update(extra.encode())
    return h.hexdigest()[:16], frames


def _scan(gateway, clock, network, controller, seconds=300.0):
    """Run one explicit scan to completion and return it."""

    scan = gateway.scan()
    sb.run(clock, network, controller, seconds, lambda: scan.done)
    return scan


def dual_radio(args):
    """Two-radio session: move to radio 1, strike once, ban, then expire the ban."""

    kind, seed = args
    clock, network, controller = sb.build(
        sb.LINE,
        seed=seed,
        record=True,
        gateway_options={
            "idle_timeout": None,
            "radios": 2,
            "radio_strikes": 1,
            "radio_ban": 20.0,
        },
    )
    gateway = network.sessions
    session = gateway.open_session(5)
    sb.run(clock, network, controller, 40.0, lambda: session.state == "open")
    sb.run(clock, network, controller, 10.0, lambda: session.radio == 1)
    gateway._strike(session, clock.now)
    gateway._leave_radio(session)
    banned_until = gateway._radio_banned[5]
    sb.run(clock, network, controller, banned_until - clock.now + 0.01)
    gateway._assign_radios(clock.now)
    sb.run(clock, network, controller, 20.0, lambda: session.radio == 1)
    future = gateway.request(5, 3, 7)
    sb.run(clock, network, controller, 30.0, future.done)
    assert gateway.stats["radio_bans"] == 1
    assert session.radio == 1
    outcomes = {
        "ban_expired": clock.now >= banned_until,
        "bans": gateway.stats["radio_bans"],
        "delivered": future.done() and future.result(timeout=0).ok,
        "radio": session.radio,
        "strikes": session.counts["radio_strikes"],
    }
    return ("dual_radio", kind, seed) + digest_many((network,), outcomes)


def health(args):
    """One health warning and its recovery, with the published notices summarized."""

    kind, seed = args
    clock, network, controller = sb.build(sb.LINE, seed=seed, record=True, gateway_options={"idle_timeout": None})
    gateway = network.sessions
    seen = []
    controller.events.subscribe(
        lambda event: seen.append((type(event).__name__, event.node_id, event.ok, event.reasons))
        if isinstance(event, HealthChanged) else None
    )
    for _ in range(50):
        gateway._note_attempts(5, 3, False)
    warned = gateway.health[5].ok, tuple(gateway.health[5].reasons)
    for _ in range(100):
        gateway._note_attempts(5, 1, True)
    recovered = gateway.health[5].ok, tuple(gateway.health[5].reasons)
    health_states = [ok for _, _, ok, _ in seen]
    first_warning = next((index for index, ok in enumerate(health_states) if not ok), None)
    first_recovery = next((index for index, ok in enumerate(health_states) if ok), None)
    assert first_warning is not None and first_recovery is not None
    assert first_warning < first_recovery
    outcomes = (warned, recovered, tuple(seen))
    return ("health", kind, seed) + digest_many((network,), outcomes)


def saved_scan(args):
    """Save a scanned node book, load it into a fresh deterministic network, then scan again."""

    kind, seed = args
    gateway_options = {"idle_timeout": None, "rescan_after": None}
    first = sb.build(
        sb.LINE,
        seed=seed,
        record=True,
        discovery_options={"join": False},
        gateway_options=gateway_options,
    )
    _scan(first[1].sessions, first[0], first[1], first[2], 300.0)
    with tempfile.TemporaryDirectory(prefix="cairnmesh-fingerprint-") as scratch:
        path = f"{scratch}/nodes.json"
        first[1].sessions.save_nodes(path)
        second = sb.build(
            sb.LINE,
            seed=seed,
            record=True,
            discovery_options={"join": False},
            gateway_options=gateway_options,
        )
        loaded = second[1].sessions.load_nodes(path)
        assert loaded > 0
        _scan(second[1].sessions, second[0], second[1], second[2], 300.0)
    final = tuple(
        (node, known.how, known.measured, known.hops, known.state, len(known.neighbours))
        for node, known in sorted(second[1].sessions.nodes.items())
    )
    outcomes = (loaded, final)
    return ("saved_scan", kind, seed) + digest_many((first[1], second[1]), outcomes)


def weak(args):
    """A scan followed by the deterministic weak-link summary."""

    kind, seed = args
    clock, network, controller = sb.build(
        sb.LADDER,
        seed=seed,
        record=True,
        discovery_options={"join": False},
        gateway_options={"idle_timeout": None, "rescan_after": None},
    )
    gateway = network.sessions
    scan = _scan(gateway, clock, network, controller)
    links = tuple(
        (link.sender, link.receiver, link.margin_db, link.bottleneck_of, link.carries)
        for link in gateway.weak_links(20.0)
    )
    assert links
    outcomes = (scan.done, sorted(gateway.nodes), links)
    return ("weak", kind, seed) + digest_many((network,), outcomes)


def join_storm(args):
    """Power-on JOINs trigger a scan on a fixed line layout."""

    kind, seed = args
    clock, network, controller = sb.build(
        sb.LINE,
        seed=seed,
        record=True,
        discovery_options={"join_spread": 5.0},
        gateway_options={"idle_timeout": None, "rescan_after": None},
    )
    gateway = network.sessions
    sb.run(clock, network, controller, 60.0, lambda: gateway.scan_state is not None)
    scan = gateway.scan_state
    assert scan is not None
    sb.run(clock, network, controller, 300.0, lambda: scan is None or scan.done)
    sb.run(clock, network, controller, 120.0, lambda: all(agent.joined for agent in network.agents.values()))
    outcomes = (
        sorted(gateway.nodes),
        tuple(sorted((node, agent.joined) for node, agent in network.agents.items())),
        scan is not None,
        None if scan is None else scan.done,
    )
    return ("join_storm", kind, seed) + digest_many((network,), outcomes)


if __name__ == "__main__":
    jobs = []
    with ProcessPoolExecutor() as pool:
        futs = [pool.submit(dormant, (k, sd)) for k in ("random", "grid") for sd in (1, 2)]
        futs += [pool.submit(awake, (k, sd)) for k in ("random40", "grid40") for sd in (1, 2)]
        futs += [pool.submit(chaos, (k, sd)) for k in ("random40", "grid40") for sd in (1,)]
        futs += [pool.submit(dual_radio, ("line", 1))]
        futs += [pool.submit(health, ("line", 2))]
        futs += [pool.submit(saved_scan, ("line", 4))]
        futs += [pool.submit(weak, ("ladder", 2))]
        futs += [pool.submit(join_storm, ("line", 3))]
        for f in futs:
            print(*f.result(), flush=True)
