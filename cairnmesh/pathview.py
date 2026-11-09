"""Where did a message get stuck? A hop-by-hop view of a session path.

``PathView`` is what a control-side display needs: per hop the link
quality (at probe time and as measured on the last message, both ways), how
often each direction had to repeat, and where the message stopped. It is
built from one of two sources:

* ``gateway_view``: only what the gateway knows for free, without a single
  extra transmission: the probe's link measurements, its own first hop
  (repeats, and the strength of the copy that acknowledged it), ROUTE_ERROR
  (where an outbound frame got stuck), and the time taken against the
  model, turned into a number of repeats somewhere on the path.
* ``records_view``: the relays' own ``MeshRelay.route_log`` records of one
  message. On hardware these reach the gateway only with a TRACE
  (``trace_view``); in the simulator they can be read
  directly (``mesh_sim.MeshNetwork.route_records``), which also checks that
  a trace would be worth its cost.

``render_path`` turns either into text for the console or the TUI.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Mapping, Optional

from .frames import TRACE_OTHER, TRACE_UNSEEN, LinkQuality, TraceRecord
from .mesh import node_report

if TYPE_CHECKING:  # pragma: no cover
    from .gateway import Delivery, Gateway
    from .mesh import HopRecord

#: Hop states.
OK, STUCK, UNREACHED, UNKNOWN, IN_PROGRESS = "ok", "stuck", "unreached", "unknown", "in_progress"
#: A trace's answer skipped a node, so nothing is known of it (user, 2026-10-19:
#: shown apart from a node that reported not receiving).
NO_REPORT = "no_report"


@dataclass
class HopView:
    """One hop: ``sender`` → ``receiver`` on the way out (back is the reverse)."""

    number: int
    sender: int
    receiver: int
    #: Measured on the probe that set the path up (outbound; links taken as symmetric).
    probe: Optional[LinkQuality] = None
    #: On the last message: RSSI at ``receiver`` going out, at ``sender`` coming back.
    rssi_out: Optional[float] = None
    rssi_back: Optional[float] = None
    #: Repeats this hop needed for the last message, each way (``None``: not known).
    repeats_out: Optional[int] = None
    repeats_back: Optional[int] = None
    state: str = UNKNOWN
    note: str = ""
    #: ROUTE_ERRORs counted on this hop over the session.
    errors: int = 0


@dataclass
class PathView:
    target: int
    session_state: str
    hops: list[HopView]
    #: Where the per-hop figures come from.
    source: str
    expected: Optional[float] = None
    worst: Optional[float] = None
    realtime_target: Optional[float] = None
    delivery: Optional["Delivery"] = None
    notes: list[str] = field(default_factory=list)
    #: Shown as ``G``.
    gateway_id: Optional[int] = None

    @property
    def stuck_at(self) -> Optional[HopView]:
        return next((hop for hop in self.hops if hop.state == STUCK), None)

    def name(self, node: int) -> str:
        # Hex like the node table (0x0A), without the prefix: the panel is narrow.
        return "G" if node == self.gateway_id else f"{node:02X}"

    def path_text(self, route: tuple[int, ...]) -> str:
        return "→".join(self.name(node) for node in (self.gateway_id, *route, self.target))


def _hops(gateway_id: int, route: tuple[int, ...], target: int, links) -> list[HopView]:
    path = (gateway_id, *route, target)
    hops = []
    for number in range(1, len(path)):
        probe = links[number - 1] if links and len(links) >= number else None
        hops.append(HopView(number, path[number - 1], path[number], probe=probe))
    return hops


def gateway_view(gateway: "Gateway", target: int) -> Optional[PathView]:
    """What the gateway alone knows about ``target``'s session (no extra air time)."""

    session = gateway.session(target)
    if session is None:
        return None
    hops = _hops(gateway.controller_id, session.route, target, session.links)
    view = PathView(
        target,
        session.state,
        hops,
        source="网关自测（零开销）：只知第一跳与断点，中间各跳需追踪帧",
        expected=session.latency.expected if session.latency else None,
        worst=session.latency.worst if session.latency else None,
        realtime_target=gateway.model.target,
        gateway_id=gateway.controller_id,
    )
    for hop in hops:
        hop.errors = session.hop_errors.get((hop.sender, hop.receiver), 0)
    delivery = session.deliveries[-1] if session.deliveries else None
    view.delivery = delivery
    if hops:
        first = hops[0]
        if session.first_hop_rssi is not None:
            first.rssi_back = session.first_hop_rssi[0]
        if delivery is not None and delivery.route == session.route:
            first.repeats_out = max(0, delivery.first_hop_sends - 1)
            first.state = OK if delivery.first_hop_ack is not None else IN_PROGRESS
    if delivery is not None and delivery.broken_at is not None:
        sender, receiver = delivery.broken_at
        hop = next((h for h in hops if (h.sender, h.receiver) == delivery.broken_at), None)
        if hop is not None and delivery.broken_route == session.route:
            hop.state = STUCK
            hop.note = f"{view.name(sender)} 报告到不了 {view.name(receiver)}"
        else:
            view.notes.append(
                f"该命令卡在 {view.name(sender)}→{view.name(receiver)}"
                f"（当时的路径 {view.path_text(delivery.broken_route or ())}），已绕开"
            )
    if delivery is not None and delivery.inferred_repeats:
        view.notes.append(f"由耗时推算：途中约重发 {delivery.inferred_repeats} 次（位置需追踪帧）")
    return view


def records_view(
    gateway_id: int,
    route: tuple[int, ...],
    target: int,
    records: Mapping[int, Mapping[tuple[int, int, int, int, int], "HopRecord"]],
    sequence: int,
    epoch: int,
    links=None,
    source: str = "各节点本地记录",
) -> PathView:
    """Per-hop truth for one command from every node's ``route_log``.

    Each log is first turned into the node's ``TraceRecord`` with
    ``mesh.node_report``, exactly as the node would answer a TRACE, so this
    view and ``trace_view`` can only differ by what was lost on the air.
    """

    reports = {
        node: node_report(dict(log), gateway_id, target, sequence, epoch)
        for node, log in records.items()
    }
    return reports_view(gateway_id, route, target, reports, links, source)


def trace_view(gateway: "Gateway", target: int, delivery: Optional["Delivery"] = None) -> Optional[PathView]:
    """The last traced command to ``target`` (or ``delivery``), as its TRACE answered."""

    session = gateway.session(target)
    if session is None:
        return None
    if delivery is None:
        delivery = next((d for d in reversed(session.deliveries) if d.trace is not None), None)
    if delivery is None or delivery.trace is None:
        return None
    trace = delivery.trace
    view = reports_view(
        gateway.controller_id,
        trace.route,
        target,
        trace.records,
        session.links if trace.route == session.route else None,
        "追踪帧（各节点回报；重发 3 表示 ≥ 3）",
        reported=True,
    )
    view.delivery = delivery
    view.session_state = session.state
    for hop in view.hops:
        hop.errors = session.hop_errors.get((hop.sender, hop.receiver), 0)
    return view


def reports_view(
    gateway_id: int,
    route: tuple[int, ...],
    target: int,
    reports: Mapping[int, "TraceRecord"],
    links=None,
    source: str = "",
    reported: bool = False,
) -> PathView:
    """Per-hop view from one ``TraceRecord`` per node (a trace's answer, or the truth).

    For hop ``a → b``: ``a``'s record gives the outbound repeats and outcome,
    ``b``'s the strength the request came in with; ``b``'s record gives the
    answer's repeats back, ``a``'s the strength it came in with. Outbound,
    the first hop whose sender has the request and whose receiver does not is
    where it stuck; failing that, inbound from the target back, the first hop
    whose sender has the answer and whose receiver does not.

    ``reported``: ``reports`` are a trace's answer, which may skip a node
    (it passed it by): a node missing from them is ``NO_REPORT``, never
    taken for one that did not receive.
    """

    hops = _hops(gateway_id, route, target, links)
    view = PathView(target, "", hops, source=source, gateway_id=gateway_id)
    blank = TraceRecord()

    def of(node: int) -> "TraceRecord":
        return reports.get(node, blank)

    silent = {node for node in (gateway_id, *route, target) if node not in reports} if reported else set()
    out_stuck = False
    for hop in hops:
        sent, got = of(hop.sender), of(hop.receiver)
        if not out_stuck and hop.receiver in silent:
            hop.state, hop.note = NO_REPORT, f"{view.name(hop.receiver)} 未回报（追踪回答没经过它）"
            continue
        if not out_stuck and hop.sender in silent:
            if got.out_state != TRACE_UNSEEN:
                hop.state = OK
                hop.repeats_out = None
                hop.rssi_out = got.rssi_out
            else:
                hop.state, out_stuck = NO_REPORT, True
                hop.note = f"{view.name(hop.receiver)} 没收到；{view.name(hop.sender)} 未回报，不知道它有没有发"
            continue
        hop.rssi_out = got.rssi_out
        hop.rssi_back = sent.rssi_back
        if sent.out_state != TRACE_UNSEEN:
            hop.repeats_out = sent.out_repeats
        if got.back_state != TRACE_UNSEEN:
            hop.repeats_back = got.back_repeats
        if out_stuck or sent.out_state == TRACE_UNSEEN:
            hop.state = UNREACHED
        elif got.out_state == TRACE_UNSEEN:
            if sent.out_state == TRACE_OTHER:
                hop.state = IN_PROGRESS
            else:
                hop.state, out_stuck = STUCK, True
                hop.note = f"{view.name(hop.sender)} 发了 {sent.out_repeats + 1} 次，{view.name(hop.receiver)} 没收到"
        else:
            hop.state = OK
    if out_stuck:
        return view
    for position in range(len(hops) - 1, -1, -1):
        hop = hops[position]
        back_sent, back_got = of(hop.receiver), of(hop.sender)
        if back_sent.back_state == TRACE_UNSEEN:
            continue  # the answer has not left the target yet (or never will)
        if hop.sender in silent:
            continue  # nothing known of it; a node nearer the gateway tells
        if back_got.back_state == TRACE_UNSEEN and back_sent.back_state != TRACE_OTHER:
            hop.state = STUCK
            hop.note = (
                f"回答在 {view.name(hop.receiver)}→{view.name(hop.sender)} 丢了"
                f"（{view.name(hop.receiver)} 发了 {back_sent.back_repeats + 1} 次）"
            )
            for nearer in hops[:position]:
                nearer.state, nearer.note = UNREACHED, "回答未到"
            break
    return view


def _fmt(value: Optional[float], width: int, digits: int = 0) -> str:
    if value is None:
        return "?".rjust(width)
    return f"{value:.{digits}f}".rjust(width)


_STATE_TEXT = {
    OK: "✓", STUCK: "✖ 卡住", UNREACHED: "· 未到", UNKNOWN: "?", IN_PROGRESS: "… 在途", NO_REPORT: "? 未回报"
}


def render_path(view: PathView) -> str:
    """Hop table plus a summary of the last command, for a monospace display."""

    lines = []
    head = f"节点 {view.target}"
    if view.session_state:
        head += f" · 会话 {view.session_state}"
    head += f" · {len(view.hops)} 跳"
    if view.expected is not None:
        head += f" · 理论往返 {view.expected:.2f} s（最坏 {view.worst:.2f} s）"
        if view.realtime_target is not None and view.worst > view.realtime_target:
            head += f" · 最坏超过 {view.realtime_target:g} s 目标"
    lines.append(head)
    lines.append(f"数据：{view.source}")
    lines.append("跳  链路        探测RSSI/SNR  去程RSSI 重发  回程RSSI 重发  断链  状态")
    for hop in view.hops:
        probe = (
            f"{hop.probe.rssi:4d}/{hop.probe.snr_db:5.1f}" if hop.probe is not None else "   ?/    ?"
        )
        repeats_out = "?" if hop.repeats_out is None else str(hop.repeats_out)
        repeats_back = "?" if hop.repeats_back is None else str(hop.repeats_back)
        link = f"{view.name(hop.sender)}→{view.name(hop.receiver)}"
        state = _STATE_TEXT.get(hop.state, hop.state)
        if hop.note:
            state += f"：{hop.note}"
        lines.append(
            f"{hop.number:2d}  {link:<10}  {probe:>12}  {_fmt(hop.rssi_out, 8)} {repeats_out:>4}"
            f"  {_fmt(hop.rssi_back, 8)} {repeats_back:>4}  {hop.errors:>4}  {state}"
        )
    delivery = view.delivery
    if delivery is not None:
        summary = f"最近命令 seq {delivery.sequence}："
        if delivery.elapsed is None:
            summary += "进行中"
        else:
            summary += f"{'成功' if delivery.ok else '失败'} {delivery.elapsed:.2f} s（期望 {delivery.expected:.2f}）"
        if delivery.broken_at is not None:
            summary += f" · 断在 {view.name(delivery.broken_at[0])}→{view.name(delivery.broken_at[1])}"
        if delivery.fell_back:
            summary += " · 已退回泛洪"
        lines.append(summary)
    lines.extend(view.notes)
    return "\n".join(lines)
