"""Extreme-condition integrity survey (slow; not unit tests; no protocol code here).

    python -m cairnmesh.stress_bench [seeds] [--out PATH] [--important]   # writes STRESS_REPORT.md next to this file

``robustness_bench`` measures delivery under one fault at a time. This one
pushes every fault to its limit and combines them, and audits **every
command end to end** against what the devices really did:

* executed at most once;
* nothing executed that the controller never issued (garbage, forgeries, replays);
* executed with exactly the ACTION and VALUE that were sent;
* a result handed to the controller (``DONE``) is the one the device returned
  for *that* command; actions answer ``ERROR`` for every fourth VALUE so a
  result crossed between commands would show;
* ``NOT_RUN`` means the device never ran it; ``DONE`` means it did;
* every command gets an outcome (the controller never hangs);
* layer 3 (Q21): two contracts. **Ordinary** commands (default):
  the controller confirms them at most ``confirm_limit`` (30 min) and then ends
  them UNCONFIRMED ("may have run"); the acceptance is **no wrong conclusion**
  (the checks above stay 0), the unconfirmed ones are counted and how many of
  them did run. **Important** commands (``--important``): the controller keeps
  confirming (``rb.CONFIRM_LIMIT`` of simulated time at most here; a command
  still unknown then is abandoned and counted as unconfirmed): that stays 0.

Scenarios (unplanned random 40 and planned 5 × 8 grid, link-budget model):
loss up to 50 %; a jammer near the gateway up to 50 % duty; chaos (loss,
jammer, nodes rebooting, path relays dying, an outside radio replaying and
bit-flipping captured frames, all at once); a controller restart with a new
EPOCH while a command is on the air; a dormant low-power network (task D)
under 30 % loss.
"""

from __future__ import annotations

import multiprocessing
import random
import statistics
import sys
import time
from pathlib import Path

from . import simpool
from . import robustness_bench as rb
from . import session_bench as sb
from .controller import DEFAULT_CONFIRM_WINDOW, DONE, INTERRUPTED, NOT_RUN, UNCONFIRMED
from .frames import ReplyResult

ACTION = 3
ATTACKER = 0xED  # the replaying radio (the jammer is ``rb.OUTSIDER``)
CONFIRM_LIMIT = rb.CONFIRM_LIMIT


def _result_for(value: int) -> int:
    return ReplyResult.ERROR if value % 4 == 3 else ReplyResult.OK


class Ledger:
    """What was issued, what the devices did, and what the controller was told."""

    def __init__(self, network) -> None:
        #: ``(node, message id)`` → ``(action, value)``
        self.issued: dict[tuple[int, int], tuple[int, int]] = {}
        #: ``(node, message id, action, value, result)`` per execution
        self.executed: list[tuple[int, int, int, int, int]] = []
        #: ``(node, message id, status, result, queried, elapsed)``; id ``None``: no outcome at all
        self.outcomes: list[tuple[int, int | None, str | None, int | None, bool, float | None]] = []
        #: EPOCH moves of each controller used (many ids to skip); the restart scenario has two
        self._moves: dict[int, int] = {}
        for node, device in network.devices.items():
            original = device.perform_action

            def perform(frame, node=node, original=original):
                original(frame)  # keeps the device's own counters
                result = _result_for(frame.value)
                self.executed.append((node, frame.message_id, frame.action, frame.value, result))
                return result

            device.perform_action = perform

    def command(self, clock, network, controller, target: int, value: int, during=None, repair=None) -> None:
        future = network.sessions.request(target, ACTION, value, important=rb.IMPORTANT)
        if during is not None:
            during()  # e.g. the target loses power while the command is on its way
        rb.wait(clock, network, controller, target, future, repair)  # layer 3: waits out confirming
        if not future.done():
            self.outcomes.append((target, None, None, None, False, None))
            return
        outcome = future.result(timeout=0)
        self._moves[id(controller)] = controller.epoch_moves
        ident = controller.epoch << 8 | outcome.sequence
        self.issued[(target, ident)] = (ACTION, value)
        self.outcomes.append((target, ident, outcome.status, outcome.result, outcome.queried, outcome.elapsed))
        sb.run(clock, network, controller, 0.3)

    def audit(self) -> dict:
        runs: dict[tuple[int, int], list[tuple[int, int, int]]] = {}
        for node, ident, action, value, result in self.executed:
            runs.setdefault((node, ident), []).append((action, value, result))
        bad = {"dup": 0, "forged": 0, "tampered": 0, "wrong_result": 0, "not_run_ran": 0, "done_not_run": 0, "no_outcome": 0}
        for key, done in runs.items():
            bad["dup"] += len(done) - 1
            if key not in self.issued:
                bad["forged"] += len(done)
            elif any((action, value) != self.issued[key] for action, value, _ in done):
                bad["tampered"] += 1
        counts = {"done": 0, "direct": 0, "confirmed": 0, "not_run": 0, "interrupted": 0, "unconfirmed": 0, "unconfirmed_ran": 0}
        times = []
        confirm_times = []
        for node, ident, status, result, queried, elapsed in self.outcomes:
            if ident is None:
                bad["no_outcome"] += 1
                continue
            ran = runs.get((node, ident))
            if status == DONE:
                counts["done"] += 1
                counts["confirmed" if queried else "direct"] += 1
                if not queried:
                    times.append(elapsed)
                else:
                    confirm_times.append(elapsed)
                if not ran:
                    bad["done_not_run"] += 1
                elif result != ran[0][2]:
                    bad["wrong_result"] += 1
            elif status == INTERRUPTED:
                # The device reset with the action open (wire v5): a final state, it may or may not have
                # completed, so only "the device never saw it" would be a violation.
                counts["interrupted"] += 1
                confirm_times.append(elapsed)
                if not ran:
                    bad["done_not_run"] += 1
            elif status == NOT_RUN:
                counts["not_run"] += 1
                confirm_times.append(elapsed)
                bad["not_run_ran"] += bool(ran)
            elif status == UNCONFIRMED:
                counts["unconfirmed"] += 1
                counts["unconfirmed_ran"] += bool(ran)
        return {"bad": bad, "counts": counts, "commands": len(self.outcomes), "times": times, "confirm_times": confirm_times,
                "epoch_moves": sum(self._moves.values())}


# ------------------------------------------------------------------ scenarios


def _setup(kind: str, seed: int, drop: float):
    clock, network, controller, targets, where = rb._setup(kind, seed, drop)
    return clock, network, controller, targets, where, Ledger(network)


def _jam(network, clock, where, seed: int, duty: float) -> None:
    jammer = network.medium.add(rb.OUTSIDER, (where[0] + 40.0, where[1] + 30.0))
    rng = random.Random(seed + 99)
    period = network.medium.airtime_of(40) / duty
    next_burst = [clock.now + rng.uniform(0, period)]
    original_tick = network.tick

    def tick(now: float) -> None:
        if now >= next_burst[0]:
            jammer.send(bytes(rng.randrange(256) for _ in range(40)))
            next_burst[0] = now + rng.expovariate(1.0 / period)
        original_tick(now)

    network.tick = tick


def loss_one(args) -> dict:
    kind, seed, drop, duty = args
    clock, network, controller, targets, where, ledger = _setup(kind, seed, drop)
    if duty:
        _jam(network, clock, where, seed, duty)
    for target in targets:
        sb.opened(clock, network, controller, target, 60.0)  # commands go by flood if it does not open
        for value in range(8):
            ledger.command(clock, network, controller, target, value)
        network.sessions.close_session(target)
    return ledger.audit()


def chaos_one(args) -> dict:
    kind, seed = args
    clock, network, controller, targets, where, ledger = _setup(kind, seed, 0.20)
    _jam(network, clock, where, seed, 0.20)
    rng = random.Random(seed + 7)
    nodes = sorted(network.devices)
    attacker = None
    for target in targets[:2]:
        session, _ = sb.opened(clock, network, controller, target, 60.0)
        position = network.medium.port(target).position
        if attacker is None:
            attacker = network.medium.add(ATTACKER, (position[0] + 20.0, position[1] + 20.0))
        else:
            network.medium.move(ATTACKER, (position[0] + 20.0, position[1] + 20.0))
        dead: list[int] = []
        for value in range(12):
            mark = len(network.medium.log)
            # a node (every fourth time the target) power-cycles for 2 s
            victim = target if value % 4 == 3 else rng.choice(nodes)
            network.set_power(victim, False)
            sb.run(clock, network, controller, 2.0)
            network.set_power(victim, True)
            # two relays on the path die for good
            if value in (4, 8) and session.route:
                alive = [node for node in session.route if node not in dead]
                if alive:
                    dead.append(alive[len(alive) // 2])
                    network.set_power(dead[-1], False)
            def repair(dead=dead):
                # Layer 3: still confirming means the node is cut off (seed 11:
                # both dead relays were its only way out); the crew restores them.
                for node in dead:
                    network.set_power(node, True)
                dead.clear()

            ledger.command(clock, network, controller, target, value, repair=repair)
            # the outside radio replays what it heard, and bit-flipped copies
            heard = [data for _, sender, data in network.medium.log[mark:] if data and sender not in (rb.OUTSIDER, ATTACKER)]
            for data in rng.sample(heard, min(4, len(heard))):
                attacker.send(data)
                flipped = bytearray(data)
                flipped[rng.randrange(len(flipped))] ^= 1 << rng.randrange(8)
                attacker.send(bytes(flipped))
                sb.run(clock, network, controller, 0.2)
        for node in dead:
            network.set_power(node, True)
        network.sessions.close_session(target)
    return ledger.audit()


def restart_one(args) -> dict:
    kind, seed = args
    clock, network, controller, targets, _, ledger = _setup(kind, seed, 0.20)
    target = targets[0]
    sb.opened(clock, network, controller, target, 60.0)
    for value in range(4):
        ledger.command(clock, network, controller, target, value)
    # One more goes out and the controller restarts while it is on the air.
    # Its id: the counter's next value, read before the gateway's probes and
    # keepalives (they draw on the same counter) can move it.
    sequence = controller._id_ledger.next_sequence[target]
    network.sessions.request(target, ACTION, 4)
    ledger.issued[(target, controller.epoch << 8 | sequence)] = (ACTION, 4)  # it may run: allowed
    sb.run(clock, network, controller, random.Random(seed).uniform(0.2, 3.0))
    fresh = network.controller(
        network.sessions, clock=clock, timeout=8.0, max_retries=2,
        confirm_window=DEFAULT_CONFIRM_WINDOW, epoch=(controller.epoch + 1) & 0xFF,
    )
    network.sessions.attach(fresh)
    for value in range(5, 13):
        ledger.command(clock, network, fresh, target, value)
    return ledger.audit()


def outage_one(args) -> dict:
    """Layer 3: the target loses power for 10–60 min right after a command goes out, then comes back.

    Every command must still end certain: run before the cut → DONE, never
    arrived → NOT_RUN, and a copy that arrives late never runs.
    """

    kind, seed = args
    clock, network, controller, targets, _, ledger = _setup(kind, seed, 0.20)
    rng = random.Random(seed + 11)
    for target in targets[:2]:
        sb.opened(clock, network, controller, target, 60.0)
        for value in range(4):
            def cut(target=target):
                sb.run(clock, network, controller, rng.uniform(0.0, 3.0))
                network.set_power(target, False)
                sb.run(clock, network, controller, rng.uniform(600.0, 3600.0))
                network.set_power(target, True)

            ledger.command(clock, network, controller, target, value, cut if value % 2 == 0 else None)
        network.sessions.close_session(target)
    return ledger.audit()


def dormant_one(args) -> dict:
    kind, seed = args
    from . import wake_bench as wb

    rng, clock, network, _ = wb._build(kind, seed, {"idle_timeout": 60.0, "lease": 15})
    network.medium.drop = 0.30
    controller = network.controller(
        network.sessions, clock=clock, timeout=8.0, max_retries=2, confirm_window=DEFAULT_CONFIRM_WINDOW
    )
    network.sessions.attach(controller)
    ledger = Ledger(network)
    depth = wb._depths(network.medium, network.controller_id)
    far = sorted(node for node, d in depth.items() if node in network.devices and d >= 2)
    for target in rng.sample(far, min(2, len(far))):
        sb.opened(clock, network, controller, target, 60.0)
        for value in range(6):
            ledger.command(clock, network, controller, target, value)
        network.sessions.close_session(target)
    return ledger.audit()


# ------------------------------------------------------------------- report

BAD_LABELS = {
    "dup": "重复执行",
    "forged": "执行了没发过的命令",
    "tampered": "执行的动作 / 值被篡改",
    "wrong_result": "回给控制端的结果与设备结果不符",
    "done_not_run": "报已执行但设备没执行",
    "not_run_ran": "报确认未执行但设备执行了",
    "no_outcome": "控制端没给结论",
}


def _merge(rows: list[dict]) -> dict:
    bad = {key: sum(row["bad"][key] for row in rows) for key in BAD_LABELS}
    counts = {key: sum(row["counts"][key] for row in rows) for key in rows[0]["counts"]} if rows else {}
    times = sorted(t for row in rows for t in row["times"])
    confirm_times = sorted(t for row in rows for t in row["confirm_times"])
    return {"bad": bad, "counts": counts, "commands": sum(row["commands"] for row in rows), "times": times,
            "confirm_times": confirm_times, "epoch_moves": sum(row.get("epoch_moves", 0) for row in rows)}


def main(argv: list[str]) -> None:
    multiprocessing.set_start_method("fork", force=True)
    out = Path(__file__).with_name("STRESS_REPORT.md")
    if "--out" in argv:
        out = Path(argv[argv.index("--out") + 1])
        argv = [a for i, a in enumerate(argv) if a != "--out" and (i == 0 or argv[i - 1] != "--out")]
    if "--important" in argv:
        rb.IMPORTANT = True  # before the pool forks: every command is an important one
        argv = [a for a in argv if a != "--important"]
    seeds = int(argv[0]) if argv else 8
    began = time.time()
    layouts = ("random40", "grid40")
    cases: list[tuple[str, str, dict]] = []

    with simpool.pool() as pool:
        def run(fn, jobs):
            return [row for row in pool.map(fn, jobs) if row]

        for kind in layouts:
            for drop in (0.0, 0.10, 0.30, 0.40, 0.50):
                cases.append((f"丢包 {drop:.0%}", kind, _merge(run(loss_one, [(kind, s, drop, 0.0) for s in range(1, seeds + 1)]))))
            for duty in (0.30, 0.50):
                cases.append((f"干扰 {duty:.0%} + 丢包 10%", kind,
                              _merge(run(loss_one, [(kind, s, 0.10, duty) for s in range(1, seeds + 1)]))))
            cases.append(("混沌：丢包 20% + 干扰 20% + 重启 + 中继断电 + 重放 / 篡改", kind,
                          _merge(run(chaos_one, [(kind, s) for s in range(1, seeds + 1)]))))
            cases.append(("控制端中途重启（新 EPOCH）+ 丢包 20%", kind,
                          _merge(run(restart_one, [(kind, s) for s in range(1, seeds + 1)]))))
            cases.append(("目标断电 10–60 min 后恢复（第 3 层）+ 丢包 20%", kind,
                          _merge(run(outage_one, [(kind, s) for s in range(1, seeds + 1)]))))
        for kind, label in (("random", "random40"), ("grid", "grid40")):
            cases.append(("静默低功耗网络 + 丢包 30%", label, _merge(run(dormant_one, [(kind, s) for s in range(1, seeds + 1)]))))

    violations = sum(sum(case["bad"].values()) for _, _, case in cases)
    commands = sum(case["commands"] for _, _, case in cases)
    unconfirmed = sum(case["counts"].get("unconfirmed", 0) for _, _, case in cases)
    unconfirmed_ran = sum(case["counts"].get("unconfirmed_ran", 0) for _, _, case in cases)
    epoch_moves = sum(case.get("epoch_moves", 0) for _, _, case in cases)
    important = rb.IMPORTANT
    # Ordinary commands: no wrong conclusion (violations 0); the unconfirmed ones are the contract's price.
    # Important commands: no unconfirmed either.
    passed = violations == 0 and (unconfirmed == 0 or not important)
    lines = [
        "# CairnMesh 极限条件完整性测试报告",
        "",
        f"> 生成：`python -m cairnmesh.stress_bench {seeds}`（{time.strftime('%Y-%m-%d %H:%M')}，用时 {(time.time() - began) / 60:.0f} 分钟）。"
        f"模拟验证，非真机；每格 {seeds} 个种子；状态查询（Q18）与持续确认（第 3 层）开启。",
        "> 每条命令逐条核对：控制端发了什么、设备实际执行了什么（动作、值、结果）、控制端最后被告知什么。动作对每第 4 个 VALUE 回 ERROR，"
        "结果一旦串到别的命令上就能查出来。",
        "",
        f"## 结论（{'重要指令' if important else '普通指令'}）：{commands} 条命令，错误结论（违规）{violations} 次，"
        f"未确认 {unconfirmed} 条（其中实际已执行 {unconfirmed_ran}），EPOCH 前移 {epoch_moves} 次"
        + ("（全部场景通过）" if passed else "（未通过，见下表）"),
        "",
        "| 场景 | 布局 | 命令 | 违规 | 自身窗口内回答 | 查询补确认 | 确认未执行 | 未确认（实际已执行） | 时延中位 | p95 | 查询得结论最长 |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for name, kind, case in cases:
        c, times, n = case["counts"], case["times"], case["commands"]
        bad = [f"{BAD_LABELS[key]} {count}" for key, count in case["bad"].items() if count]
        median = f"{statistics.median(times):.2f} s" if times else "—"
        p95 = f"{times[max(0, int(len(times) * 0.95) - 1)]:.2f} s" if times else "—"
        slowest = f"{case['confirm_times'][-1] / 60:.1f} min" if case["confirm_times"] else "—"
        lines.append(
            f"| {name} | {kind} | {n} | {'❌ ' + '、'.join(bad) if bad else '✅ 0'} | "
            f"{c.get('direct', 0)}（{100 * c.get('direct', 0) / n:.1f} %） | {c.get('confirmed', 0)} | {c.get('not_run', 0)} | "
            f"{c.get('unconfirmed', 0)}（{c.get('unconfirmed_ran', 0)}） | {median} | {p95} | {slowest} |"
        )
    lines += [
        "",
        "**检查项（每项都必须是 0）：** " + "；".join(BAD_LABELS.values()) + "。",
        "",
        ("**重要指令验收**（`--important`）：控制端不再自己放弃，查询窗口（90 s）过后转入“确认中”，按节奏一直查到有结论。"
         f"“未确认”只在确认中超过 {CONFIRM_LIMIT / 3600:.0f} h（模拟）时由测试代操作员放弃才会出现，目标是 0；括号里是其中实际已执行的。"
         if important else
         "**普通指令验收**（默认）：查询窗口（90 s）过后转入“确认中”，到首发后 `confirm_limit`（默认 30 min）"
         "由控制端自己结案为“未确认（可能已执行）”，不自动重发；验收口径是**零错误结论**（上面各检查项为 0），"
         "“未确认”条数不作为失败，括号里是其中实际已执行的。重要指令另跑 `--important`，目标仍是未确认 0。"),
        "已知不覆盖：控制端在同一秒内重启（EPOCH 相同，靠随机 SEQ 起点把撞号概率压到约 4/256，spec 4.2）。",
    ]
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"{commands} 条命令，{'重要指令' if important else '普通指令'}，违规 {violations} 次，未确认 {unconfirmed} 条"
          f"（实际已执行 {unconfirmed_ran}），EPOCH 前移 {epoch_moves} 次；报告：{out}")


if __name__ == "__main__":
    main(sys.argv[1:])
