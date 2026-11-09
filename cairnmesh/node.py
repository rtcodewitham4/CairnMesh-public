"""One device's protocol stack, wired the way firmware must wire it (``protocol_spec.md`` section 17).

The layers are written as separate classes (``MeshRelay``, ``SecureTransport``,
``RouteEndpoint``, ``Device``, ``NeighbourWatch``, ``EventReporter``,
``WakeAgent``, ``DiscoveryAgent``); this module is the one place that says how they connect
on a node, in what order the main loop drives them, and what a reboot does to
each. ``mesh_sim.MeshNetwork`` builds every simulated device with it; the
firmware's main loop mirrors ``NodeStack.tick``.

Wiring (who calls whom):

- ``radio_stack``: the relay owns the radio; ``SecureTransport`` sits on the
  relay, and the frames the relay originates itself (re-sealed probes, route
  errors, trace records) are sealed by that same transport (``relay.sealer``),
  so the node has one FCNT counter.
- ``RouteEndpoint`` on the transport, ``Device`` on the endpoint;
  ``NeighbourWatch`` and ``DiscoveryAgent`` on the transport, both reading the
  relay (link quality) and the device's state; ``EventReporter`` (module
  events, section 19) on the transport, watching the relay for its
  acknowledgements.
- ``WakeAgent`` hooks the relay (``observers``, ``wake_gate``) and switches the
  radio between sampling and receiving; when the node wakes, the neighbour
  table resumes and the queued events go out (``on_wake``).
"""

from __future__ import annotations

import math
import random
import time
from typing import Callable, Optional

from .device import Device
from .discovery import DiscoveryAgent
from .frames import PROBE_WINDOW_UNIT
from .liveness import NeighbourWatch
from .mesh import MeshRelay
from .routed import RouteEndpoint
from .secure import AppKeys, SecureTransport
from .uplink import EventReporter
from .wake import WakeAgent

#: Q22: how long a node booting in a sleeping network stays awake after its
#: JOIN's window for the gateway's answer (long preamble on every hop back).
BOOT_ANSWER_WAIT = 15.0


def radio_stack(
    radio,
    key: bytes,
    node_id: int,
    *,
    forward: bool,
    clock: Callable[[], float] = time.monotonic,
    rng: Optional[random.Random] = None,
    app: Optional[AppKeys] = None,
    initial_fcnt: int = 0,
    **relay_options,
) -> tuple[MeshRelay, SecureTransport]:
    """The relay on ``radio`` and the security layer on it, sharing one FCNT counter.

    ``forward``: a device relays; the gateway's own stack does not.
    """

    relay = MeshRelay(radio, key, node_id, forward=forward, clock=clock, rng=rng, **relay_options)
    secure = SecureTransport(relay, key, initial_fcnt=initial_fcnt, app=app)
    # Frames the relay originates (probes, route errors) share the node's one counter.
    relay.sealer = secure.seal_frame
    return relay, secure


class NodeStack:
    """A device's layers above ``radio_stack``, and its main loop.

    ``rngs`` gives each layer that draws jitter its own source (``device``,
    ``watch``, ``events``, ``agent``); firmware has one RNG, the simulator one per layer
    so that adding a layer does not move another's draws. A layer is present
    when its options are: ``hello_interval`` (the neighbour watch),
    ``wake_options`` (dormant start and hardware wake; ``radio`` must then
    offer ``set_dormant``), ``discovery_options`` (JOIN at boot, answers to
    scans).
    """

    def __init__(
        self,
        relay: MeshRelay,
        secure: SecureTransport,
        radio,
        node_id: int,
        *,
        controller_id: int,
        clock: Callable[[], float] = time.monotonic,
        rngs: Optional[dict[str, random.Random]] = None,
        heartbeat_interval: float = math.inf,
        device_options: Optional[dict] = None,
        hello_interval: Optional[float] = None,
        watch_options: Optional[dict] = None,
        wake_options: Optional[dict] = None,
        discovery_options: Optional[dict] = None,
        event_options: Optional[dict] = None,
    ) -> None:
        rngs = rngs or {}
        self.node_id = node_id
        self.controller_id = controller_id
        self.clock = clock
        self.relay = relay
        self.secure = secure
        self.endpoint = RouteEndpoint(secure, node_id, clock=clock)
        self.device = Device(
            self.endpoint,
            node_id,
            controller_id=controller_id,
            awake=True,
            heartbeat_interval=heartbeat_interval,
            rng=rngs.get("device"),
            clock=clock,
            **(device_options or {}),
        )
        device = self.device
        self.watch: Optional[NeighbourWatch] = None
        if hello_interval is not None:
            self.watch = NeighbourWatch(
                secure,
                relay,
                node_id,
                controller_id=controller_id,
                state=lambda: device.state,
                hello_interval=hello_interval,
                epoch=node_id,
                clock=clock,
                rng=rngs.get("watch"),
                **(watch_options or {}),
            )
        #: Module events (wire format v4, section 19): every node has the queue.
        self.events = EventReporter(
            secure,
            relay,
            node_id,
            controller_id=controller_id,
            next_sequence=self.watch.next_sequence if self.watch is not None else None,
            epoch=node_id,
            clock=clock,
            rng=rngs.get("events"),
            **(event_options or {}),
        )
        self.waker: Optional[WakeAgent] = None
        if wake_options is not None:
            self.waker = WakeAgent(relay, radio, node_id, clock=clock, **wake_options)
            if self.watch is not None:
                self.waker.on_wake.append(self.watch.resume)
            self.waker.on_wake.append(self.events.resume)
        self.agent: Optional[DiscoveryAgent] = None
        if discovery_options is not None:
            self.agent = DiscoveryAgent(
                secure,
                relay,
                node_id,
                controller_id=controller_id,
                state=lambda: device.state,
                neighbours=self.watch.neighbours if self.watch is not None else None,
                epoch=node_id,
                clock=clock,
                rng=rngs.get("agent"),
                **discovery_options,
            )

    @property
    def awake(self) -> bool:
        """Asleep, only the relay and the wake agent run (the radio samples for preambles)."""

        return self.waker is None or self.waker.awake

    def tick(self, now: Optional[float] = None) -> None:
        """One pass of the main loop, in this order: relay, wake agent, then (awake only) device, watch, events, agent.

        The wake agent runs after the relay so that a node puts itself to
        sleep only once the relay has nothing left to send this pass.
        """

        self.relay.tick(now)
        if self.waker is not None:
            self.waker.tick(now)
            if not self.waker.awake:
                return
        self.device.tick(now)
        if self.watch is not None:
            self.watch.tick(now)
        self.events.tick(now)
        if self.agent is not None:
            self.agent.tick(now)

    def next_due(self) -> float:
        """The earliest time ``tick`` can do anything: until then the MCU may sleep."""

        due = self.relay.next_due()
        if self.waker is not None:
            due = min(due, self.waker.next_due())
            if not self.waker.awake:
                return due  # asleep: its device, watch and agent are not ticked
        due = min(due, self.device.next_due())
        if self.watch is not None:
            due = min(due, self.watch.next_due())
        due = min(due, self.events.next_due())
        if self.agent is not None:
            due = min(due, self.agent.next_due())
        return due

    def notify(self, event_id: int) -> bool:
        """A module raised ``event_id`` (section 19): queued, asleep or awake; ``False`` when the queue is full."""

        return self.events.notify(event_id)

    def boot(self) -> None:
        """Power back on: what each layer keeps and loses across a reboot.

        Kept (persisted on the MCU): the FCNT, the controller's mark (the
        highest FCNT heard from it, relayed or for this node; it seeds the
        controller's replay and relay windows), the device's dedup records
        (``reset`` keeps them; open actions become INTERRUPTED). Lost: every other
        sender's replay and relay window (flash wear, spec section 5), the
        neighbour table, the join state, the queued module events. A node
        booting into a sleeping network stays awake
        long enough to JOIN and hear the answer (Q22).
        """

        heard = [m for m in (self.secure.mark(self.controller_id), self.relay.mark(self.controller_id)) if m is not None]
        marks = {self.controller_id: max(heard)} if heard else None
        self.secure.reboot(marks)
        self.relay.reboot(marks)
        self.device.reset()
        if self.watch is not None:
            self.watch.forget()
        self.events.forget()
        if self.agent is not None:
            self.agent.start()  # it joins again
            if self.waker is not None and self.relay.join_wakes:
                # Q22: its main loop runs from boot (otherwise it would join
                # only once something woke it): awake through the JOIN
                # spread, the JOIN's window and the answer's way back.
                agent = self.agent
                self.waker.boot(self.clock() + agent.join_spread + agent.join_window * PROBE_WINDOW_UNIT + BOOT_ANSWER_WAIT)
