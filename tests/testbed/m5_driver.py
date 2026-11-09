"""Host-only M5 multi-hop driver: several virtual protocol stacks share fake gateway radios.

``FanoutFilteredPort`` is the one-port/many-stacks adapter. Each virtual node gets its own
``FilteredPort`` (so its own topology view and drop log), ``MeshRelay``, ``SecureTransport`` and
FCNT. Every frame received by the physical port is offered to every attached filter; every stack's
transmission goes through the same physical port under one lock.

``M5Driver`` adds a fake air medium, reads the topology text and a virtual-node layout such as
``66@0,1@1,2@2,3@3``, and runs the controlled downlink, HELLO liveness and duplicate-request
scenarios. Node IDs, keys and initial counters are constructor arguments. Keys never enter the
summary or logs.
"""

from __future__ import annotations

import argparse
import heapq
import json
import math
import os
import random
import re
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from cairnmesh.frames import RequestFrame
from cairnmesh.mesh import MeshRelay
from cairnmesh.node import NodeStack, radio_stack
from cairnmesh.secure import SecureTransport, transmitter

from . import topo as topo_mod
from .rxfilter import ENV, FilterError, FilteredPort, assert_no_active_filters



class _BranchPort:
    """One filter's private view of a shared physical port."""

    def __init__(self, owner: "FanoutFilteredPort") -> None:
        self._owner = owner
        self._receiver: Optional[Callable[..., None]] = None
        self._attached = True
        self.node: Optional[int] = None

    def set_receiver(self, callback: Callable[..., None]) -> None:
        self._receiver = callback

    def send(self, data: bytes, preamble: int = 8, **kwargs: object) -> None:
        self._owner._send(bytes(data), preamble=preamble, source_node=self.node, **kwargs)

    def _deliver(self, raw: bytes, *rest: object) -> None:
        if self._attached and self._receiver is not None:
            self._receiver(raw, *rest)

    def _detach(self) -> None:
        self._attached = False

    def __getattr__(self, name: str):
        if name.startswith("__") or name in {"_owner", "_receiver", "_attached", "node"}:
            raise AttributeError(name)
        return getattr(self._owner._physical, name)


class FanoutFilteredPort:
    """Give every virtual stack its own ``FilteredPort`` on one physical radio.

    The physical radio has one receiver callback, owned by this adapter. Each stack sees a private
    branch, so constructing a second stack cannot replace the first one's filter callback. Sends
    are serialized with ``_tx_lock``.
    """

    def __init__(
        self,
        physical,
        *,
        topo: Optional[str],
        log_size: int = 256,
        strict: bool = False,
        suspect_after: int = 5,
    ) -> None:
        if os.environ.get(ENV) != "1":
            raise FilterError(f"{ENV}=1 is not set: the test filter only starts on purpose")
        if topo is None or not str(topo).strip():
            raise FilterError("topo must be passed explicitly (the topology text, e.g. '66-1,1-2')")
        self._physical = physical
        self._topo = str(topo)
        self._links = topo_mod.parse_links(self._topo)
        self._log_size = log_size
        self._strict = strict
        self._suspect_after = suspect_after
        self._lock = threading.RLock()
        self._tx_lock = threading.RLock()
        self._branches: Dict[FilteredPort, _BranchPort] = {}
        self._by_node: Dict[int, FilteredPort] = {}
        self._closed = False
        physical.set_receiver(self._distribute)

    def _check_open(self) -> None:
        if self._closed:
            raise FilterError("this fan-out port is closed")

    def add_stack(self, *, node: int, key: bytes, **filter_options: object) -> FilteredPort:
        """Attach one virtual node; the returned port is what its protocol stack receives."""

        with self._lock:
            self._check_open()
            if node in self._by_node:
                raise ValueError(f"node {node} already has a stack on this port")
            if "topo" in filter_options or "key" in filter_options or "node" in filter_options:
                raise TypeError("node, key and topo are positional to add_stack")
            branch = _BranchPort(self)
            filtered = FilteredPort(
                branch,
                key=key,
                node=node,
                topo=self._topo,
                log_size=self._log_size,
                strict=self._strict,
                suspect_after=self._suspect_after,
                **filter_options,
            )
            branch.node = node
            self._branches[filtered] = branch
            self._by_node[node] = filtered
            return filtered

    def _send(
        self,
        data: bytes,
        preamble: int = 8,
        source_node: Optional[int] = None,
        **kwargs: object,
    ) -> None:
        with self._tx_lock:
            send_from = getattr(self._physical, "send_from", None)
            if source_node is not None and send_from is not None:
                send_from(source_node, data, preamble=preamble, **kwargs)
                return
            if kwargs:
                self._physical.send(data, preamble=preamble, **kwargs)
            elif preamble != 8:
                self._physical.send(data, preamble=preamble)
            else:
                self._physical.send(data)

    def _distribute(self, raw: bytes, *rest: object) -> None:
        with self._lock:
            branches = tuple(self._branches.values())
        errors: List[Exception] = []
        for branch in branches:
            try:
                branch._deliver(raw, *rest)
            except Exception as exc:  # one stack must not silence the others
                errors.append(exc)
        if errors:
            raise errors[0]

    def filter_for(self, node: int) -> FilteredPort:
        with self._lock:
            return self._by_node[node]

    def status(self) -> Dict[str, object]:
        with self._lock:
            filters = tuple(self._by_node.values())
        return {str(f.node): f.status() for f in sorted(filters, key=lambda item: item.node)}

    def drops(self) -> Dict[int, List[object]]:
        with self._lock:
            filters = tuple(self._by_node.values())
        return {f.node: list(f.log) for f in sorted(filters, key=lambda item: item.node)}

    def check(self) -> None:
        with self._lock:
            filters = tuple(self._by_node.values())
        for filtered in filters:
            filtered.check()

    def close(self) -> None:
        """Detach every branch first, then close all filters (idempotent)."""

        with self._lock:
            if self._closed:
                return
            self._closed = True
            branches = tuple(self._branches.values())
            filters = tuple(self._branches)
            self._branches.clear()
            self._by_node.clear()
        for branch in branches:
            branch._detach()
        if hasattr(self._physical, "set_receiver"):
            self._physical.set_receiver(None)
        errors: List[FilterError] = []
        for filtered in filters:
            try:
                filtered.close()
            except FilterError as exc:
                errors.append(exc)
        if errors:
            raise errors[0]

    def __getattr__(self, name: str):
        if name.startswith("__") or name in {"_physical", "_lock", "_tx_lock"}:
            raise AttributeError(name)
        return getattr(self._physical, name)


@dataclass(frozen=True)
class NodePlacement:
    """A virtual node and the physical radio index it is attached to."""

    node: int
    radio: int


_LAYOUT_ITEM = re.compile(r"^(\d+)(?:@|:)(\d+)$")


def parse_layout(text: Optional[str]) -> List[NodePlacement]:
    """Parse ``66@0,1@1 2:1`` into sorted placements; a node may occur only once."""

    if text is None or not str(text).strip():
        raise ValueError("layout must be passed explicitly (e.g. '66@0,1@1,2@2')")
    placements: List[NodePlacement] = []
    seen = set()
    for item in re.split(r"[,\s]+", str(text).strip()):
        if not item:
            continue
        match = _LAYOUT_ITEM.fullmatch(item)
        if match is None:
            raise ValueError(f"bad layout item {item!r} (want NODE@RADIO)")
        node, radio = int(match[1]), int(match[2])
        if not 1 <= node <= 254:
            raise ValueError(f"bad node in {item!r} (IDs 1..254)")
        if radio < 0:
            raise ValueError(f"bad radio in {item!r} (index >= 0)")
        if node in seen:
            raise ValueError(f"node {node} appears more than once in the layout")
        seen.add(node)
        placements.append(NodePlacement(node, radio))
    if not placements:
        raise ValueError("layout has no nodes")
    return sorted(placements, key=lambda placement: (placement.node, placement.radio))


@dataclass(frozen=True)
class _Delivery:
    due: float
    order: int
    destination: int
    data: bytes
    rssi: float


@dataclass(frozen=True)
class HostTransmission:
    radio: int
    source: Optional[int]
    tx: Optional[int]
    start: float
    end: float
    data: bytes
    preamble: int


class _FakeRadioPort:
    """One fake physical radio attached to ``HostRadioMedium``."""

    def __init__(self, medium: "HostRadioMedium", index: int) -> None:
        self._medium = medium
        self.index = index
        self.receiver: Optional[Callable[..., None]] = None
        self.channel = 0
        self.sf: Optional[int] = None
        self.enabled = True

    def set_receiver(self, callback: Optional[Callable[..., None]]) -> None:
        self.receiver = callback

    def send(self, data: bytes, preamble: int = 8, **_: object) -> None:
        if self.enabled:
            self._medium.transmit(self.index, bytes(data), preamble)

    def send_from(self, source_node: int, data: bytes, preamble: int = 8, **_: object) -> None:
        if self.enabled:
            self._medium.transmit(self.index, bytes(data), preamble, source_node)

    def set_radio(self, channel: int, sf: int) -> None:
        self.channel, self.sf = channel, sf

    def busy_until(self) -> float:
        return self._medium.busy_until(self.index)

    def _deliver(self, data: bytes, rssi: float) -> None:
        if self.enabled and self.receiver is not None:
            self.receiver(data, rssi)


class HostRadioMedium:
    """A deterministic fake air medium: all physical radios hear one another.

    Logical reach is still decided by each stack's ``FilteredPort``. A physical radio sends one
    frame at a time; frames on different radios do not collide in this host model.
    """

    def __init__(
        self,
        radios: int,
        *,
        clock: Callable[[], float],
        airtime: float = 0.01,
        symbol_time: float = 0.001,
        rssi: float = -70.0,
    ) -> None:
        if radios < 1:
            raise ValueError("the medium needs at least one radio")
        self.clock = clock
        self.airtime = airtime
        self.symbol_time = symbol_time
        self.rssi = rssi
        self.ports = [_FakeRadioPort(self, index) for index in range(radios)]
        self._lock = threading.RLock()
        self._port_locks = [threading.Lock() for _ in range(radios)]
        self._events: List[Tuple[float, int, _Delivery]] = []
        self._order = 0
        self._tx_until = [0.0] * radios
        self._rx_windows: List[List[Tuple[float, float]]] = [[] for _ in range(radios)]
        self.transmissions: List[HostTransmission] = []

    def transmit(
        self,
        source: int,
        data: bytes,
        preamble: int = 8,
        source_node: Optional[int] = None,
    ) -> None:
        with self._port_locks[source]:
            now = self.clock()
            start = max(now, self._tx_until[source])
            air = self.airtime + max(0, preamble - 8) * self.symbol_time
            end = start + air
            self._tx_until[source] = end
            tx = transmitter(data) if len(data) >= 9 else None
            self.transmissions.append(HostTransmission(source, source_node, tx, start, end, data, preamble))
            with self._lock:
                for destination in range(len(self.ports)):
                    if destination == source:
                        continue
                    self._order += 1
                    delivery = _Delivery(end, self._order, destination, data, self.rssi)
                    heapq.heappush(self._events, (end, self._order, delivery))
                    self._rx_windows[destination].append((start, end))

    def tick(self, now: Optional[float] = None) -> None:
        at = self.clock() if now is None else now
        due: List[_Delivery] = []
        with self._lock:
            while self._events and self._events[0][0] <= at:
                _, _, delivery = heapq.heappop(self._events)
                due.append(delivery)
            for windows in self._rx_windows:
                windows[:] = [(start, end) for start, end in windows if end > at]
        for delivery in due:
            self.ports[delivery.destination]._deliver(delivery.data, delivery.rssi)

    def busy_until(self, radio: int) -> float:
        now = self.clock()
        with self._lock:
            receiving = [end for start, end in self._rx_windows[radio] if start <= now < end]
            return max([self._tx_until[radio], *receiving], default=self._tx_until[radio])

    def busy(self) -> bool:
        with self._lock:
            return bool(self._events)


class FakeClock:
    def __init__(self, now: float = 0.0) -> None:
        self.now = float(now)

    def __call__(self) -> float:
        return self.now


@dataclass
class VirtualNode:
    node_id: int
    radio: int
    port: FilteredPort
    relay: MeshRelay
    secure: SecureTransport
    stack: NodeStack
    dedup_suppressed: int = 0


class M5Driver:
    """Host-side M5 experiment driver; see the module docstring and README.md."""

    def __init__(
        self,
        *,
        topo: Optional[str],
        layout: Optional[str],
        key: Optional[bytes] = None,
        keys: Optional[Mapping[int, bytes]] = None,
        gateway_id: int,
        initial_fcnt: object = 0,
        hello_interval: Optional[float] = None,
        watch_options: Optional[dict] = None,
        device_options: Optional[dict] = None,
        relay_options: Optional[dict] = None,
        seed: int = 1,
        airtime: float = 0.01,
        tick_step: float = 0.01,
        clock: Optional[Callable[[], float]] = None,
        strict: bool = True,
    ) -> None:
        if topo is None or not str(topo).strip():
            raise FilterError("topo must be passed explicitly (the topology text, e.g. '66-1,1-2')")
        if os.environ.get(ENV) != "1":
            raise FilterError(f"{ENV}=1 is not set: the test filter only starts on purpose")
        self.topo_text = str(topo)
        self.links = topo_mod.parse_links(self.topo_text)
        self.placements = parse_layout(layout)
        self.gateway_id = gateway_id
        nodes = topo_mod.nodes(self.links)
        placed = [placement.node for placement in self.placements]
        if placed != nodes:
            missing = sorted(set(nodes) - set(placed))
            extra = sorted(set(placed) - set(nodes))
            raise ValueError(f"layout must cover exactly the topology nodes (missing={missing}, extra={extra})")
        if gateway_id not in nodes:
            raise ValueError(f"gateway {gateway_id} is not in the topology")
        if key is not None and keys is not None:
            raise ValueError("pass key or keys, not both")
        if keys is None:
            if key is None:
                raise ValueError("key (or keys) must be passed explicitly")
            keys = {node: bytes(key) for node in nodes}
        self._keys = dict(keys)
        absent = [node for node in nodes if node not in self._keys]
        if absent:
            raise ValueError(f"no key for nodes {absent}")

        self.clock = clock or FakeClock()
        self.tick_step = tick_step
        self.seed = seed
        self._rng = random.Random(seed)
        self._next_request_sequence = self._rng.randrange(256)
        self._request_sends = 0
        self._closed = False
        self._ran = False
        by_radio = {placement.node: placement.radio for placement in self.placements}
        for left, right in self.links:
            if by_radio[left] == by_radio[right]:
                raise ValueError(
                    f"topology link {left}-{right} puts both ends on radio {by_radio[left]}; "
                    "one physical radio is half-duplex, so its virtual nodes cannot hear each other"
                )
        radio_count = max(placement.radio for placement in self.placements) + 1
        self.medium = HostRadioMedium(radio_count, clock=self.clock, airtime=airtime)
        self.fanouts: Dict[int, FanoutFilteredPort] = {
            radio: FanoutFilteredPort(self.medium.ports[radio], topo=self.topo_text, strict=strict)
            for radio in sorted({placement.radio for placement in self.placements})
        }
        self.nodes: Dict[int, VirtualNode] = {}
        relay_options = dict(relay_options or {})
        relay_options.pop("forward", None)
        try:
            for node in nodes:
                radio = by_radio[node]
                filtered = self.fanouts[radio].add_stack(node=node, key=self._keys[node])
                rng = random.Random(f"m5-{seed}-{node}")
                rngs = {
                    "device": random.Random(rng.random()),
                    "watch": random.Random(rng.random()),
                    "events": random.Random(rng.random()),
                    "agent": random.Random(rng.random()),
                }
                if isinstance(initial_fcnt, Mapping):
                    fcnt = int(initial_fcnt[node])
                else:
                    fcnt = int(initial_fcnt)
                relay, secure = radio_stack(
                    filtered,
                    self._keys[node],
                    node,
                    forward=node != gateway_id,
                    clock=self.clock,
                    rng=random.Random(rng.random()),
                    initial_fcnt=fcnt,
                    **relay_options,
                )
                stack = NodeStack(
                    relay,
                    secure,
                    filtered,
                    node,
                    controller_id=gateway_id,
                    clock=self.clock,
                    rngs=rngs,
                    heartbeat_interval=math.inf,
                    device_options=device_options,
                    hello_interval=hello_interval,
                    watch_options=watch_options,
                )
                self.nodes[node] = VirtualNode(node, radio, filtered, relay, secure, stack)
                self._instrument_dedup(node)
        except Exception:
            for fanout in self.fanouts.values():
                fanout.close()
            raise

    def _instrument_dedup(self, node: int) -> None:
        """Count requests the device suppresses as duplicates, without changing protocol code."""

        device = self.nodes[node].stack.device
        original = device.handle_frame
        seen: set[tuple[int, int]] = set()

        def counted(frame):
            if isinstance(frame, RequestFrame):
                request_id = (frame.sender_id, frame.message_id)
                if request_id in seen:
                    self.nodes[node].dedup_suppressed += 1
                else:
                    seen.add(request_id)
            return original(frame)

        device.handle_frame = counted  # type: ignore[method-assign]

    def __enter__(self) -> "M5Driver":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        errors: List[FilterError] = []
        for fanout in self.fanouts.values():
            try:
                fanout.close()
            except FilterError as exc:
                errors.append(exc)
        if errors:
            raise errors[0]

    def _check_running(self) -> None:
        if self._closed:
            raise RuntimeError("the M5 driver is closed")
        if self._ran:
            raise RuntimeError("an M5 driver runs once; construct another for another experiment")

    def tick(self, seconds: float, *, step: Optional[float] = None) -> None:
        if seconds < 0:
            raise ValueError("seconds must be >= 0")
        step = self.tick_step if step is None else step
        if step <= 0:
            raise ValueError("step must be > 0")
        if not hasattr(self.clock, "now"):
            raise ValueError("the M5 driver clock must expose a writable 'now' attribute")
        end = self.clock() + seconds
        while self.clock() + 1e-12 < end:
            now = min(end, self.clock() + step)
            self.clock.now = now  # type: ignore[attr-defined]
            self.medium.tick(now)
            for node in self.nodes.values():
                node.stack.tick(now)
        self.clock.now = end  # type: ignore[attr-defined]
        self.medium.tick(end)
        for node in self.nodes.values():
            node.stack.tick(end)

    def _sequence(self) -> int:
        sequence = self._next_request_sequence
        self._next_request_sequence = (sequence + 1) & 0xFF
        return sequence

    def _request(self, target: int, *, sequence: Optional[int], action: int, value: int) -> RequestFrame:
        if target == self.gateway_id:
            raise ValueError("a controlled downlink target must not be the gateway")
        if target not in self.nodes:
            raise ValueError(f"unknown target {target}")
        return RequestFrame(
            sender_id=self.gateway_id,
            target_id=target,
            sequence=self._sequence() if sequence is None else sequence,
            epoch=self.gateway_id,
            action=action,
            value=value,
        )

    def _send_request(self, frame: RequestFrame) -> None:
        self._request_sends += 1
        self.nodes[self.gateway_id].secure.send(frame.encode())

    def _handled(self) -> Dict[int, int]:
        return {node: item.stack.device.handled_count for node, item in self.nodes.items()}

    def run_controlled_flood(
        self,
        target: int,
        *,
        seconds: float = 8.0,
        action: int = 3,
        value: int = 1,
        sequence: Optional[int] = None,
    ) -> Dict[str, object]:
        """Originate one request at the gateway and let the network flood it."""

        before = self._handled()
        frame = self._request(target, sequence=sequence, action=action, value=value)
        self._send_request(frame)
        self.tick(seconds)
        after = self._handled()
        return {
            "request": {
                "sender": frame.sender_id,
                "target": frame.target_id,
                "sequence": frame.sequence,
                "epoch": frame.epoch,
            },
            "handled": {str(node): after[node] - before[node] for node in self.nodes},
        }

    def run_hello(self, *, seconds: float = 2.0) -> Dict[str, object]:
        """Run HELLO beacons and report each node's one-hop live-neighbour count."""

        watches = {node: item.stack.watch for node, item in self.nodes.items() if item.stack.watch is not None}
        if not watches:
            raise ValueError("HELLO needs hello_interval at construction")
        for watch in watches.values():
            watch.stats["hellos"] = 0
            watch._next_hello = self.clock() + 1e-9  # a zero-length phase must send nothing
        self.tick(seconds)
        return {
            "hello_delta": {str(node): watch.stats["hellos"] for node, watch in watches.items()},
            "neighbours": {
                str(node): {str(peer): rssi for peer, rssi in watch.neighbours().items()}
                for node, watch in watches.items()
            },
            "neighbour_count": {str(node): watch.neighbour_count() for node, watch in watches.items()},
        }

    def run_dedup(
        self,
        target: int,
        *,
        seconds: float = 8.0,
        action: int = 3,
        value: int = 1,
        sequence: Optional[int] = None,
    ) -> Dict[str, object]:
        """Send one message id twice with two FCNTs; the device action must run once."""

        before = self._handled()
        before_sends = self._request_sends
        before_sealed = self.nodes[self.gateway_id].secure.stats["sealed"]
        before_suppressed = {node: item.dedup_suppressed for node, item in self.nodes.items()}
        frame = self._request(target, sequence=sequence, action=action, value=value)
        self._send_request(frame)
        self.tick(seconds / 2)
        self._send_request(frame)
        self.tick(seconds - seconds / 2)
        after = self._handled()
        return {
            "request": {
                "sender": frame.sender_id,
                "target": frame.target_id,
                "sequence": frame.sequence,
                "epoch": frame.epoch,
            },
            "origin_sends": self._request_sends - before_sends,
            "origin_sealed": self.nodes[self.gateway_id].secure.stats["sealed"] - before_sealed,
            "handled": {str(node): after[node] - before[node] for node in self.nodes},
            "dedup_suppressed": {
                str(node): item.dedup_suppressed - before_suppressed[node] for node, item in self.nodes.items()
            },
        }

    def _snapshot(self) -> Dict[str, object]:
        nodes: Dict[str, object] = {}
        for node, item in self.nodes.items():
            watch = item.stack.watch
            device = item.stack.device
            actual_tx = sorted(
                {
                    sent.tx
                    for sent in self.medium.transmissions
                    if sent.source == node and sent.tx is not None
                }
            )
            tx_mismatch = sorted(
                {
                    sent.tx
                    for sent in self.medium.transmissions
                    if sent.source == node and sent.tx is not None and sent.tx != node
                }
            )
            nodes[str(node)] = {
                "radio": item.radio,
                "tx": actual_tx[0] if len(actual_tx) == 1 else None,
                "transmitted_tx": actual_tx,
                "tx_mismatch": tx_mismatch,
                "fcnt": item.secure.fcnt,
                "filter": item.port.status(),
                "relay": dict(item.relay.stats),
                "device": {
                    "handled": device.handled_count,
                    "dedup_suppressed": item.dedup_suppressed,
                    "accepted": device.stats["accepted"],
                    "results_sent": device.stats["results_sent"],
                },
                "hello": (
                    None
                    if watch is None
                    else {
                        "stats": dict(watch.stats),
                        "neighbours": sorted(watch.neighbours()),
                        "neighbour_count": watch.neighbour_count(),
                    }
                ),
            }
        drops: Dict[str, List[Dict[str, object]]] = {}
        for node, item in self.nodes.items():
            drops[str(node)] = [
                {
                    "sender": record.sender,
                    "tx": record.tx,
                    "kind": record.kind,
                    "fcnt": record.fcnt,
                    "rssi": record.rssi,
                    "mic_hash": record.mic_hash,
                }
                for record in item.port.log
            ]
        return {
            "topo": topo_mod.canonical(self.links),
            "layout": ",".join(f"{p.node}@{p.radio}" for p in self.placements),
            "gateway": self.gateway_id,
            "physical_transmissions": len(self.medium.transmissions),
            "transmission_tx": sorted(
                {sent.tx for sent in self.medium.transmissions if sent.tx is not None}
            ),
            "nodes": nodes,
            "drops": drops,
        }

    def run(
        self,
        scenarios: Sequence[str] = ("flood", "hello", "dedup"),
        *,
        target: int,
        flood_seconds: float = 8.0,
        hello_seconds: float = 2.0,
        dedup_seconds: float = 8.0,
    ) -> Dict[str, object]:
        """Run the named scenarios, collect the summary, close, and assert no filter is active."""

        self._check_running()
        self._ran = True
        result: Dict[str, object] = {"scenarios": {}}
        try:
            names = tuple(scenarios)
            allowed = {"flood", "hello", "dedup"}
            unknown = sorted(set(names) - allowed)
            if unknown:
                raise ValueError(f"unknown scenarios {unknown}; choose from {sorted(allowed)}")
            scenarios_out = result["scenarios"]
            assert isinstance(scenarios_out, dict)
            for name in names:
                if name == "flood":
                    scenarios_out[name] = self.run_controlled_flood(target, seconds=flood_seconds)
                elif name == "hello":
                    scenarios_out[name] = self.run_hello(seconds=hello_seconds)
                else:
                    scenarios_out[name] = self.run_dedup(target, seconds=dedup_seconds)
            result.update(self._snapshot())
        finally:
            try:
                self.close()
            finally:
                assert_no_active_filters()
        return result


def _summary_text(summary: Mapping[str, object]) -> str:
    return json.dumps(summary, indent=2, sort_keys=True)


def _parse_scenarios(text: str) -> Tuple[str, ...]:
    if text.strip() == "all":
        return ("flood", "hello", "dedup")
    return tuple(part.strip() for part in text.split(",") if part.strip())


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Run the host-only M5 multi-node driver")
    parser.add_argument("--topo", required=True, help="topology text, e.g. 66-1,1-2,2-3")
    parser.add_argument("--layout", required=True, help="node@radio list, e.g. 66@0,1@1,2@2,3@3")
    parser.add_argument("--key", required=True, help="shared 16-byte network key as hex")
    parser.add_argument("--node-key", action="append", default=[], metavar="NODE=HEX", help="per-node key override")
    parser.add_argument("--gateway", type=int, required=True)
    parser.add_argument("--target", type=int, required=True)
    parser.add_argument("--scenario", default="all", help="all or comma-separated flood,hello,dedup")
    parser.add_argument("--hello-interval", type=float, default=1.0)
    parser.add_argument("--flood-seconds", type=float, default=8.0)
    parser.add_argument("--hello-seconds", type=float, default=2.0)
    parser.add_argument("--dedup-seconds", type=float, default=8.0)
    parser.add_argument("--initial-fcnt", type=int, default=0)
    parser.add_argument("--seed", type=int, default=1)
    args = parser.parse_args(argv)

    try:
        shared = bytes.fromhex(args.key)
    except ValueError:
        parser.error("--key must be a 16-byte hex value")
    if len(shared) != 16:
        parser.error("--key must be a 16-byte hex value")
    keys = {node: shared for node in topo_mod.nodes(topo_mod.parse_links(args.topo))}
    for item in args.node_key:
        node_text, sep, value = item.partition("=")
        try:
            if not sep:
                raise ValueError
            node = int(node_text, 0)
            node_key = bytes.fromhex(value)
            if node not in keys or len(node_key) != 16:
                raise ValueError
        except ValueError:
            parser.error("--node-key must be NODE=HEX with a topology node and a 16-byte key")
        keys[node] = node_key
    scenarios = _parse_scenarios(args.scenario)
    driver = M5Driver(
        topo=args.topo,
        layout=args.layout,
        keys=keys,
        gateway_id=args.gateway,
        hello_interval=args.hello_interval if "hello" in scenarios else None,
        initial_fcnt=args.initial_fcnt,
        seed=args.seed,
    )
    summary = driver.run(
        scenarios,
        target=args.target,
        flood_seconds=args.flood_seconds,
        hello_seconds=args.hello_seconds,
        dedup_seconds=args.dedup_seconds,
    )
    print(_summary_text(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
