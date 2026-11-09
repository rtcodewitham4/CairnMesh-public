"""Waking a dormant network and putting it back to sleep (tasks D2, D3).

A dormant node only samples the channel (``energy.Sniffer``). The controller
wakes the network with a WAKE_NET flood that every copy sends with a long
preamble (``MeshRelay.wake_preamble``), so each sampling neighbour notices
it; a node that hears it stays awake for the frame's LEASE seconds, during
which the whole protocol runs as before, and then goes dormant again on its
own. SLEEP_NET ends the lease early; a node that misses it still falls
asleep when its lease runs out, so no node stays awake for lack of one frame.
A routed KEEPALIVE through or to a node holds it awake for the session's
lease as well (``protocol_spec.md`` section 14).

To reach one node without waking everyone for long, the gateway sends a
directed wake probe instead (D3): a PROBE with ``PROBE_WAKE`` that every
relay passes on with the long preamble. Nodes stay awake only for its
window plus a little; the target's answer returns with the long preamble
too, re-waking the relays on its path, which the session then keeps awake.

Security: a dormant node must wake and receive a whole frame before it can
check the MIC, so forged wake-ups cost energy. A frame that fails the MIC
never reaches this layer (the node stays dormant), and a node honours at
most ``max_wakes`` WAKE_NET (and ``max_probe_wakes`` wake probes, which
cost a few seconds awake each) per ``wake_period`` seconds, relaying none
beyond that. That alone let one WAKE_NET every 4 min (LEASE up to 255 s)
keep the whole network awake for good (2026-10-21 review B2, a node holding
the network key): WAKE_NET leases also draw on an awake-time budget,
``net_awake_quota`` seconds refilled over ``quota_period`` (a token bucket,
8 bytes); lease time past it is cut, and with the budget spent WAKE_NET is
refused and not relayed. Sessions (KEEPALIVE holds) and wake probes do not
draw on it.

``WakeAgent`` runs on every device, ``WakeCaller`` at the controller.
"""

from __future__ import annotations

import math
import time
from collections import deque
from typing import Callable, Optional

from .frames import (
    PROBE_WAKE,
    ROUTED,
    FrameKind,
    JoinFrame,
    ProbeFrame,
    SleepNetFrame,
    WakeNetFrame,
    decode_frame,
    split_route,
)
from .mesh import MeshRelay
from .transport import Transport


class WakeAgent:
    """One device's dormant / awake state, driven by ``tick()``.

    ``radio`` must offer ``set_dormant(bool)`` (``mesh_sim.GeoPort``; on
    hardware the SX126x sampling mode). ``hold`` is added to a session
    KEEPALIVE's lease (the gateway sends the next one before it runs out).

    A directed wake probe (task D3, a PROBE with ``PROBE_WAKE``) keeps a
    node awake for its window plus ``probe_hold``: long enough to pass it
    on. The target stays on until its answer is back at the gateway, and
    any routed frame through or to a node (its long-preamble answer first,
    then the session's frames) wakes it or holds it for ``route_hold``.
    """

    def __init__(
        self,
        relay: MeshRelay,
        radio,
        node_id: int,
        *,
        max_wakes: int = 3,
        max_probe_wakes: int = 10,
        wake_period: float = 60.0,
        hold: float = 5.0,
        route_hold: float = 10.0,
        probe_hold: float = 3.0,
        net_awake_quota: Optional[float] = 600.0,
        quota_period: float = 3600.0,
        dormant: bool = True,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.relay = relay
        self.radio = radio
        self.node_id = node_id
        self.max_wakes = max_wakes
        self.max_probe_wakes = max_probe_wakes
        self.wake_period = wake_period
        self.hold = hold
        #: Awake this long after any other routed frame through or to this node.
        self.route_hold = route_hold
        #: Awake this long past a wake probe's window (time to pass it on).
        self.probe_hold = probe_hold
        self.clock = clock
        #: Awake seconds WAKE_NET leases may add per ``quota_period`` (``None``: no budget).
        self.net_awake_quota = net_awake_quota
        self.quota_period = quota_period
        # [MEM] WAKE_NET awake budget | 端: device | 上限: 1 | 每项: 余额 4 B + 上次结算时刻 4 B
        self._budget = net_awake_quota if net_awake_quota is not None else 0.0
        self._budget_at = clock()
        # [MEM] recent wake-ups honoured | 端: device | 上限: max_wakes + max_probe_wakes(默认 3 + 10) | 每项: 4 B 时间
        self._wakes: deque[float] = deque()
        self._probe_wakes: deque[float] = deque()
        # [MEM] wake probes already heard | 端: device | 上限: 4 | 每项: (ORIGIN, 消息 ID) 3 B
        self._probes_seen: dict[tuple[int, int], None] = {}
        # Decisions ``MeshRelay.wake_gate`` took for frames this layer sees next.
        self._decided: dict[bytes, bool] = {}
        #: Awake until the later of these: the lease of a WAKE_NET or wake
        #: probe, and the hold of the session through this node.
        self.lease_until = -1.0
        self.hold_until = -1.0
        #: When this node last woke and last fell asleep (``None``: never).
        self.woke_at: Optional[float] = None
        self.slept_at: Optional[float] = None
        #: Called with the time whenever this node wakes (``NeighbourWatch.resume``).
        self.on_wake: list[Callable[[float], None]] = []
        self.stats = {"woken": 0, "woken_by_route": 0, "woken_by_boot": 0, "slept": 0, "refused": 0, "told_to_sleep": 0, "held": 0, "over_budget": 0, "lease_cut": 0}
        relay.observers.append(self.on_heard)
        relay.wake_gate = self._gate
        self._set(dormant, clock())

    @property
    def awake(self) -> bool:
        return not self.relay.asleep

    def _set(self, dormant: bool, now: float) -> None:
        if dormant:
            self.relay.set_radio(0, self.relay.base_sf)  # samples the common setting
        self.relay.asleep = dormant
        self.radio.set_dormant(dormant)
        if dormant:
            self.slept_at = now
        else:
            self.woke_at = now
            for callback in self.on_wake:
                callback(now)

    def _refill(self, now: float) -> None:
        quota = self.net_awake_quota
        self._budget = min(quota, self._budget + (now - self._budget_at) * quota / self.quota_period)
        self._budget_at = now

    def _allow(self, now: float, plain: bytes) -> bool:
        if plain[0] == FrameKind.WAKE_NET and self.net_awake_quota is not None:
            self._refill(now)
            if self._budget < 1.0:
                self.stats["over_budget"] += 1
                return False
        probe = plain[0] == FrameKind.PROBE
        wakes, limit = (self._probe_wakes, self.max_probe_wakes) if probe else (self._wakes, self.max_wakes)
        while wakes and wakes[0] <= now - self.wake_period:
            wakes.popleft()
        if len(wakes) >= limit:
            self.stats["refused"] += 1
            return False
        wakes.append(now)
        return True

    def _gate(self, plain: bytes) -> bool:
        allowed = self._allow(self.clock(), plain)
        self._decided[plain] = allowed
        return allowed

    def _budgeted(self, now: float, until: float) -> float:
        """A WAKE_NET lease to ``until``, cut to what the awake budget still pays for (review B2)."""

        if self.net_awake_quota is None:
            return until
        self._refill(now)
        start = max(now, self.lease_until, self.hold_until)  # awake anyway until then
        extra = max(0.0, until - start)
        granted = min(extra, self._budget)
        if granted < extra:
            self.stats["lease_cut"] += 1
        self._budget -= granted
        return start + granted if granted > 0 else self.lease_until

    def _wake(self, now: float, until: float, counter: str = "woken") -> None:
        self.lease_until = max(self.lease_until, until)
        if not self.awake:
            self.stats[counter] += 1
            self._set(False, now)

    def _admitted(self, plain: bytes, now: float) -> bool:
        allowed = self._decided.pop(plain, None)
        if allowed is None:  # the relay did not ask (full, or not forwarding)
            allowed = self._allow(now, plain)
        return allowed

    def on_heard(self, plain: bytes, rssi: Optional[float]) -> None:
        """``MeshRelay`` observer: every authenticated first copy."""

        # A good MIC does not make a frame well formed: lengths are checked
        # before fields are read (review B4).
        now = self.clock()
        if plain[0] == FrameKind.WAKE_NET:
            if self._admitted(plain, now):
                try:
                    frame = decode_frame(plain)
                except ValueError:
                    return
                if isinstance(frame, WakeNetFrame):
                    until = self._budgeted(now, now + frame.lease)
                    if until > now:
                        self._wake(now, until)
        elif plain[0] == FrameKind.SLEEP_NET and self.awake:
            self.stats["told_to_sleep"] += 1
            self.lease_until = self.hold_until = now  # asleep once the relay has passed it on
        elif plain[0] in (FrameKind.PROBE, FrameKind.JOIN) and len(plain) > 6 and plain[6] & PROBE_WAKE:
            # A wake probe, or (Q22) a booting node's wake JOIN: the
            # gateway's answer to that comes back with the long preamble.
            if not self._admitted(plain, now):
                return
            try:
                frame = (JoinFrame if plain[0] == FrameKind.JOIN else ProbeFrame).from_bytes(plain)
            except ValueError:
                return
            # Every relay seals its copy as a frame of its own: only the
            # first copy of a probe sets the lease (later ones kept pushing
            # it on, measured: 10 s awake instead of 7).
            key = (frame.origin_id, frame.message_id)
            if key in self._probes_seen:
                return
            self._probes_seen[key] = None
            while len(self._probes_seen) > 4:
                self._probes_seen.pop(next(iter(self._probes_seen)))
            until = now + frame.window_s + self.probe_hold
            if frame.target_id == self.node_id and frame.kind == FrameKind.PROBE:
                # The target answers after the window; its answer then
                # crosses every hop with a long preamble before the
                # gateway's first keepalive can come back.
                until += self.route_hold + (len(frame.hops) + 1) * self._reply_hop()
            self._wake(now, until)
        elif plain[0] & ROUTED:
            try:
                base, route = split_route(plain)
            except ValueError:
                return
            if self.node_id not in route and base[2] != self.node_id:
                return
            if base[0] == FrameKind.KEEPALIVE and len(base) < 7:
                return
            self.stats["held"] += 1
            if base[0] == FrameKind.KEEPALIVE and base[5] == 0:
                self.hold_until = now  # the session closed; a network lease still holds
                return
            hold = base[5] + self.hold if base[0] == FrameKind.KEEPALIVE else self.route_hold
            self.hold_until = max(self.hold_until, now + hold)
            self._wake(now, self.lease_until, "woken_by_route")

    def boot(self, until: float) -> None:
        """Q22: the node just started in a sleeping network; awake until ``until`` to send its JOIN and hear the answer."""

        self.relay.boot_until = until  # its JOINs until then go with the long preamble
        self._wake(self.clock(), until, "woken_by_boot")

    def _reply_hop(self) -> float:
        relay = self.relay
        return relay.wake_lead + relay.airtime(48) + relay.route_jitter + relay.route_slack

    def next_due(self) -> float:
        """The earliest time ``tick`` can put the node to sleep (``MeshNetwork`` skips idle ticks).

        Asleep it only wakes on a frame. Awake, at the end of its lease, or
        later while the relay still has frames: when the relay next acts.
        """

        if not self.awake:
            return math.inf
        due = max(self.lease_until, self.hold_until)
        return max(due, self.relay.next_due()) if self.relay.pending() else due

    def tick(self, now: Optional[float] = None) -> None:
        now = self.clock() if now is None else now
        if self.awake and now >= max(self.lease_until, self.hold_until) and not self.relay.pending():
            self.stats["slept"] += 1
            self._set(True, now)


class WakeCaller:
    """The controller's side: send WAKE_NET / SLEEP_NET and note the first relay heard.

    ``transport`` is the controller's secure layer, ``relay`` the
    ``MeshRelay`` under it (it repeats the WAKE_NET until a copy comes back).
    """

    def __init__(
        self,
        transport: Transport,
        relay: MeshRelay,
        node_id: int,
        *,
        epoch: int = 0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.transport = transport
        self.relay = relay
        self.node_id = node_id
        self.epoch = epoch
        self.clock = clock
        self._sequence = 0
        #: When the last WAKE_NET went out, and when a relay was first heard passing it on.
        self.sent_at: Optional[float] = None
        self.heard_at: Optional[float] = None
        relay.wake_heard = self._heard

    def _next(self) -> int:
        self._sequence = (self._sequence + 1) & 0xFF
        return self._sequence

    def _heard(self) -> None:
        if self.heard_at is None:
            self.heard_at = self.clock()

    def wake(self, lease: int) -> None:
        """Wake the network for ``lease`` seconds (1..255)."""

        self.sent_at, self.heard_at = self.clock(), None
        frame = WakeNetFrame(self.node_id, self._next(), lease, epoch=self.epoch)
        self.transport.send(frame.encode())

    def sleep(self) -> None:
        self.transport.send(SleepNetFrame(self.node_id, self._next(), epoch=self.epoch).encode())
