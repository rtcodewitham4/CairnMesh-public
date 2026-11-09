"""Routed sessions in the simulator: what the control side can see when things go wrong.

    python -m cairnmesh.session_demo

Two scenes on a ladder of 10 nodes (two rows of five), SF7, 5 % loss per
hop: a session runs a few commands, then a relay on its path is switched
off. For every command it prints the hop table: with what the gateway knows for
free (``gateway_view``), with the relays' own records (``records_view``: the
simulator's ground truth) and, when the command was traced, with what the
nodes answered (``trace_view``; on hardware the only way to see past the
first hop). A command that only needed repeats is traced by hand here;
faults are traced automatically (policy A).
"""

from __future__ import annotations

from .airtime import airtime_for
from .mesh_sim import MeshNetwork, planned
from .pathview import gateway_view, records_view, render_path, trace_view

LADDER = {
    **{node: (80.0 * node, 0.0) for node in range(1, 6)},
    **{10 + node: (80.0 * node, 50.0) for node in range(1, 6)},
}
STEP = 0.005


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _run(clock, network, controller, seconds, until=None) -> None:
    end = clock.now + seconds
    while clock.now < end and not (until is not None and until()):
        clock.now += STEP
        network.tick(clock.now)
        controller.tick(clock.now)


def _show(network, target: int) -> None:
    gateway = network.sessions
    view = gateway_view(gateway, target)
    print(render_path(view))
    delivery = view.delivery
    if delivery is not None:
        truth = records_view(
            gateway.controller_id,
            delivery.route,
            target,
            network.route_records(),
            delivery.sequence,
            delivery.epoch,
            links=gateway.session(target).links,
            source="各节点本地记录（模拟真值；真机需追踪帧）",
        )
        print("\n".join(render_path(truth).splitlines()[1:]))
        traced = trace_view(gateway, target, delivery)
        if traced is not None:
            print("\n".join(render_path(traced).splitlines()[1:]))
        for when, what, node in delivery.events:
            where = "" if node is None else f" @ {node}"
            print(f"    {when - delivery.sent_at:6.3f} s  {what}{where}")
    print()


def main(seed: int = 4, drop: float = 0.05, target: int = 5) -> None:
    clock = _Clock()
    network = MeshNetwork(
        LADDER,
        key=bytes(range(16)),
        clock=clock,
        airtime=airtime_for(7),
        drop=drop,
        seed=seed,
        gateway_options={"idle_timeout": None},
        **planned(),
    )
    controller = network.controller(network.sessions, clock=clock)
    network.sessions.attach(controller)
    session = network.sessions.open_session(target)
    _run(clock, network, controller, 30.0, lambda: session.state == "open")
    print(f"会话建立：{clock.now:.2f} s，路径 G→{'→'.join(map(str, session.route))}→{target}\n")
    _run(clock, network, controller, 0.5)

    for value in range(3):
        future = network.sessions.request(target, 3, value)
        _run(clock, network, controller, 30.0, future.done)
        delivery = network.sessions.deliveries(target)[-1]
        if delivery.anomalous and not delivery.faulty:
            # Repeats only: not traced automatically (policy A traces faults);
            # ask by hand, as the TUI would.
            network.sessions.trace(target, delivery)
            _run(clock, network, controller, 10.0, lambda: delivery.trace is not None)
        _show(network, target)
        _run(clock, network, controller, 0.3)

    dead = session.route[len(session.route) // 2]
    print(f"=== 关掉路径中继 {dead} ===\n")
    network.set_power(dead, False)
    future = network.sessions.request(target, 3, 9)
    _run(clock, network, controller, 30.0, future.done)
    _show(network, target)
    _run(clock, network, controller, 20.0, lambda: session.state == "open")
    print(f"重新建立：路径 G→{'→'.join(map(str, session.route))}→{target}")


if __name__ == "__main__":
    main()
