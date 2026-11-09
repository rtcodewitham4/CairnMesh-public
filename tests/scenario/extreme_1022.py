"""Extreme cases past ``stress_bench`` (2026-10-22): hunting for integrity defects.

    PYTHONPATH=. python tests/scenario/extreme_1022.py [seeds] [scenario …]   # from the repository root
    python -m pytest -m slow tests/scenario    # one seed of each, as gate full runs it

Every command gets a unique tag, (ACTION, VALUE), so an execution is matched
to the command that asked for it, not to its message id: two commands that
end up under one message id (the defect class this hunts) show up as "DONE
but never ran" instead of being merged, as a ledger keyed by message id
would merge them. Per command:

* ``dup``: run more than once; ``forged``: run but never issued;
* ``done_not_run``: the controller said DONE, the device never ran it;
* ``wrong_result``: DONE with a result other than the device's;
* ``not_run_ran``: NOT_RUN, yet it ran; ``no_outcome``: no outcome at all.

Scenarios (link-budget model, seeds in parallel through ``simpool``):

* ``wrap``: the target is off for a long outage; an operator keeps sending
  a command every few minutes and gives each up after 10 min of confirming,
  the gateway keeps trying to open a session; then the target comes back.
* ``restart_wrap``: the same outage across 260 controller restarts (state
  file kept: EPOCH +1 each time, so it wraps).
* ``loss``: 60–80 % frame loss, and 60 % with a 50 % duty jammer.
* ``slow_chaos``: slow actions (ACCEPTED + RESULT) and in-budget ones, the
  target power-cycling while they run, 30 % loss.
* ``deep``: a 20-hop line, commands to hops 14–20 (past ``MAX_RELAYS``).
"""

from __future__ import annotations

import random
import sys
import tempfile
import time
from pathlib import Path

from cairnmesh import robustness_bench as rb
from cairnmesh import session_bench as sb
from cairnmesh import simpool
from cairnmesh.controller import ACCEPTED, DEFAULT_CONFIRM_WINDOW, DONE, NOT_RUN, NOT_SENT, UNCONFIRMED
from cairnmesh.frames import QUERY_ACTION, ReplyResult
from cairnmesh.mesh_sim import line, planned
from cairnmesh.stress_bench import _jam

BAD = ("dup", "forged", "done_not_run", "wrong_result", "not_run_ran", "no_outcome")


def _tag(k: int) -> tuple[int, int]:
    return 1 + (k // 256) % 200, k % 256


def _result_for(value: int) -> int:
    return ReplyResult.ERROR if value % 4 == 3 else ReplyResult.OK


class Ledger:
    def __init__(self, network, clock=None) -> None:
        self.clock = clock
        #: tag → (node, start, duration) of actions that take time (``slow_chaos``)
        self.started: dict[tuple[int, int], tuple[int, float, float]] = {}
        #: (time, node, on) of every power switch, to tell a cut action (ERROR by design, spec 4.1)
        self.power: list[tuple[float, int, bool]] = []
        self.next = 0
        self.issued: set[tuple[int, int, int]] = set()
        self.executed: list[tuple[int, int, int, int]] = []
        self.outcomes: list[tuple[int, tuple[int, int], str | None, int | None]] = []
        for node, device in network.devices.items():
            original = device.perform_action

            def perform(frame, node=node, original=original):
                original(frame)
                result = _result_for(frame.value)
                if frame.action != QUERY_ACTION:
                    self.executed.append((node, frame.action, frame.value, result))
                return result

            device.perform_action = perform

    def send(self, network, target: int):
        tag = _tag(self.next)
        self.next += 1
        self.issued.add((target, *tag))  # registered first: it may run whatever happens next
        return tag, network.sessions.request(target, *tag)

    def settle(self, target: int, tag, future) -> None:
        if not future.done():
            self.outcomes.append((target, tag, None, None))
            return
        outcome = future.result(timeout=0)
        self.outcomes.append((target, tag, outcome.status, outcome.result))

    def command(self, clock, network, controller, target: int, give_up: float = rb.CONFIRM_LIMIT, during=None) -> str | None:
        tag, future = self.send(network, target)
        if during is not None:
            during()
        sb.run(clock, network, controller, rb.COMMAND_WAIT, future.done)
        if not future.done() and controller.confirming():
            sb.run(clock, network, controller, give_up, future.done)
            if not future.done():
                controller.abandon(target)
                sb.run(clock, network, controller, 1.0)
        self.settle(target, tag, future)
        sb.run(clock, network, controller, 0.3)
        return self.outcomes[-1][2]

    def _cut(self, node: int, tag) -> bool:
        """The action ran while its node lost power: recorded ERROR on purpose (spec 4.1)."""

        node_, start, duration = self.started.get(tag, (node, 0.0, 0.0))
        return any(n == node and not on and start <= t < start + duration for t, n, on in self.power)

    def audit(self) -> dict:
        runs: dict[tuple[int, int, int], list[int]] = {}
        for node, action, value, result in self.executed:
            runs.setdefault((node, action, value), []).append(result)
        bad = dict.fromkeys(BAD, 0)
        for key, results in runs.items():
            bad["dup"] += len(results) - 1
            bad["forged"] += key not in self.issued
        counts = {"done": 0, "not_run": 0, "unconfirmed": 0, "accepted": 0, "not_sent": 0}
        examples = []
        for node, tag, status, result in self.outcomes:
            ran = runs.get((node, *tag))
            if status is None:
                bad["no_outcome"] += 1
            elif status == DONE:
                counts["done"] += 1
                if not ran:
                    bad["done_not_run"] += 1
                    examples.append((node, tag))
                elif result != ran[0] and not (result == ReplyResult.ERROR and self._cut(node, tag)):
                    bad["wrong_result"] += 1
            elif status == NOT_RUN:
                counts["not_run"] += 1
                bad["not_run_ran"] += bool(ran)
            elif status == UNCONFIRMED:
                counts["unconfirmed"] += 1
            elif status in (ACCEPTED, NOT_SENT):  # certain too: runs once / never sent
                counts[status] += 1
        return {"bad": bad, "counts": counts, "commands": len(self.outcomes), "examples": examples[:3]}


def _setup(kind: str, seed: int, drop: float, **extra):
    positions, layout_extra, targets, where = sb._a5_layout(kind, seed)
    clock, network, controller = sb.build(
        positions, seed=seed, drop=drop, gateway_options={"idle_timeout": None},
        confirm_window=DEFAULT_CONFIRM_WINDOW, controller_position=where, **layout_extra, **extra,
    )
    if targets is None:
        targets = sb._far_targets(network, 3)
    ledger = Ledger(network, clock)
    switch = network.set_power

    def set_power(node: int, on: bool) -> None:
        ledger.power.append((clock.now, node, on))
        switch(node, on)

    network.set_power = set_power
    return clock, network, controller, targets, where, ledger


# ------------------------------------------------------------------ scenarios


def wrap_one(args) -> dict:
    kind, seed, cycles = args
    clock, network, controller, targets, _, ledger = _setup(kind, seed, 0.10)
    target = targets[0]
    sb.opened(clock, network, controller, target, 60.0)
    for _ in range(4):
        ledger.command(clock, network, controller, target)
    start = controller._id_ledger.next_sequence[target]
    network.set_power(target, False)
    turned = 0
    for _ in range(cycles):
        before = controller._id_ledger.next_sequence[target]
        ledger.command(clock, network, controller, target, give_up=600.0)
        network.sessions.open_session(target)  # the gateway keeps trying to reach it
        sb.run(clock, network, controller, 120.0)
        turned += (controller._id_ledger.next_sequence[target] - before) % 256
    network.set_power(target, True)
    sb.run(clock, network, controller, 30.0)
    sb.opened(clock, network, controller, target, 120.0)
    for _ in range(12):
        ledger.command(clock, network, controller, target)
    out = ledger.audit()
    out["ids_used_in_outage"] = turned
    out["counter_start"] = start
    return out


def restart_wrap_one(args) -> dict:
    kind, seed, restarts = args
    clock, network, controller, targets, _, ledger = _setup(kind, seed, 0.10)
    target = targets[0]
    with tempfile.TemporaryDirectory() as scratch:
        state = str(Path(scratch) / "controller.json")

        def fresh():
            made = network.controller(
                network.sessions, clock=clock, timeout=8.0, max_retries=2, confirm_window=DEFAULT_CONFIRM_WINDOW,
                state_file=state, epoch=None,
            )
            network.sessions.attach(made)
            return made

        controller = fresh()
        sb.opened(clock, network, controller, target, 60.0)
        for _ in range(4):
            ledger.command(clock, network, controller, target)
        first_epoch = controller.epoch
        network.set_power(target, False)
        for _ in range(restarts):
            controller = fresh()
            ledger.command(clock, network, controller, target, give_up=60.0)
        network.set_power(target, True)
        controller = fresh()
        sb.run(clock, network, controller, 30.0)
        sb.opened(clock, network, controller, target, 120.0)
        for _ in range(12):
            ledger.command(clock, network, controller, target)
        out = ledger.audit()
        out["epochs"] = (first_epoch, controller.epoch)
    return out


def loss_one(args) -> dict:
    kind, seed, drop, duty = args
    clock, network, controller, targets, where, ledger = _setup(kind, seed, drop)
    if duty:
        _jam(network, clock, where, seed, duty)
    for target in targets[:2]:
        sb.opened(clock, network, controller, target, 120.0)
        for _ in range(8):
            ledger.command(clock, network, controller, target, give_up=1800.0)
        network.sessions.close_session(target)
    return ledger.audit()


def slow_chaos_one(args) -> dict:
    kind, seed = args
    pick = random.Random(seed + 5)
    hold: dict = {}

    def action_time(frame):  # drawn per command: instant, in budget, slow, very slow
        duration = pick.choice((0.0, 0.3, 4.0, 12.0))
        hold["ledger"].started[(frame.action, frame.value)] = (frame.target_id, hold["clock"].now, duration)
        return duration

    clock, network, controller, targets, _, ledger = _setup(
        kind, seed, 0.30, device_options={"action_time": action_time}
    )
    hold.update(ledger=ledger, clock=clock)
    rng = random.Random(seed + 9)
    nodes = sorted(network.devices)
    for target in targets[:2]:
        sb.opened(clock, network, controller, target, 120.0)
        for k in range(16):
            def cycle(target=target, k=k):
                sb.run(clock, network, controller, rng.uniform(0.0, 8.0))
                victim = target if k % 2 == 0 else rng.choice(nodes)
                network.set_power(victim, False)
                sb.run(clock, network, controller, rng.uniform(0.5, 20.0))
                network.set_power(victim, True)

            ledger.command(clock, network, controller, target, give_up=1800.0, during=cycle)
        network.sessions.close_session(target)
    return ledger.audit()


def deep_one(args) -> dict:
    (seed,) = args
    clock, network, controller = sb.build(
        line(20), seed=seed, drop=0.05, gateway_options={"idle_timeout": None},
        confirm_window=DEFAULT_CONFIRM_WINDOW, **planned(),
    )
    ledger = Ledger(network)
    for target in (14, 16, 17, 18, 20):
        sb.opened(clock, network, controller, target, 120.0)
        for _ in range(3):
            ledger.command(clock, network, controller, target, give_up=600.0)
        network.sessions.close_session(target)
    return ledger.audit()


SCENARIOS = {
    "wrap": (wrap_one, lambda seeds: [(k, s, c) for k in ("random40", "grid") for s in seeds for c in (15, 40)]),
    "restart_wrap": (restart_wrap_one, lambda seeds: [(k, s, 260) for k in ("random40",) for s in seeds]),
    "loss": (loss_one, lambda seeds: [(k, s, d, j) for k in ("random40", "grid") for s in seeds
                                       for d, j in ((0.6, 0.0), (0.7, 0.0), (0.8, 0.0), (0.6, 0.5))]),
    "slow_chaos": (slow_chaos_one, lambda seeds: [(k, s) for k in ("random40", "grid") for s in seeds]),
    "deep": (deep_one, lambda seeds: [(s,) for s in seeds]),
}


def _run_one(job):
    name, args = job
    start = time.process_time()
    try:
        out = SCENARIOS[name][0](args)
    except Exception as exc:  # noqa: BLE001 - a crash is a finding too
        out = {"bad": {"crash": 1}, "error": f"{type(exc).__name__}: {exc}", "counts": {}, "commands": 0}
    out["cpu"] = time.process_time() - start
    return name, args, out


def main(argv: list[str]) -> None:
    seeds = list(range(int(argv[0]))) if argv else list(range(4))
    names = argv[1:] or list(SCENARIOS)
    jobs = [(name, args) for name in names for args in SCENARIOS[name][1](seeds)]
    jobs.sort(key=lambda job: job[0] in ("wrap", "restart_wrap"), reverse=True)  # long ones first
    totals: dict[str, dict] = {}
    started = time.monotonic()
    with simpool.pool() as pool:
        for name, args, out in pool.map(_run_one, jobs):
            bad = {key: value for key, value in out["bad"].items() if value}
            extra = {key: out[key] for key in ("ids_used_in_outage", "epochs", "examples", "error") if out.get(key)}
            print(f"{name:12s} {str(args):34s} cmds={out['commands']:3d} {out.get('counts')} "
                  f"bad={bad or 0} cpu={out['cpu']:.0f}s {extra}", flush=True)
            total = totals.setdefault(name, {"commands": 0, "cpu": 0.0, "bad": {}})
            total["commands"] += out["commands"]
            total["cpu"] += out["cpu"]
            for key, value in out["bad"].items():
                total["bad"][key] = total["bad"].get(key, 0) + value
    print(f"\n== summary ({time.monotonic() - started:.0f} s wall, {simpool.workers()} workers)")
    for name, total in totals.items():
        bad = {key: value for key, value in total["bad"].items() if value}
        print(f"{name:12s} commands={total['commands']:5d} cpu={total['cpu']:7.0f}s  defects={bad or 'none'}")


if __name__ == "__main__":
    main(sys.argv[1:])
