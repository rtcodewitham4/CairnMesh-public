"""A simulated multi-hop radio network (PC only).

``GeoMedium`` places radios on a plane. A frame reaches every radio within
``radio_range`` after ``airtime`` seconds, with an RSSI that falls with
distance, unless it is lost. With ``signal`` (``radio_model.LogDistance``,
task B1) reach is instead decided per copy by a link budget with shadowing,
fading and a per-SF sensitivity; ``radio_range`` is then the median reach at
the reference SF. Each radio is on one ``channel`` and listens at one ``sf``
and only hears frames sent on both (different SFs are treated as fully
orthogonal, a simplification). Frames are lost:

* at random (``drop``);
* in a **collision**: two frames overlapping in time at one receiver are
  both lost (no capture effect, the pessimistic case), unless ``signal`` has
  a ``capture_db`` and one is that much stronger;
* to **half duplex**: a radio that is transmitting hears nothing;
* to **sleep** (task D1): a ``dormant`` radio only samples the channel
  (``energy.Sniffer``) and hears a frame only when a look falls inside its
  preamble; senders reach it with a long preamble (``send(…, preamble=)``).

Every radio has an ``energy.EnergyMeter`` (``GeoPort.meter``). ``move`` puts
a radio somewhere else (the mobile controller); its links get new shadowing.

A radio sends one frame at a time; a frame handed over while it is still on
air waits its turn. Collisions are what make a naive flood collapse, so they
are modelled; the numbers (``airtime`` 50 ms is roughly SF7 / 125 kHz for a
15-byte frame) are estimates, not measurements. ``airtime`` may instead be a
function of the frame length (``airtime.airtime_for(sf)``, the Semtech
formula), which session scenarios need: routed frames are much longer.

``MeshNetwork`` puts a controller and devices on such a medium, each behind
``SecureTransport`` and ``MeshRelay``.
"""

from __future__ import annotations

import hashlib
import heapq
import itertools
import math
import random
import threading
import time
from collections import deque
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Callable, Mapping, Optional

from .airtime import Airtime, lora_airtime
from .device import Device
from .discovery import DiscoveryAgent
from .energy import EnergyMeter, Sniffer, preamble_for, symbol_time
from .frames import BROADCAST_ID, DEFAULT_CONTROLLER_ID
from .gateway import Gateway
from .liveness import NeighbourWatch
from .mesh import MeshRelay, RadioReceiver
from .node import NodeStack, radio_stack
from .radio_model import LogDistance, planned_range, seeded
from .routed import RouteEndpoint
from .secure import AppKeys, SecureTransport
from .wake import WakeAgent, WakeCaller

if TYPE_CHECKING:
    from .controller import Controller

Position = tuple[float, float]

#: Link model ``MeshNetwork`` uses unless given ``signal`` (user 2026-10-19:
#: simulate as close to a real deployment as possible). Unit tests of protocol
#: mechanics set it to ``None`` (``tests/conftest.py``): the disc is exact.
DEFAULT_SIGNAL: Optional[LogDistance] = LogDistance()
_DEFAULT = object()



def app_key(key: bytes, node: int) -> bytes:
    """A simulated device's application key (task Q14; real ones are random, written at install)."""

    return hashlib.sha256(b"app" + bytes(key) + bytes([node])).digest()[:16]


class GeoPort:
    """One radio on a ``GeoMedium``; implements ``mesh.RadioPort``."""

    def __init__(self, medium: "GeoMedium", node_id: int, position: Position) -> None:
        self._medium = medium
        self.node_id = node_id
        self.position = position
        self.enabled = True
        #: Channel the radio is tuned to; it hears only frames sent on it.
        self.channel = 0
        #: Spreading factor it sends and listens at (``None``: the medium's ``sf``).
        self.sf: Optional[int] = None
        #: Preamble sampling while dormant (``None``: a dormant radio hears nothing).
        self.sniffer: Optional[Sniffer] = None
        self.meter = EnergyMeter()
        self._receiver: Optional[RadioReceiver] = None

    def set_receiver(self, callback: RadioReceiver) -> None:
        self._receiver = callback

    def send(self, data: bytes, preamble: int = 8) -> None:
        """Put ``data`` on air; ``preamble`` symbols (long ones reach dormant radios)."""

        self._medium._transmit(self, bytes(data), preamble)

    @property
    def dormant(self) -> bool:
        return self.meter.is_dormant

    def set_dormant(self, dormant: bool) -> None:
        """Sleep (sampling with ``sniffer``) or wake (receiver always on)."""

        self.meter.sniffer = self.sniffer
        self.meter.set_dormant(dormant, self._medium.clock())

    def set_radio(self, channel: int, sf: int) -> None:
        """Tune to ``channel`` at ``sf`` (task Q11 / Q12)."""

        self.channel = channel
        self.sf = None if sf == self._medium.sf else sf

    def busy_until(self) -> float:
        """Channel activity detection: until when this radio hears or sends something."""

        return self._medium._busy_until(self.node_id)

    def _deliver(self, data: bytes, rssi: float) -> None:
        if self.enabled and self._receiver is not None:
            self._receiver(data, rssi)


@dataclass
class _Reception:
    port: GeoPort
    start: float
    end: float
    data: bytes
    rssi: float
    lost: bool = False


@dataclass(frozen=True)
class _Air:
    """What a sender's frame is on air with."""

    channel: int
    sf: int


class GeoMedium:
    """Radios at fixed positions; ``signal`` picks the link model (``None``: disc)."""

    def __init__(
        self,
        *,
        radio_range: float = 100.0,
        airtime: "float | Airtime" = 0.05,
        drop: float = 0.0,
        rssi_range: tuple[float, float] = (-120.0, -40.0),
        rng: Optional[random.Random] = None,
        clock: Callable[[], float] = time.monotonic,
        record: bool = False,
        signal: Optional[LogDistance] = None,
        sf: int = 7,
    ) -> None:
        self.radio_range = radio_range
        self.signal = signal
        #: SF of radios whose ``GeoPort.sf`` is ``None``; ``airtime`` is taken to be for it.
        self.sf = sf
        self.airtime = airtime
        self.drop = drop
        self.rssi_range = rssi_range
        self.rng = rng or random.Random()
        self.clock = clock
        self._lock = threading.Lock()
        self._ports: dict[int, GeoPort] = {}
        #: Task B4.5: the antenna site of each radio. A gateway's further
        #: radios share its site (same links, never hear one another), and
        #: with ``site_blocking`` one of them sending deafens the others
        #: (isolation not good enough; unmeasured, the bench runs both).
        self._site: dict[int, int] = {}
        self.site_blocking = False
        self._tx_until: dict[int, float] = {}
        # [MEM] last frame each radio put on air | 端: sim(仅 PC，供界面闪烁) | 上限: 节点数 | 每项: 3 个数
        self._last_tx: dict[int, tuple[float, float, int]] = {}
        self._receiving: dict[int, list[_Reception]] = {}
        self._events: list[tuple[float, int, _Reception]] = []
        self._counter = itertools.count()
        # [MEM] fixed shadowing per link | 端: sim(仅 PC) | 上限: 节点数² / 2 | 每项: 1 个数
        self._shadow: dict[tuple[int, int], float] = {}
        #: How often each radio was moved: a new place, new terrain on its links.
        self._moves: dict[int, int] = {}
        self.stats = {
            "transmissions": 0,
            "delivered": 0,
            "collisions": 0,
            "captured": 0,
            "half_duplex": 0,
            "dropped": 0,
            "too_weak": 0,
            "slept_through": 0,
            "blocked": 0,
        }
        #: ``(start, node_id, frame)`` per transmission when ``record`` is set.
        self.log: Optional[list[tuple[float, int, bytes]]] = [] if record else None

    # --------------------------------------------------------------- topology

    def add(self, node_id: int, position: Position, site: Optional[int] = None) -> GeoPort:
        """A radio at ``position``; ``site``: another radio of node ``site`` (task B4.5)."""

        if node_id in self._ports:
            raise ValueError(f"node 0x{node_id:02X} is already on the medium")
        if site is not None:
            position = self._ports[site].position
        port = GeoPort(self, node_id, position)
        port.meter.start(self.clock())
        self._ports[node_id] = port
        self._site[node_id] = node_id if site is None else site
        self._receiving[node_id] = []
        return port

    def _siblings(self, node_id: int) -> list[int]:
        site = self._site[node_id]
        return [other for other, at in self._site.items() if at == site and other != node_id]

    def port(self, node_id: int) -> GeoPort:
        return self._ports[node_id]

    def last_transmissions(self) -> dict[int, tuple[float, float, int]]:
        """``{node: (start, end, first byte)}`` of the last frame each radio sent.

        ``start`` may lie ahead (queued behind the radio's previous frame).
        The first byte of a sealed frame is ``KIND | 0x80`` (``| ROUTED`` for
        session traffic), readable without the key. For display only.
        """

        with self._lock:
            return dict(self._last_tx)

    def distance(self, a: int, b: int) -> float:
        (ax, ay), (bx, by) = self._ports[a].position, self._ports[b].position
        return math.hypot(ax - bx, ay - by)

    def neighbours(self, node_id: int) -> list[int]:
        """Radios that hear ``node_id``: in range, or (``signal``) at least half the time.

        Ignores channels; with ``signal`` it uses each listener's SF. A
        gateway's further radios (``site``) are not listed.
        """

        return [other for other in self._in_range(node_id) if self._site[other] == other]

    def _in_range(self, node_id: int) -> list[int]:
        site = self._site[node_id]
        others = [other for other in self._ports if self._site[other] != site]
        if self.signal is None:
            return [other for other in others if self.distance(node_id, other) <= self.radio_range]
        return [
            other for other in others if self.signal.received(self.median_rssi(node_id, other), self.sf_of(other))
        ]

    def sf_of(self, node_id: int) -> int:
        sf = self._ports[node_id].sf
        return self.sf if sf is None else sf

    def median_rssi(self, a: int, b: int) -> float:
        """RSSI of the ``a``–``b`` link before per-frame fading."""

        if self.signal is None:
            return self._rssi(a, b)
        a, b = self._site[a], self._site[b]  # one antenna site, one set of links
        pair = (a, b) if a < b else (b, a)
        shadow = self._shadow.get(pair)
        if shadow is None:
            moves = (self._moves.get(pair[0], 0), self._moves.get(pair[1], 0))
            shadow = self._shadow[pair] = self.signal.shadowing(a, b, moves=moves)
        signal = self.signal
        return signal.median_rssi(self.distance(a, b), self.radio_range, a, b, shadowing=shadow)

    def connected(self, a: int, b: int) -> bool:
        """Is there any chain of in-range, switched-on radios from ``a`` to ``b``?"""

        seen, todo = {a}, deque([a])
        while todo:
            node = todo.popleft()
            if node == b:
                return True
            for other in self.neighbours(node):
                if other not in seen and self._ports[other].enabled:
                    seen.add(other)
                    todo.append(other)
        return False

    # ------------------------------------------------------------------ radio

    def move(self, node_id: int, position: Position) -> None:
        """Put a radio somewhere else; the links it is on get fresh shadowing."""

        with self._lock:
            for radio in (node_id, *self._siblings(node_id)):
                self._ports[radio].position = position
            self._moves[node_id] = self._moves.get(node_id, 0) + 1
            for pair in [pair for pair in self._shadow if node_id in pair]:
                del self._shadow[pair]

    def _transmit(self, source: GeoPort, data: bytes, preamble: int = 8) -> None:
        if not source.enabled:
            return
        now = self.clock()
        with self._lock:
            sender = source.node_id
            air = _Air(source.channel, self.sf_of(sender))
            start = max(now, self._tx_until.get(sender, now))
            # Symbols beyond the standard 8 only lengthen the preamble.
            preamble_time = preamble * symbol_time(air.sf)
            end = start + self.airtime_of(len(data), source.sf) + (preamble - 8) * symbol_time(air.sf)
            source.meter.add_tx(end - start)
            self._tx_until[sender] = end
            self._last_tx[sender] = (start, end, data[0] if data else 0)
            self.stats["transmissions"] += 1
            if self.log is not None:
                self.log.append((start, sender, data))
            # A radio that starts sending stops hearing whatever overlaps.
            for reception in self._receiving[sender]:
                if reception.end > start and not reception.lost:
                    reception.lost = True
                    self.stats["half_duplex"] += 1
            siblings = self._siblings(sender) if self.site_blocking else []
            for sibling in siblings:
                for reception in self._receiving[sibling]:
                    if reception.end > start and not reception.lost:
                        reception.lost = True
                        self.stats["blocked"] += 1
            for node_id in self._hearers(sender, air):
                port = self._ports[node_id]
                if self.rng.random() < self.drop:
                    self.stats["dropped"] += 1
                    continue
                # A copy too weak to demodulate is taken to be too weak for
                # the CAD as well: it never wakes a dormant radio.
                rssi = self._copy_rssi(sender, node_id, air)
                if rssi is None:
                    continue
                if port.dormant:
                    noticed = self._sniffed(port, start, preamble_time)
                    if noticed is None:
                        continue
                    port.meter.add_dormant_rx(end - noticed)
                reception = _Reception(port, start, end, data, rssi)
                if self._tx_until.get(node_id, -math.inf) > start:
                    reception.lost = True
                    self.stats["half_duplex"] += 1
                elif self.site_blocking and any(
                    self._tx_until.get(sibling, -math.inf) > start for sibling in self._siblings(node_id)
                ):
                    reception.lost = True
                    self.stats["blocked"] += 1
                for other in self._receiving[node_id]:
                    if other.start < end and other.end > start:
                        self._collide(other, reception)
                self._receiving[node_id].append(reception)
                heapq.heappush(self._events, (end, next(self._counter), reception))

    def _hearers(self, sender: int, air: _Air) -> list[int]:
        """Switched-on radios tuned to ``air`` that could hear ``sender`` at all."""

        if self.signal is None:
            candidates = self._in_range(sender)
        else:
            site = self._site[sender]
            candidates = [other for other in self._ports if self._site[other] != site]
        return [
            node_id
            for node_id in candidates
            if self._ports[node_id].enabled
            and self._ports[node_id].channel == air.channel
            and self.sf_of(node_id) == air.sf
        ]

    def _sniffed(self, port: GeoPort, start: float, preamble: float) -> Optional[float]:
        """When a dormant radio notices this preamble, or ``None`` (lock held)."""

        if port.sniffer is None:
            self.stats["slept_through"] += 1
            return None
        noticed = port.sniffer.catches(start, preamble, self.rng)
        if noticed is None:
            self.stats["slept_through"] += 1
        return noticed

    def _copy_rssi(self, sender: int, receiver: int, air: _Air) -> Optional[float]:
        """RSSI of this copy, or ``None`` when it is too weak to demodulate."""

        if self.signal is None:
            return self._rssi(sender, receiver)
        rssi = self.signal.frame_rssi(self.median_rssi(sender, receiver), self.rng)
        if not self.signal.received(rssi, air.sf):
            self.stats["too_weak"] += 1
            return None
        return rssi

    def _collide(self, first: _Reception, second: _Reception) -> None:
        """Two copies overlap at one receiver: both lost, or the stronger is captured."""

        capture = self.signal.capture_db if self.signal is not None else None
        if not (first.lost and second.lost):
            self.stats["collisions"] += 1
        if capture is not None and abs(first.rssi - second.rssi) >= capture:
            weaker, stronger = sorted((first, second), key=lambda r: r.rssi)
            weaker.lost = True
            if not stronger.lost:
                self.stats["captured"] += 1
            return
        first.lost = second.lost = True

    def _busy_until(self, node_id: int) -> float:
        # Energy on the channel counts, even a frame already lost to a
        # collision: the radio's activity detection sees the preamble.
        now = self.clock()
        with self._lock:
            ends = [r.end for r in self._receiving[node_id] if r.start <= now < r.end]
            return max([self._tx_until.get(node_id, 0.0), *ends])

    def airtime_of(self, length: int, sf: Optional[int] = None) -> float:
        """Seconds a ``length``-byte frame is on air (at ``sf``, default the medium's)."""

        if sf is not None and sf != self.sf:
            return lora_airtime(length, sf)
        airtime = self.airtime
        return airtime(length) if callable(airtime) else airtime

    def _rssi(self, a: int, b: int) -> float:
        weak, strong = self.rssi_range
        return strong - (strong - weak) * min(1.0, self.distance(a, b) / self.radio_range)

    def tick(self, now: Optional[float] = None) -> None:
        """Deliver every frame whose airtime has ended."""

        now = self.clock() if now is None else now
        due: list[_Reception] = []
        with self._lock:
            while self._events and self._events[0][0] <= now:
                _, _, reception = heapq.heappop(self._events)
                self._receiving[reception.port.node_id].remove(reception)
                if not reception.lost:
                    due.append(reception)
                    self.stats["delivered"] += 1
        # Outside the lock: receivers may transmit in response.
        for reception in due:
            reception.port._deliver(reception.data, reception.rssi)

    def busy(self) -> bool:
        """Is any frame still on air?"""

        with self._lock:
            return bool(self._events)


def scatter(
    count: int, size: float, rng: random.Random, *, controller_id: int = DEFAULT_CONTROLLER_ID
) -> dict[int, Position]:
    """``count`` devices at random in a ``size`` × ``size`` square, keyed by id.

    Ids count up from 1, skipping the controller's id and the broadcast id.
    """

    ids = (n for n in itertools.count(1) if n not in (controller_id, BROADCAST_ID))
    return {
        node_id: (rng.uniform(0, size), rng.uniform(0, size))
        for node_id, _ in zip(ids, range(count))
    }


def line(count: int, spacing: float = 80.0) -> dict[int, Position]:
    """Devices 1..``count`` in a row from a controller at the origin: ``count`` hops.

    With the default 100 radio range each node hears only its neighbours.
    """

    return {node: (spacing * node, 0.0) for node in range(1, count + 1)}


def planned(spacing: float = 80.0) -> dict:
    """``MeshNetwork`` options for ``line`` / ``ladder`` under ``DEFAULT_SIGNAL``.

    A planned deployment: each ``spacing`` link gets the deployment margin
    (``radio_model.planned_range``). Under the disc the old geometry stands.
    """

    if DEFAULT_SIGNAL is None:
        return {}
    return {"radio_range": planned_range(spacing, model=DEFAULT_SIGNAL)}


def ladder(length: int, spacing: float = 80.0, gap: float = 50.0) -> dict[int, Position]:
    """Two rows of ``length`` (ids 1..length, then length+1..2·length).

    Every node has a neighbour in the other row to go round it, so a path
    survives one failed relay.
    """

    rows = line(length, spacing)
    rows.update({length + node: (x, gap) for node, (x, _) in line(length, spacing).items()})
    return rows


class MeshNetwork:
    """A controller and devices on one ``GeoMedium``, all on the secure link.

    ``gateway`` is the transport for the ``Controller``. Devices stay awake
    unless ``wake_options`` (below) starts them dormant. ``relay_options`` go to every
    ``MeshRelay``. Liveness is off by default: ``heartbeat_interval`` makes
    devices flood heartbeats, ``hello_interval`` gives each device a
    ``NeighbourWatch`` (``watch_options`` go to it) instead.

    Every device answers routed sessions (``RouteEndpoint``); passing
    ``gateway_options`` (``{}`` for the defaults) also builds the session
    ``Gateway`` as ``sessions``, which is then the controller's transport
    instead of ``gateway`` (call ``sessions.attach(controller)``).
    ``discovery_options`` gives every device a ``DiscoveryAgent`` (JOIN on
    start, answers to scans; task C) in ``agents``. ``signal`` is the link
    model (``radio_model.LogDistance``, task B1); left out, ``DEFAULT_SIGNAL``
    with shadowing drawn from ``seed``; ``None`` is the old disc.

    ``wake_options`` (task D2) makes devices start dormant, sampling the
    channel every ``period`` seconds (0.5; ``energy.Sniffer``, random phase per
    node; ``sniffer`` options go to it), each with a ``wake.WakeAgent`` in
    ``wakers`` (the other options go to it); every relay sends WAKE_NET with
    a preamble spanning one period, and ``waker`` (``wake.WakeCaller``)
    wakes the network from the controller; the session ``Gateway`` opens
    sessions with directed wake probes (``wake="probe"``). Only a dormant node's relay and
    wake agent run: its device, HELLOs and discovery agent sleep with it.

    ``app_keys`` (default on, task Q14): every device gets an application key
    of its own (``app_key``, derived for the simulation) and the controller
    all of them. ``gateway_options["radios"]`` (task B4.5) gives the gateway
    that many radios at its site, the extra ones in ``gateway_radios``.
    ``device_options`` go to every ``Device`` (e.g. ``action_time``: slow
    actions answer ACCEPTED and report a RESULT later).
    """

    def __init__(
        self,
        positions: Mapping[int, Position],
        *,
        key: bytes,
        controller_id: int = DEFAULT_CONTROLLER_ID,
        controller_position: Position = (0.0, 0.0),
        radio_range: float = 100.0,
        airtime: "float | Airtime" = 0.05,
        drop: float = 0.0,
        relay_options: Optional[dict] = None,
        gateway_options: Optional[dict] = None,
        heartbeat_interval: Optional[float] = None,
        hello_interval: Optional[float] = None,
        watch_options: Optional[dict] = None,
        discovery_options: Optional[dict] = None,
        seed: Optional[int] = None,
        clock: Callable[[], float] = time.monotonic,
        record: bool = False,
        signal: "Optional[LogDistance] | object" = _DEFAULT,
        wake_options: Optional[dict] = None,
        app_keys: bool = True,
        device_options: Optional[dict] = None,
    ) -> None:
        if signal is _DEFAULT:
            signal = seeded(DEFAULT_SIGNAL, seed)
        for node_id in positions:
            if node_id in (controller_id, BROADCAST_ID):
                raise ValueError(f"device id 0x{node_id:02X} is reserved (controller or broadcast)")
        rng = random.Random(seed)
        self.seed = seed
        self.clock = clock
        self.controller_id = controller_id
        self.medium = GeoMedium(
            radio_range=radio_range,
            airtime=airtime,
            drop=drop,
            rng=random.Random(rng.random()),
            clock=clock,
            record=record,
            signal=signal,
        )
        # [MEM] relays and devices | 端: sim(仅 PC) | 上限: 节点数 | 每项: 1 套协议栈
        self.relays: dict[int, MeshRelay] = {}
        self.devices: dict[int, Device] = {}
        #: Before this the devices' side has nothing to do (``_nodes_next``); a
        #: frame in or out, or a power switch, brings it forward (``touch``).
        self._nodes_due = -math.inf
        #: The same for the gateway's own relays (its radios).
        self._gateway_due = -math.inf
        self.endpoints: dict[int, RouteEndpoint] = {}
        self.watches: dict[int, NeighbourWatch] = {}
        self.agents: dict[int, DiscoveryAgent] = {}
        options = dict(relay_options or {})
        wake = dict(wake_options) if wake_options is not None else None
        sniffer: Optional[Sniffer] = None
        if wake is not None:
            # 0.5 s: the user's choice (2026-10-19) between idle current and wake speed.
            sniffer = Sniffer(period=wake.pop("period", 0.5), **wake.pop("sniffer", {}))
            options.setdefault("wake_preamble", preamble_for(sniffer, self.medium.sf))
            options.setdefault("symbol", symbol_time(self.medium.sf))
        self.wakers: dict[int, WakeAgent] = {}
        self.waker: Optional[WakeCaller] = None

        #: Task Q14: every device has an application key of its own (here
        #: derived from ``key`` and its id; on hardware written at install),
        #: and the controller holds them all. ``app_keys=False``: network key only.
        self.app_keys: Optional[AppKeys] = None
        if app_keys:
            self.app_keys = AppKeys({node: app_key(key, node) for node in positions})

        def stack(node_id: int, position: Position, forward: bool) -> SecureTransport:
            app = None
            if self.app_keys is not None:
                own = {node_id: app_key(key, node_id)} if node_id != controller_id else None
                app = AppKeys(own) if own is not None else self.app_keys
            relay, secure = radio_stack(
                self.medium.add(node_id, position),
                key,
                node_id,
                forward=forward,
                clock=clock,
                rng=random.Random(rng.random()),
                app=app,
                **options,
            )
            self.relays[node_id] = relay
            relay.activity = self.touch
            return secure

        self.gateway = stack(controller_id, controller_position, forward=False)
        self.relays[controller_id].activity = self._touch_gateway
        #: Every device's ``node.NodeStack``; ``devices``, ``endpoints``,
        #: ``watches``, ``wakers`` and ``agents`` name its layers.
        self.nodes: dict[int, NodeStack] = {}
        for node_id, position in positions.items():
            secure = stack(node_id, position, forward=True)
            port = self.medium.port(node_id)
            # Drawn in this order so that every run is the same as before ``NodeStack``.
            rngs = {"device": random.Random(rng.random())}
            if hello_interval is not None:
                rngs["watch"] = random.Random(rng.random())
            if wake is not None:
                port.sniffer = replace(sniffer, phase=rng.uniform(0.0, sniffer.period))
            if discovery_options is not None:
                rngs["agent"] = random.Random(rng.random())
            # Module events (v4) are seeded from the device's source without
            # drawing from it or from ``rng``: runs without events stay as they were.
            rngs["events"] = random.Random(repr(rngs["device"].getstate()[1][:8]))
            node = self.nodes[node_id] = NodeStack(
                self.relays[node_id],
                secure,
                port,
                node_id,
                controller_id=controller_id,
                clock=clock,
                rngs=rngs,
                heartbeat_interval=heartbeat_interval or math.inf,
                device_options=device_options,
                hello_interval=hello_interval,
                watch_options=watch_options,
                wake_options=wake,
                discovery_options=discovery_options,
            )
            self.endpoints[node_id] = node.endpoint
            self.devices[node_id] = node.device
            if node.watch is not None:
                self.watches[node_id] = node.watch
            if node.waker is not None:
                self.wakers[node_id] = node.waker
            if node.agent is not None:
                self.agents[node_id] = node.agent
        self._powered = set(self.devices)
        if wake is not None:
            self.waker = WakeCaller(self.gateway, self.relays[controller_id], controller_id, clock=clock)
        self.sessions: Optional[Gateway] = None
        #: Task B4.5: the gateway's further radios (``gateway_options``
        #: ``radios``: how many in all), each a relay on a port of its own at
        #: the gateway's site (``GeoPort.node_id`` = controller | k << 8).
        self.gateway_radios: list[MeshRelay] = []
        if gateway_options is not None:
            gateway_options = dict(gateway_options)
            extra = []
            for k in range(1, gateway_options.pop("radios", 1)):
                relay = MeshRelay(
                    self.medium.add(controller_id | k << 8, controller_position, site=controller_id),
                    key,
                    controller_id,
                    forward=False,
                    clock=clock,
                    rng=random.Random(rng.random()),
                    **options,
                )
                relay.sealer = self.gateway.seal_frame
                relay.activity = self._touch_gateway
                self.gateway_radios.append(relay)
                extra.append((SecureTransport(relay, key, initial_fcnt=0, shares=self.gateway), relay))
            if wake is not None:
                # A session's KEEPALIVE holds its path awake for LEASE + 5 s
                # after the last one: 15 s, not the always-awake 60 s (D6).
                gateway_options = {"wake": "probe", "lease": 15, **gateway_options}
            self.sessions = Gateway(
                self.gateway,
                self.relays[controller_id],
                controller_id,
                clock=clock,
                radios=extra,
                **gateway_options,
            )

    def controller(self, transport=None, **options) -> "Controller":
        """A ``Controller`` on this network (``sessions``, else ``gateway``), on its clock.

        With a ``seed`` its sequence start and EPOCH come from the seed too:
        left to ``Controller`` they come from the system and the wall clock,
        and message ids then change the order of sets keyed by them from one
        run to the next (measured: the same seeds gave other results).
        """

        from .controller import Controller

        if self.seed is not None:
            rng = random.Random(f"controller-{self.seed}")
            options.setdefault("rng", rng)
            options.setdefault("epoch", rng.randrange(256))
        options.setdefault("clock", self.clock)
        options.setdefault("allow_volatile_epoch", True)  # a simulation: no state file needed (review D1)
        if transport is None:
            transport = self.sessions if self.sessions is not None else self.gateway
        return Controller(transport, **options)

    #: Skip the devices' ticks while none of them has anything due (results
    #: are the same, bit for bit: those ticks change nothing). False: tick all.
    skip_idle = True

    def touch(self) -> None:
        """Something reached a device's stack from outside its own ticks: look at every device next tick."""

        self._nodes_due = -math.inf

    def _touch_gateway(self) -> None:
        self._gateway_due = -math.inf

    def tick(self, now: Optional[float] = None) -> None:
        self.medium.tick(now)
        at = self.clock() if now is None else now
        if not self.skip_idle or at >= self._gateway_due:
            gateway = self.relays[self.controller_id]
            gateway.tick(now)
            for relay in self.gateway_radios:
                relay.tick(now)
            self._gateway_due = min([gateway.next_due(), *(relay.next_due() for relay in self.gateway_radios)])
        if self.sessions is not None:
            self.sessions.tick(now)
        if self.skip_idle and at < self._nodes_due:
            return  # every device's tick would be a no-op
        for node_id in self._powered:
            self.nodes[node_id].tick(now)
        self._nodes_due = self._nodes_next()

    def _nodes_next(self) -> float:
        """The earliest time any powered device's stack has something to do."""

        return min((self.nodes[node_id].next_due() for node_id in self._powered), default=math.inf)

    def set_power(self, node_id: int, on: bool) -> None:
        """Switch a device's radio and main loop off or back on.

        Switching on is a reboot (``NodeStack.boot``): the FCNT, the
        controller's replay mark and the dedup records are kept, as if
        persisted (a real node must persist FCNT, see ``secure.py``, or
        every neighbour would drop its frames as replays); every other
        replay and relay window starts empty.
        """

        self.medium.port(node_id).enabled = on
        self.touch()
        if on and node_id not in self._powered:
            self.nodes[node_id].boot()
            self._powered.add(node_id)
        else:
            self._powered.discard(node_id)

    def route_records(self) -> dict:
        """Every node's ``MeshRelay.route_log``: the simulator's ground truth.

        On hardware the gateway collects them with a TRACE
        (``pathview.trace_view``); ``pathview.records_view`` reads these.
        """

        return {node: dict(relay.route_log) for node, relay in self.relays.items()}

    def move(self, node_id: int, position: Position) -> None:
        """Move a radio, e.g. the mobile controller (task D): its links are drawn anew."""

        self.medium.move(node_id, position)

    def reachable(self, node_id: int) -> bool:
        return self.medium.connected(self.controller_id, node_id)

    def quiet(self) -> bool:
        """Nothing on air and nothing waiting to be relayed or repeated."""

        live = [*(self.relays[n] for n in (self.controller_id, *self._powered)), *self.gateway_radios]
        return not self.medium.busy() and not any(relay.pending() for relay in live)
