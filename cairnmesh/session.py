"""The facade front ends use: one object, a few verbs, and an event bus.

``LinkSession`` owns the background loop that drives ``Controller.tick()``
(plus any extra pollers such as a simulated network). A UI depends only on
this class and ``events``; swapping the simulator for real hardware means
building the session from a different transport:

    session = LinkSession.serial("/dev/ttyUSB0", nodes=[1, 2], key=KEY)

``LinkSession.mesh(positions)`` is the multi-hop simulator with routed
sessions: the controller talks through the session ``Gateway``, and the
verbs ``open_session`` / ``close_session`` / ``trace`` / ``path_view`` /
``link_info`` expose it. Session changes and trace answers come out as
``SessionChanged`` / ``TraceArrived`` events.

The single- and multi-hop simulators are source-tree / editable-install
features; release wheels do not contain their modules.
"""

from __future__ import annotations

import os
import threading
import time
from concurrent.futures import Future
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable, Iterable, Mapping, Optional

from .controller import Controller, RequestOutcome
from .events import EventBus, FrameRejected, LinkError, SessionChanged, TraceArrived

if TYPE_CHECKING:  # pragma: no cover
    from .gateway import Gateway, Scan
    from .mesh_sim import MeshNetwork, Position
    from .pathview import PathView

Poller = Callable[[float], None]
ErrorHandler = Callable[[Exception], None]

#: Path views ``LinkSession.path_view`` can build.
PATH_SOURCES = ("gateway", "trace", "truth")


class SimClock:
    """Virtual time for a simulated network, advanced in fixed steps by its driver."""

    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class MeshDriver:
    """Runs a ``MeshNetwork`` and its controller on a ``SimClock`` in fixed steps.

    Each step (``step``, 5 ms: the latency model's ``turnaround`` includes
    it, and every session test runs this way) ticks the network, then the
    controller. The background loop calls ``catch_up``, which steps the
    virtual clock up to the wall clock (times ``speed``); a loop that falls
    more than ``max_lag`` behind lets virtual time slip rather than race.
    """

    def __init__(
        self,
        network: "MeshNetwork",
        controller: Controller,
        clock: SimClock,
        *,
        step: float = 0.005,
        speed: float = 1.0,
        max_lag: float = 0.25,
    ) -> None:
        self.network = network
        self.controller = controller
        self.clock = clock
        self.step = step
        self.speed = speed
        self.max_lag = max_lag
        self._wall = time.monotonic()
        self._base = clock.now

    def restart(self) -> None:
        """Count wall time from now (the loop starts, or starts again)."""

        self._wall = time.monotonic()
        self._base = self.clock.now

    def catch_up(self, on_error: ErrorHandler, after_step: Callable[[], None]) -> None:
        target = self._base + (time.monotonic() - self._wall) * self.speed
        behind = target - self.clock.now
        if behind > self.max_lag:
            self._base -= behind - self.max_lag
            target = self.clock.now + self.max_lag
        while self.clock.now + self.step <= target:
            self._step(on_error)
            after_step()

    def advance(
        self, seconds: float, on_error: ErrorHandler, after_step: Callable[[], None]
    ) -> None:
        for _ in range(round(seconds / self.step)):
            self._step(on_error)
            after_step()
        # Wall time spent here does not count as lag.
        self.restart()

    def _step(self, on_error: ErrorHandler) -> None:
        self.clock.now += self.step
        now = self.clock.now
        for tick in (self.network.tick, self.controller.tick):
            try:
                tick(now)
            except Exception as exc:  # noqa: BLE001 - report and keep the link alive
                on_error(exc)


@dataclass(frozen=True)
class LinkInfo:
    """A snapshot of the routed session with one node, for display."""

    node_id: int
    state: str
    #: Relays from the gateway outward (the target not included).
    route: tuple[int, ...]
    hops: int
    #: The latency model's command round trip: no loss, and one repeat per hop.
    expected: Optional[float]
    worst: Optional[float]
    #: ``worst`` within ``realtime_target`` (the 5 s control goal, not a pass mark).
    within_target: Optional[bool]
    realtime_target: float
    #: Seconds the current path has been up (``None`` until it is).
    age: Optional[float]
    #: Seconds since the session was last used, and when the gateway closes it.
    idle: float
    idle_timeout: Optional[float]
    #: A trace is waiting for the path or out on it.
    tracing: bool
    counts: dict = field(default_factory=dict)


class LinkSession:
    def __init__(
        self,
        controller: Controller,
        *,
        nodes: Iterable[int] = (),
        pollers: Iterable[Poller] = (),
        tick_interval: float = 0.02,
        heartbeat_interval: Optional[float] = None,
        simulation=None,
        resources: Iterable = (),
        gateway: Optional["Gateway"] = None,
        driver: Optional[MeshDriver] = None,
    ) -> None:
        self.controller = controller
        self.nodes = tuple(nodes)
        self.pollers = list(pollers)
        self.tick_interval = tick_interval
        #: Expected heartbeat period; front ends derive "stale/offline" from it.
        self.heartbeat_interval = heartbeat_interval
        #: Present only for simulated sessions (power switch, drop rates…):
        #: a ``SimulatedNetwork``, or a ``MeshNetwork`` when ``multihop``.
        self.simulation = simulation
        #: Objects with ``start()`` / ``close()`` (a serial port) tied to the session.
        self.resources = list(resources)
        #: The routed-session gateway the controller talks through (multi-hop only).
        self.gateway = gateway
        #: Steps a simulated multi-hop network in place of ``pollers``.
        self.driver = driver
        #: Held by the loop for each tick and by every verb. The gateway and
        #: the simulator are not thread-safe: hold it to touch them directly.
        self.lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._history_seen = 0
        self._probe_started: dict[int, float] = {}
        self._traces_seen: dict[int, float] = {}
        self._hop_errors_seen: dict[int, dict] = {}

    @classmethod
    def simulated(
        cls,
        node_ids: Iterable[int] = (1, 2, 3, 4),
        *,
        downlink_drop: float = 0.1,
        uplink_drop: float = 0.1,
        heartbeat_interval: float = 3.0,
        timeout: float = 0.5,
        max_retries: int = 5,
        seed: Optional[int] = None,
        trace: Optional[Callable[[str], None]] = None,
    ) -> "LinkSession":
        """Create an in-process single-hop simulation.

        Simulation support is available only from the source tree or an
        editable install; it is not included in the wheel.
        """

        try:
            from .sim import SimulatedNetwork
        except ImportError as exc:
            raise ImportError(
                "仿真仅源码树可用，wheel 不含；请用 pip install -e . 从源码树安装"
            ) from exc

        node_ids = tuple(node_ids)
        network = SimulatedNetwork(
            node_ids,
            downlink_drop=downlink_drop,
            uplink_drop=uplink_drop,
            heartbeat_interval=heartbeat_interval,
            seed=seed,
            trace=trace,
        )
        controller = Controller(
            network.gateway, timeout=timeout, max_retries=max_retries, allow_volatile_epoch=True
        )
        return cls(
            controller,
            nodes=node_ids,
            pollers=[network.tick],
            heartbeat_interval=heartbeat_interval,
            simulation=network,
        )

    @classmethod
    def mesh(
        cls,
        positions: Mapping[int, "Position"],
        *,
        key: Optional[bytes] = None,
        sf: int = 7,
        drop: float = 0.0,
        hello_interval: Optional[float] = None,
        seed: Optional[int] = None,
        speed: float = 1.0,
        step: float = 0.005,
        tick_interval: float = 0.02,
        gateway_options: Optional[dict] = None,
        controller_options: Optional[dict] = None,
        discover: bool = False,
        **network_options,
    ) -> "LinkSession":
        """The multi-hop simulator with routed sessions (``MeshNetwork``).

        Devices sit at ``positions`` (``mesh_sim.line`` / ``ladder`` /
        ``scatter``), the controller at ``controller_position`` (default the
        origin). ``hello_interval`` turns on HELLO liveness, whose LOST /
        RECOVERED reports arrive as ``NodeReported``; there are no
        heartbeats. ``network_options`` go to ``MeshNetwork``.

        ``discover`` (task C): nodes JOIN when they start and the gateway
        scans at start and every hour; ``nodes`` is then empty, and nodes
        come in as ``NodeFound`` events instead of from a hand-made list.

        Simulation support is available only from the source tree or an
        editable install; it is not included in the wheel.
        """

        from .airtime import airtime_for
        try:
            from .mesh_sim import MeshNetwork
        except ImportError as exc:
            raise ImportError(
                "仿真仅源码树可用，wheel 不含；请用 pip install -e . 从源码树安装"
            ) from exc

        gateway_options = dict(gateway_options or {})
        if discover:
            gateway_options = {
                "expected_nodes": len(positions),
                "scan_at_start": True,
                "scan_interval": 3600.0,
                **gateway_options,
            }
            network_options.setdefault("discovery_options", {})
        clock = SimClock()
        network = MeshNetwork(
            positions,
            key=os.urandom(16) if key is None else key,
            clock=clock,
            airtime=airtime_for(sf),
            drop=drop,
            hello_interval=hello_interval,
            seed=seed,
            gateway_options=gateway_options,
            **network_options,
        )
        controller = network.controller(network.sessions, clock=clock, **(controller_options or {}))
        network.sessions.attach(controller)
        return cls(
            controller,
            nodes=() if discover else sorted(positions),
            tick_interval=tick_interval,
            simulation=network,
            gateway=network.sessions,
            driver=MeshDriver(network, controller, clock, step=step, speed=speed),
        )

    @classmethod
    def serial(
        cls,
        port,
        *,
        nodes: Iterable[int] = (),
        baud: int = 115200,
        key: Optional[bytes] = None,
        app_keys=None,
        state_file=None,
        controller_state=None,
        heartbeat_interval: Optional[float] = 10.0,
        **controller_options,
    ) -> "LinkSession":
        """Session over a real serial module (SLIP + CRC16, optional AES-CCM).

        ``port`` is a device path or an already opened port object; ``key``
        (16 bytes) turns on the secure envelope, ``app_keys`` (``AppKeys``,
        needs ``key``) the per-node application layer (Q14), and ``state_file`` keeps its
        frame counters and replay marks across restarts (strongly advised for
        a long-running controller). ``controller_state`` is the controller's
        own state file (layer 3: EPOCH, recent message ids, commands still
        being confirmed); it defaults to ``state_file`` + ``.controller``
        when ``state_file`` is given, else to ``controller.json`` in
        ``$CAIRNMESH_STATE_DIR`` or ``~/.local/state/cairnmesh`` (review D1: a
        controller without one refuses commands). ``controller_options`` go to
        ``Controller``. Starting the session also starts the serial reader.
        """

        from .serial_link import SerialTransport

        events = EventBus()
        link = (
            SerialTransport.open(port, baud) if isinstance(port, str) else SerialTransport(port)
        )
        link.set_error_handler(
            lambda exc: events.publish(LinkError(message=f"serial: {type(exc).__name__}: {exc}"))
        )
        transport = link
        # Closed in this order: the port first, so the reader thread stops
        # accepting frames, then the secure layer flushes their replay marks.
        resources = [link]
        if key is not None:
            from .secure import SecureTransport

            transport = SecureTransport(
                link,
                key,
                state_file=state_file,
                app=app_keys,
                on_reject=lambda reason, raw: events.publish(
                    FrameRejected(raw=raw, reason=reason)
                ),
            )
            resources.append(transport)
        elif state_file is not None:
            raise ValueError("state_file needs a key: only the secure layer keeps counters")
        elif app_keys is not None:
            raise ValueError("app_keys needs a key: the application layer sits inside the secure envelope")
        if controller_state is None and state_file is not None:
            controller_state = f"{state_file}.controller"
        if controller_state is None and "state_file" not in controller_options and "epoch" not in controller_options:
            directory = os.environ.get("CAIRNMESH_STATE_DIR") or os.path.expanduser("~/.local/state/cairnmesh")
            os.makedirs(directory, exist_ok=True)
            controller_state = os.path.join(directory, "controller.json")
        if controller_state is not None:
            controller_options.setdefault("state_file", str(controller_state))
        controller = Controller(transport, events=events, **controller_options)
        if hasattr(transport, "awaits_request_answer"):
            transport.awaits_request_answer = controller.awaits_request_answer  # review B1
        if controller.state_warning is not None:
            events.publish(LinkError(message=controller.state_warning))
        return cls(
            controller,
            nodes=nodes,
            heartbeat_interval=heartbeat_interval,
            resources=resources,
        )

    # -------------------------------------------------------------- lifecycle

    @property
    def events(self) -> EventBus:
        return self.controller.events

    @property
    def gateway_id(self) -> int:
        return self.controller.sender_id

    @property
    def multihop(self) -> bool:
        return self.gateway is not None

    def now(self) -> float:
        """The controller's clock (virtual time in a simulated multi-hop session)."""

        return self.controller.clock()

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        for resource in self.resources:
            resource.start()
        if self.driver is not None:
            self.driver.restart()
        self._thread = threading.Thread(
            target=self._loop, name="link-session", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is None:
            return
        self._thread.join(timeout=1.0)
        # Keep a thread that is still finishing a tick, so start() does not
        # launch a second loop next to it.
        if not self._thread.is_alive():
            self._thread = None
        for resource in self.resources:
            # One failing close (say, a state file that cannot be written)
            # must not leave the others open.
            try:
                resource.close()
            except Exception as exc:  # noqa: BLE001 - report and close the rest
                self.events.publish(
                    LinkError(message=f"close: {type(exc).__name__}: {exc}")
                )

    def __enter__(self) -> "LinkSession":
        self.start()
        return self

    def __exit__(self, *exc_info) -> None:
        self.stop()

    def _loop(self) -> None:
        while not self._stop.is_set():
            with self.lock:
                self._tick()
            self._stop.wait(self.tick_interval)

    def _tick(self) -> None:
        if self.driver is not None:
            # Gateway events go out after every step: a ``broken`` event must
            # carry the path that broke, before a re-probe replaces it.
            self.driver.catch_up(self._report, self._publish_gateway_events)
            return
        # The controller's own clock: it stamps deadlines with it, so
        # driving tick() from another clock would skew every retry.
        now = self.controller.clock()
        # Each step is guarded on its own: a failing device or poller must
        # not keep the controller from retransmitting.
        for step in (*self.pollers, self.controller.tick):
            try:
                step(now)
            except Exception as exc:  # noqa: BLE001 - report and keep the link alive
                self._report(exc)

    def _report(self, exc: Exception) -> None:
        self.events.publish(LinkError(message=f"{type(exc).__name__}: {exc}"))

    def advance(self, seconds: float) -> None:
        """Run a simulated multi-hop session ``seconds`` of virtual time now.

        For tests and scripts, with the background loop not running.
        """

        if self.driver is None:
            raise RuntimeError("advance() needs a simulated multi-hop session (LinkSession.mesh)")
        with self.lock:
            self.driver.advance(seconds, self._report, self._publish_gateway_events)

    # ------------------------------------------------------------------ verbs

    def wake(self, node_id: int) -> "Future[RequestOutcome]":
        with self.lock:
            if self.gateway is not None:
                timeout, retries = self.gateway.timeouts_for(node_id)
                return self.controller.wake(node_id, timeout=timeout, max_retries=retries)
            return self.controller.wake(node_id)

    def request(
        self,
        node_id: int,
        action: int,
        value: int = 0,
        force: bool = False,
        past_confirming: bool = False,
        important: bool = False,
    ) -> "Future[RequestOutcome]":
        """``force`` skips the acceptance check; ``past_confirming`` goes past a confirming command.

        ``important`` (default no): confirm it on to the devices' keep period; an ordinary command
        ends ``UNCONFIRMED`` at the controller's ``confirm_limit`` ("may have run", never sent again).
        """

        with self.lock:
            if self.gateway is not None:
                # The gateway's timeouts follow the latency model of the path;
                # it refuses nodes that failed acceptance unless ``force``.
                return self.gateway.request(
                    node_id, action, value, force=force, past_confirming=past_confirming, important=important
                )
            return self.controller.request(node_id, action, value, force=past_confirming, important=important)

    def past_allowance(self, node_id: int):
        """``Controller.past_allowance``: the confirming SEQs and how many may still go past them."""

        return self.controller.past_allowance(node_id)

    def pending_count(self, node_id: int) -> int:
        return self.controller.pending_count(node_id)

    def confirming(self) -> list:
        """Commands whose outcome is still being confirmed (layer 3, ``Controller.confirming``)."""

        return self.controller.confirming()

    def abandon(self, node_id: int, sequence: Optional[int] = None) -> int:
        """The operator gives up confirming (recorded as such); returns how many."""

        with self.lock:
            return self.controller.abandon(node_id, sequence)

    # ------------------------------------------------------- routed sessions

    def _require_gateway(self) -> "Gateway":
        if self.gateway is None:
            raise RuntimeError("routed sessions need a multi-hop session (LinkSession.mesh)")
        return self.gateway

    def open_session(self, node_id: int) -> str:
        """Open (or keep using) the routed session with ``node_id``; returns its state."""

        gateway = self._require_gateway()
        with self.lock:
            state = gateway.open_session(node_id).state
            self._publish_gateway_events()
            return state

    def close_session(self, node_id: int) -> None:
        gateway = self._require_gateway()
        with self.lock:
            gateway.close_session(node_id)
            self._publish_gateway_events()

    def accept(self, node_id: int, rounds: int = 20):
        """Deployment acceptance of ``node_id`` (``Gateway.accept``); the verdict comes as ``AcceptanceFinished``."""

        gateway = self._require_gateway()
        with self.lock:
            return gateway.accept(node_id, rounds)

    def test_link(self, node_id: int, rounds: int = 20):
        """Start a link test to ``node_id`` (``Gateway.test_link``); the result comes as ``LinkTestFinished``."""

        gateway = self._require_gateway()
        with self.lock:
            return gateway.test_link(node_id, rounds)

    def trace(self, node_id: int) -> bool:
        """Trace the last command to ``node_id`` once its path is free.

        ``False`` when there is no session or no command on it yet. The
        answer comes as ``TraceArrived``; ``path_view(node, "trace")`` shows it.
        """

        gateway = self._require_gateway()
        with self.lock:
            return gateway.trace(node_id)

    def link_info(self, node_id: int) -> Optional[LinkInfo]:
        gateway = self._require_gateway()
        with self.lock:
            session = gateway.session(node_id)
            if session is None:
                return None
            now = self.now()
            latency = session.latency
            return LinkInfo(
                node_id=node_id,
                state=session.state,
                route=session.route,
                hops=session.hops,
                expected=latency.expected if latency else None,
                worst=latency.worst if latency else None,
                within_target=session.within_target,
                realtime_target=gateway.model.target,
                age=None if session.established_at is None else now - session.established_at,
                idle=now - session.last_used,
                idle_timeout=gateway.idle_timeout,
                tracing=session.trace_wanted is not None or session.trace_id is not None,
                counts=dict(session.counts),
            )

    def scan(self) -> "Scan":
        """Start a DISCOVER scan (task C), or return the one running.

        Answers come as ``NodeFound`` events, the end as ``ScanFinished``.
        """

        gateway = self._require_gateway()
        with self.lock:
            return gateway.scan()

    def known_nodes(self) -> dict:
        """``{node: KnownNode}``: every node the gateway heard JOIN or answer a scan."""

        gateway = self._require_gateway()
        with self.lock:
            return dict(gateway.nodes)

    def weak_links(self, threshold: Optional[float] = None) -> list:
        """Measured hops with little margin, weakest first (``Gateway.weak_links``)."""

        gateway = self._require_gateway()
        with self.lock:
            return gateway.weak_links(threshold)

    def air_activity(self) -> dict[int, tuple[float, float, int]]:
        """``{node: (start, end, first byte)}`` of the last frame each radio has put on air.

        Simulated multi-hop only (``GeoMedium.last_transmissions``); without
        simulation this is empty. The gateway is ``gateway_id``. Frames still
        queued behind a radio's previous one are left out. The first byte is
        ``KIND | 0x80``, with ``ROUTED`` (0x40) for session traffic.
        """

        self._require_gateway()
        with self.lock:
            if self.simulation is None:
                return {}
            now = self.now()
            return {
                node: tx
                for node, tx in self.simulation.medium.last_transmissions().items()
                if tx[0] <= now
            }

    def path_view(self, node_id: int, source: str = "gateway") -> Optional["PathView"]:
        """Hop-by-hop view of ``node_id``'s path, or ``None`` when there is none.

        ``gateway``: what the gateway knows for free (``pathview.gateway_view``);
        ``trace``: the last trace's answer (``pathview.trace_view``);
        ``truth``: every node's own record of the last command, read straight
        from the simulator (``pathview.records_view``; no hardware equivalent
        without a trace).
        """

        from . import pathview

        gateway = self._require_gateway()
        if source not in PATH_SOURCES:
            raise ValueError(f"source must be one of {PATH_SOURCES}")
        with self.lock:
            if source == "gateway":
                return pathview.gateway_view(gateway, node_id)
            if source == "trace":
                return pathview.trace_view(gateway, node_id)
            if self.simulation is None:
                return None
            session = gateway.session(node_id)
            if session is None or not session.deliveries:
                return None
            delivery = session.deliveries[-1]
            view = pathview.records_view(
                gateway.controller_id,
                delivery.route,
                node_id,
                self.simulation.route_records(),
                delivery.sequence,
                delivery.epoch,
                session.links if delivery.route == session.route else None,
                "模拟真值：各节点本地记录（仅模拟；真机要靠追踪帧）",
            )
            view.delivery = delivery
            view.session_state = session.state
            for hop in view.hops:
                hop.errors = session.hop_errors.get((hop.sender, hop.receiver), 0)
            if delivery.ok and all(hop.repeats_out is None for hop in view.hops):
                # Each node keeps only its last ``max_route_log`` routed frames;
                # keepalives push an older command out, which is not a failure.
                view.notes.append("该命令的逐跳记录已被后来的帧挤出（每节点只留最近几条），真值只对最近的命令有效")
            return view

    def _publish_gateway_events(self) -> None:
        """Turn new ``Gateway.history`` entries and trace answers into events."""

        gateway = self.gateway
        history = gateway.history
        # ``_history_seen`` counts every entry ever logged; the gateway drops old ones from the front.
        self._history_seen = max(self._history_seen, gateway.history_base)
        while self._history_seen < gateway.history_base + len(history):
            at, what, node = history[self._history_seen - gateway.history_base]
            self._history_seen += 1
            state, _, detail = what.partition(":")
            if state == "probe":
                self._probe_started.setdefault(node, at)
                self.events.publish(SessionChanged(node_id=node, state="probing"))
                continue
            if state not in ("open", "broken", "failed", "closed"):
                continue
            if state == "open":
                started = self._probe_started.pop(node, None)
                session = gateway.session(node)
                if session is not None and session.latency is not None:
                    self.events.publish(
                        SessionChanged(
                            node_id=node,
                            state="open",
                            route=session.route,
                            expected=session.latency.expected,
                            worst=session.latency.worst,
                            within_target=session.within_target,
                            realtime_target=gateway.model.target,
                            setup=None if started is None else at - started,
                        )
                    )
                continue
            if state == "broken":
                session = gateway.session(node)
                if session is not None:
                    # The gateway counts the hop a ROUTE_ERROR (or its own
                    # unacknowledged first hop) named just before it breaks.
                    seen = self._hop_errors_seen.get(node, {})
                    grown = [hop for hop, n in session.hop_errors.items() if n > seen.get(hop, 0)]
                    self._hop_errors_seen[node] = dict(session.hop_errors)
                    self.events.publish(
                        SessionChanged(
                            node_id=node,
                            state=state,
                            detail=detail,
                            route=session.route,
                            broken_at=grown[0] if grown else None,
                        )
                    )
                    continue
            if state == "closed":
                self._probe_started.pop(node, None)
                self._hop_errors_seen.pop(node, None)
            self.events.publish(SessionChanged(node_id=node, state=state, detail=detail))

        for node, session in gateway.sessions.items():
            seen = self._traces_seen.get(node, float("-inf"))
            newest = seen
            for delivery in session.deliveries:
                trace = delivery.trace
                if trace is None or trace.received_at <= seen:
                    continue
                newest = max(newest, trace.received_at)
                self.events.publish(
                    TraceArrived(
                        node_id=node,
                        sequence=delivery.sequence,
                        # The gateway adds its own record to the nodes' answers.
                        answered=len(trace.records) - 1,
                        expected=len(trace.route) + 1,
                    )
                )
            self._traces_seen[node] = newest
