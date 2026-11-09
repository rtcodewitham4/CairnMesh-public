"""Robustness of the protocol stack under faults (slow; not unit tests; no protocol code here).

    python -m cairnmesh.robustness_bench [seeds] [--out PATH]   # ≈ 10–20 min; writes a Markdown report

Deployment-scale layouts (``session_bench`` a5 layouts, link-budget model):
unplanned random 40 (550 m square, gateway in the middle) and a planned
5 × 8 grid. Always-awake network, routed sessions to the farthest nodes.
Every scenario counts commands delivered, commands **executed more than
once** (at-most-once must hold), and the latency median / p95:

* ``loss``: extra random frame loss 0 / 5 / 10 / 20 / 30 %;
* ``relay_death``: two relays on the session's path switched off mid-session;
* ``churn``: a random node (now and then the target) power-cycles every few seconds;
* ``jammer``: a radio near the gateway sends garbage 10 % / 30 % of the time;
* ``replay``: an outside radio replays captured commands (at once and 130 s
  later: the device's record and the FCNT window both stand in the way) and
  bit-flipped copies;

Commands whose own retries get no answer are followed by the status query
(Q18): the report counts what it confirmed, and checks that every command it
called "not run" really never ran.
* ``switching``: back-to-back sessions to random targets, 3 commands each.

Results go to ``ROBUSTNESS_REPORT.md`` next to this file unless ``--out``.
``--important`` sends every command as an important one: the controller then keeps confirming,
and the "unconfirmed" column must stay 0; by default they are ordinary ones, ended UNCONFIRMED by the
controller at its 30 min ``confirm_limit`` ("may have run"), and the acceptance is no wrong conclusion.
"""

from __future__ import annotations

import multiprocessing
import random
import statistics
import sys
import time
from pathlib import Path
from typing import Callable, Optional

from . import simpool
from . import session_bench as sb
from .controller import DEFAULT_CONFIRM_WINDOW, DONE, INTERRUPTED, NOT_RUN, UNCONFIRMED

COMMAND_WAIT = 100.0  # longer than the controller's confirm window (90 s, Q18 status query)
#: Layer 3: how long a command may stay CONFIRMING (simulated) before the
#: bench abandons it in the operator's place: 30 min of 2 min rounds and
#: three of 30 min. Every target stays alive, so it must never come to that.
CONFIRM_LIMIT = 2 * 3600.0
#: The commands of these benches are ordinary: the controller ends them itself at its
#: ``confirm_limit`` (30 min) as UNCONFIRMED ("may have run"); the acceptance is **no wrong conclusion**, not no
#: unconfirmed. ``--important`` (stress_bench) sends them all as important ones: confirmed on, unconfirmed must be 0.
IMPORTANT = False
OUTSIDER = 0xEE  # a radio on the medium that is not part of the network


def _setup(kind: str, seed: int, drop: float):
    positions, extra, targets, where = sb._a5_layout(kind, seed)
    clock, network, controller = sb.build(
        positions, seed=seed, drop=drop, gateway_options={"idle_timeout": None}, confirm_window=DEFAULT_CONFIRM_WINDOW,
        controller_position=where, record=True, **extra,
    )
    if targets is None:
        targets = sb._far_targets(network, 3)
    return clock, network, controller, targets, where


def _commands(clock, network, controller, target: int, count: int, between: Optional[Callable[[int], None]] = None):
    """``count`` commands to ``target`` one after another.

    Each: ``(ok, elapsed, id, status, queried)``; ``ok`` only for an answer
    within the command's own retries (a result the status query brought
    back counts under ``status`` / ``queried``, not as delivered).
    """

    out = []
    for value in range(count):
        if between is not None:
            between(value)
        future = network.sessions.request(target, 3, value & 0xFF, important=IMPORTANT)
        wait(clock, network, controller, target, future)
        outcome = future.result(timeout=0) if future.done() else None
        ident = controller.epoch << 8 | outcome.sequence if outcome is not None else None
        if outcome is None:
            out.append((False, None, None, None, False))
        else:
            out.append((outcome.ok and not outcome.queried, outcome.elapsed, ident, outcome.status, outcome.queried))
        sb.run(clock, network, controller, 0.3)
    return out


def wait(clock, network, controller, target: int, future, repair=None) -> None:
    """Run until ``future`` is done; a command still CONFIRMING (layer 3) gets ``CONFIRM_LIMIT``, then is abandoned.

    ``repair`` (if any) runs first when it is still confirming: the field
    crew restoring what cut the node off (dead relays), as an operator would
    on a "确认中" notice.
    """

    sb.run(clock, network, controller, COMMAND_WAIT, future.done)
    if not future.done() and controller.confirming():
        if repair is not None:
            repair()
        sb.run(clock, network, controller, CONFIRM_LIMIT, future.done)
        if not future.done():
            controller.abandon(target)


def _duplicates(executed: list[int]) -> int:
    return len(executed) - len(set(executed))


def _unasked(executed: list[int], results) -> int:
    """Executions of message ids the controller never issued (forged or garbage taken for a command)."""

    issued = {r[2] for r in results if r[2] is not None}
    return sum(ident not in issued for ident in executed)


def _query_counts(executed: list[int], results) -> dict:
    """What the status query (Q18) made of commands whose own retries got no answer.

    ``not_run_ran`` must be 0: "not run" is a promise. ``unconfirmed_ran``
    are the ones that did run although nobody could say so.
    """

    ran = set(executed)
    ids = lambda status: {r[2] for r in results if r[3] == status and r[4] and r[2] is not None}
    confirmed, not_run, unconfirmed = ids(DONE) | ids(INTERRUPTED), ids(NOT_RUN), ids(UNCONFIRMED)  # INTERRUPTED: final, wire v5
    return {"confirmed": len(confirmed), "not_run": len(not_run), "not_run_ran": len(not_run & ran),
            "unconfirmed": len(unconfirmed), "unconfirmed_ran": len(unconfirmed & ran)}


def _add(total: dict, part: dict) -> None:
    for key, value in part.items():
        total[key] = total.get(key, 0) + value


def _query_text(data: list[dict]) -> str:
    q: dict = {}
    for d in data:
        _add(q, d.get("query", {}))
    return (f"补确认 {q.get('confirmed', 0)} · 确认未执行 {q.get('not_run', 0)} · "
            f"未确认 {q.get('unconfirmed', 0)}（实际已执行 {q.get('unconfirmed_ran', 0)}）")


def _not_run_ran(data: list[dict]) -> int:
    return sum(d.get("query", {}).get("not_run_ran", 0) for d in data)


# ------------------------------------------------------------------ scenarios


def loss_one(args) -> dict:
    kind, seed, drop = args
    clock, network, controller, targets, _ = _setup(kind, seed, drop)
    results, dups, opened, query = [], 0, 0, {}
    for target in targets:
        executed = sb.record_executions(network, target)
        session, _ = sb.opened(clock, network, controller, target, 60.0)
        if session.state != "open":
            results += [(False, None, None, None, False)] * 10
            continue
        opened += 1
        mine = _commands(clock, network, controller, target, 10)
        results += mine
        dups += _duplicates(executed)
        _add(query, _query_counts(executed, mine))
        network.sessions.close_session(target)
    return {"results": results, "dups": dups, "opened": opened, "sessions": len(targets), "query": query}


def relay_death_one(args) -> dict:
    kind, seed, drop = args
    clock, network, controller, targets, _ = _setup(kind, seed, drop)
    target = targets[0]
    executed = sb.record_executions(network, target)
    session, _ = sb.opened(clock, network, controller, target, 60.0)
    if session.state != "open" or len(session.route) < 2:
        return {}
    dead: list[int] = []

    def kill(value: int) -> None:
        if value in (3, 6):
            route = [node for node in session.route if node not in dead]
            if route:
                victim = route[len(route) // 2]
                dead.append(victim)
                network.set_power(victim, False)

    results = _commands(clock, network, controller, target, 10, kill)
    sb.run(clock, network, controller, 30.0, lambda: session.state == "open")
    recovered = session.state == "open" and not set(dead) & set(session.route)
    return {"results": results, "dups": _duplicates(executed), "recovered": recovered}


def churn_one(args) -> dict:
    kind, seed, drop = args
    clock, network, controller, targets, _ = _setup(kind, seed, drop)
    target = targets[0]
    executed = sb.record_executions(network, target)
    session, _ = sb.opened(clock, network, controller, target, 60.0)
    if session.state != "open":
        return {}
    rng = random.Random(seed + 77)
    nodes = sorted(network.devices)
    rebooted_target = 0

    def reboot(value: int) -> None:
        nonlocal rebooted_target
        # Every command: one node goes down for 2 s; every fifth time the target itself.
        victim = target if value % 5 == 4 else rng.choice(nodes)
        rebooted_target += victim == target
        network.set_power(victim, False)
        sb.run(clock, network, controller, 2.0)
        network.set_power(victim, True)

    results = _commands(clock, network, controller, target, 20, reboot)
    return {"results": results, "dups": _duplicates(executed), "target_reboots": rebooted_target}


def jammer_one(args) -> dict:
    kind, seed, drop, duty = args
    clock, network, controller, targets, where = _setup(kind, seed, drop)
    jammer = network.medium.add(OUTSIDER, (where[0] + 40.0, where[1] + 30.0))
    rng = random.Random(seed + 99)
    garbage_len = 40
    airtime = network.medium.airtime_of(garbage_len)
    period = airtime / duty
    next_burst = [clock.now + rng.uniform(0, period)]
    jammed = [0]
    original_tick = network.tick

    def tick(now: float) -> None:
        if now >= next_burst[0]:
            jammed[0] += 1
            jammer.send(bytes(rng.randrange(256) for _ in range(garbage_len)))
            next_burst[0] = now + rng.expovariate(1.0 / period)
        original_tick(now)

    network.tick = tick
    results, dups, opened, unasked, query = [], 0, 0, 0, {}
    executions = {node: sb.record_executions(network, node) for node in network.devices}
    for target in targets:
        session, _ = sb.opened(clock, network, controller, target, 60.0)
        if session.state != "open":
            results += [(False, None, None, None, False)] * 10
            continue
        opened += 1
        mine = _commands(clock, network, controller, target, 10)
        results += mine
        dups += _duplicates(executions[target])
        _add(query, _query_counts(executions[target], mine))
        network.sessions.close_session(target)
    unasked = sum(_unasked(executed, results) for executed in executions.values())
    return {"results": results, "dups": dups, "opened": opened, "sessions": len(targets), "unasked": unasked,
            "query": query, "jammed": jammed[0]}


def replay_one(args) -> dict:
    kind, seed, drop = args
    clock, network, controller, targets, where = _setup(kind, seed, 0.0)
    target = targets[0]
    executed = sb.record_executions(network, target)
    session, _ = sb.opened(clock, network, controller, target, 60.0)
    if session.state != "open":
        return {}
    mark = len(network.medium.log)
    results = _commands(clock, network, controller, target, 3)
    captured = [data for _, sender, data in network.medium.log[mark:] if data and data[0] & 0x1F == 0x01]
    before = list(executed)
    # The attacker sits next to the target and replays everything it heard.
    position = network.medium.port(target).position
    attacker = network.medium.add(OUTSIDER, (position[0] + 20.0, position[1] + 20.0))

    def replay(frames: list[bytes]) -> None:
        for data in frames:
            attacker.send(data)
            sb.run(clock, network, controller, 0.5)

    replay(captured)  # at once
    sb.run(clock, network, controller, 130.0)
    replay(captured)  # 130 s later: the record is still kept, and the FCNT window refuses it too
    flipped = []
    for data in captured:
        raw = bytearray(data)
        raw[len(raw) // 2] ^= 0x01
        flipped.append(bytes(raw))
    replay(flipped)
    sb.run(clock, network, controller, 5.0)
    extra = len(executed) - len(before)
    return {"results": results, "dups": _duplicates(executed), "replayed": 2 * len(captured) + len(flipped), "extra": extra}


def switching_one(args) -> dict:
    kind, seed, drop = args
    clock, network, controller, _, _ = _setup(kind, seed, drop)
    rng = random.Random(seed + 5)
    reachable = [node for node in network.devices if network.reachable(node)]
    results, dups, opens, failed = [], 0, [], 0
    for _ in range(8):
        target = rng.choice(reachable)
        executed = sb.record_executions(network, target)
        session, taken = sb.opened(clock, network, controller, target, 60.0)
        if session.state != "open":
            failed += 1
            results += [(False, None, None, None, False)] * 3
            continue
        opens.append(taken)
        results += _commands(clock, network, controller, target, 3)
        dups += _duplicates(executed)
        network.sessions.close_session(target)
    return {"results": results, "dups": dups, "opens": opens, "failed_opens": failed}


# ------------------------------------------------------------------- report


def _summary(rows: list[dict]) -> dict:
    results = [r for row in rows for r in row.get("results", [])]
    times = sorted(r[1] for r in results if r[0] and r[1] is not None)
    ok = sum(r[0] for r in results)
    return {
        "delivered": f"{ok}/{len(results)}（{100 * ok / len(results):.1f} %）" if results else "—",
        "rate": ok / len(results) if results else float("nan"),
        "dups": sum(row.get("dups", 0) for row in rows),
        "median": f"{statistics.median(times):.2f} s" if times else "—",
        "p95": f"{times[max(0, int(len(times) * 0.95) - 1)]:.2f} s" if times else "—",
        "runs": len([row for row in rows if row]),
    }


def main(argv: list[str]) -> None:
    multiprocessing.set_start_method("fork", force=True)
    out = Path(__file__).with_name("ROBUSTNESS_REPORT.md")
    if "--out" in argv:
        out = Path(argv[argv.index("--out") + 1])
        argv = [a for i, a in enumerate(argv) if a != "--out" and (i == 0 or argv[i - 1] != "--out")]
    global IMPORTANT
    if "--important" in argv:
        IMPORTANT = True  # before the pool forks: every command is an important one (confirmed on, never ended by the controller)
        argv = [a for a in argv if a != "--important"]
    seeds = int(argv[0]) if argv else 8
    began = time.time()
    lines: list[str] = []
    verdicts: list[tuple[str, bool, str]] = []
    layouts = ("random40", "grid40")

    def table(title: str, header: list[str], rows: list[list[str]]) -> None:
        lines.extend([f"### {title}", "", "| " + " | ".join(header) + " |", "|" + "---|" * len(header)])
        lines.extend("| " + " | ".join(row) + " |" for row in rows)
        lines.append("")

    with simpool.pool() as pool:
        def run(fn, jobs):
            return list(pool.map(fn, jobs))

        # 1. loss
        rows = []
        for kind in layouts:
            for drop in (0.0, 0.05, 0.10, 0.20, 0.30):
                data = run(loss_one, [(kind, s, drop) for s in range(1, seeds + 1)])
                summary = _summary(data)
                opened = sum(d["opened"] for d in data)
                sessions = sum(d["sessions"] for d in data)
                rows.append([kind, f"{drop:.0%}", f"{opened}/{sessions}", summary["delivered"], str(summary["dups"]),
                             _query_text(data), summary["median"], summary["p95"]])
                verdicts.append((f"丢包 {drop:.0%} {kind} 确认未执行的确实没执行", _not_run_ran(data) == 0, f"{_not_run_ran(data)} 次"))
                if drop <= 0.05:
                    verdicts.append((f"丢包 {drop:.0%} {kind} 送达 ≥ 98 %", summary["rate"] >= 0.98, summary["delivered"]))
                verdicts.append((f"丢包 {drop:.0%} {kind} 不重复执行", summary["dups"] == 0, f"{summary['dups']} 次"))
        table("1. 额外随机丢包", ["布局", "丢包", "会话打开", "送达", "重复执行", "自身重试无回答的命令²", "时延中位", "p95"], rows)
        lines.append("² 命令自己的重试（≤ 25 s）没等到回答后，控制端查询它是否执行（Q18，到首发后 90 s 为止）：“补确认”= 查到已执行；"
                     "“确认未执行”= 设备保证不会再执行；“未确认”= 查询也没回答，括号里是其中实际已执行的。送达与时延只算命令自身窗口内的回答。"
                     + ("本次全部按**重要指令**发出（`--important`）：控制端一直确认到有结论，未确认应为 0。" if IMPORTANT else
                        "本次全部按**普通指令**发出（默认）：确认到首发后 `confirm_limit`（30 min）由控制端结案为“未确认（可能已执行）”，"
                        "验收口径是零错误结论（“确认未执行的确实没执行”等判定为 0 次），未确认条数不作失败。"))
        lines.append("")

        # 2. relay death
        rows = []
        for kind in layouts:
            data = [d for d in run(relay_death_one, [(kind, s, 0.05) for s in range(1, seeds + 1)]) if d]
            summary = _summary(data)
            recovered = sum(d["recovered"] for d in data)
            rows.append([kind, str(summary["runs"]), summary["delivered"], str(summary["dups"]), f"{recovered}/{len(data)}",
                         summary["median"], summary["p95"]])
            verdicts.append((f"路径中继断电 {kind} 不重复执行", summary["dups"] == 0, f"{summary['dups']} 次"))
            verdicts.append((f"路径中继断电 {kind} 送达 ≥ 90 %", summary["rate"] >= 0.90, summary["delivered"]))
        table("2. 会话中途路径上两个中继断电（5 % 丢包，每会话 10 条命令，第 4、7 条前各断一个）",
              ["布局", "有效运行", "送达", "重复执行", "最后的路由不含坏中继¹", "时延中位", "p95"], rows)
        lines.append("¹ 不含坏中继不是必须的：下一跳听到更远一跳时会越跳接管、路由保持不变（spec 11.5），命令照样送达。")
        lines.append("")

        # 3. churn
        rows = []
        for kind in layouts:
            data = [d for d in run(churn_one, [(kind, s, 0.05) for s in range(1, seeds + 1)]) if d]
            summary = _summary(data)
            reboots = sum(d["target_reboots"] for d in data)
            rows.append([kind, str(summary["runs"]), summary["delivered"], str(summary["dups"]), str(reboots),
                         summary["median"], summary["p95"]])
            verdicts.append((f"节点反复重启 {kind} 不重复执行", summary["dups"] == 0, f"{summary['dups']} 次（目标重启 {reboots} 次）"))
        table("3. 节点反复重启（5 % 丢包，20 条命令，每条前一个随机节点断电 2 s，每第 5 条是目标自己）",
              ["布局", "有效运行", "送达", "重复执行", "目标重启次数", "时延中位", "p95"], rows)

        # 4. jammer
        rows = []
        for kind in layouts:
            for duty in (0.10, 0.30):
                data = run(jammer_one, [(kind, s, 0.0, duty) for s in range(1, seeds + 1)])
                summary = _summary(data)
                opened = sum(d["opened"] for d in data)
                sessions = sum(d["sessions"] for d in data)
                bogus = sum(d["unasked"] for d in data)
                rows.append([kind, f"{duty:.0%}", str(sum(d["jammed"] for d in data)), f"{opened}/{sessions}", summary["delivered"], str(summary["dups"]),
                             str(bogus), _query_text(data), summary["median"], summary["p95"]])
                verdicts.append((f"干扰 {duty:.0%} {kind} 不执行伪造命令", bogus == 0, f"{bogus} 次"))
                verdicts.append((f"干扰 {duty:.0%} {kind} 确认未执行的确实没执行", _not_run_ran(data) == 0, f"{_not_run_ran(data)} 次"))
        table("4. 网关旁的干扰源发随机垃圾帧（无额外丢包）",
              ["布局", "占空比", "垃圾帧", "会话打开", "送达", "重复执行", "执行了没发过的命令", "自身重试无回答的命令²", "时延中位", "p95"], rows)

        # 5. replay
        rows = []
        for kind in layouts:
            data = [d for d in run(replay_one, [(kind, s, 0.0) for s in range(1, seeds + 1)]) if d]
            replayed = sum(d["replayed"] for d in data)
            extra = sum(d["extra"] for d in data)
            rows.append([kind, str(len(data)), str(replayed), str(extra)])
            verdicts.append((f"重放 / 篡改 {kind} 不被执行", extra == 0, f"{extra}/{replayed}"))
        table("5. 网外电台重放截获的命令（去重有效期内、外各一次）与逐位篡改的副本",
              ["布局", "有效运行", "注入帧数", "因此多执行"], rows)

        # 6. switching
        rows = []
        for kind in layouts:
            data = run(switching_one, [(kind, s, 0.05) for s in range(1, seeds + 1)])
            summary = _summary(data)
            opens = [t for d in data for t in d["opens"]]
            failed = sum(d["failed_opens"] for d in data)
            rows.append([kind, summary["delivered"], str(summary["dups"]), str(failed),
                         f"{statistics.mean(opens):.1f} s" if opens else "—",
                         f"{max(opens):.1f} s" if opens else "—"])
            verdicts.append((f"连续切换会话 {kind} 送达 ≥ 95 %", summary["rate"] >= 0.95, summary["delivered"]))
        table("6. 连续切换会话（5 % 丢包，8 个随机目标 × 3 条命令）",
              ["布局", "送达", "重复执行", "开不了会话", "打开平均", "打开最长"], rows)

    passed = sum(ok for _, ok, _ in verdicts)
    head = [
        "# CairnMesh 协议栈健壮性测试报告",
        "",
        f"> 生成：`python -m cairnmesh.robustness_bench {seeds}`（{time.strftime('%Y-%m-%d %H:%M')}，用时 {(time.time() - began) / 60:.0f} 分钟）。",
        "> 模拟验证，非真机：链路预算信道模型（数值为手册 / 文献估计值）、常醒网络、按需链路会话；每格 "
        f"{seeds} 个种子。布局：未规划随机 40（550 m 见方、网关居中）与规划网格 5 × 8（每链路 10 dB 余量）。",
        "> “重复执行”= 同一条命令在设备上执行超过一次（违反至多一次）。",
        "",
        f"## 结论：{passed}/{len(verdicts)} 项通过",
        "",
        "| 检查 | 结果 | 数据 |",
        "|---|---|---|",
    ]
    head += [f"| {name} | {'✅ 通过' if ok else '❌ 未通过'} | {detail} |" for name, ok, detail in verdicts]
    head += ["", "## 明细", ""]
    out.write_text("\n".join(head + lines) + "\n", encoding="utf-8")
    print(f"{passed}/{len(verdicts)} 项通过；报告：{out}")


if __name__ == "__main__":
    main(sys.argv[1:])
