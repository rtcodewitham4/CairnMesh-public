"""Node discovery (task C): JOIN, DISCOVER and the scan.

Frame and agent tests are unit level (a fake radio, a stub transport); the
scan and JOIN scenarios run whole networks on a fake clock stepped in 5 ms
with the Semtech airtime. Seeds are fixed, nothing sleeps.
"""

from __future__ import annotations

import math
import random
from collections import Counter

import pytest

pytest.importorskip("cryptography")

from cryptography.hazmat.primitives.ciphers.aead import AESCCM

from cairnmesh.airtime import airtime_for
from cairnmesh.discovery import DiscoveryAgent
from cairnmesh.events import NodeFound, ScanFinished
from cairnmesh.frames import (
    BROADCAST_ID,
    DISCOVER_SPREAD_UNIT,
    MAX_NEIGHBOURS,
    PATH_KINDS,
    ROUTED,
    DiscoverFrame,
    DiscoverReplyFrame,
    FrameKind,
    JoinFrame,
    LinkQuality,
    NodeState,
    ProbeFrame,
    ProbeHop,
    ReplyFrame,
    ReplyResult,
    RequestFrame,
    add_route,
    decode_frame,
    decode_routed,
    is_routed,
)
from cairnmesh.gateway import SEAL_OVERHEAD, KnownNode, LatencyModel
from cairnmesh.mesh import MeshRelay
from cairnmesh.mesh_sim import MeshNetwork, ladder, line
from cairnmesh.secure import MIC_LEN, open_sealed, seal

KEY = bytes(range(16))
AEAD = AESCCM(KEY, tag_length=MIC_LEN)
G = 0x42
SF7 = airtime_for(7)
LINK = LinkQuality(-90, 20)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class Radio:
    def __init__(self) -> None:
        self.sent: list[bytes] = []
        self.receiver = None

    def send(self, data: bytes) -> None:
        self.sent.append(data)

    def set_receiver(self, callback) -> None:
        self.receiver = callback


class Sealer:
    def __init__(self) -> None:
        self.fcnt = 1000

    def __call__(self, plain: bytes) -> bytes:
        self.fcnt += 1
        return seal(AEAD, plain, self.fcnt)


class StubTransport:
    """Stands in for the node's ``SecureTransport``: records what the agent sends."""

    def __init__(self) -> None:
        self.sent: list[bytes] = []

    def send(self, data: bytes) -> None:
        self.sent.append(bytes(data))


def hops(*nodes: int) -> tuple[ProbeHop, ...]:
    return tuple(ProbeHop(node, LinkQuality(-80 - node, node - 40)) for node in nodes)


def links(count: int) -> tuple[LinkQuality, ...]:
    return tuple(LinkQuality(-70 - 5 * i, 4 * i - 8) for i in range(count))


# ------------------------------------------------------------------- frames


def test_join_and_discover_round_trip_with_their_hops():
    join = JoinFrame(sender_id=1, target_id=G, sequence=7, origin_id=3, window=10, hops=hops(2, 1), epoch=5)
    data = join.encode()
    assert data[0] == FrameKind.JOIN == 0x0D and len(data) == 7 + 3 * 2
    assert decode_frame(data) == join and isinstance(decode_frame(data), JoinFrame)

    discover = DiscoverFrame(
        sender_id=2, target_id=BROADCAST_ID, sequence=9, origin_id=G, window=12, spread=6,
        hops=hops(1, 2), epoch=4,
    )
    data = discover.encode()
    assert data[0] == FrameKind.DISCOVER == 0x0E and len(data) == 8 + 3 * 2
    assert data[7] == 6  # SPREAD sits right after WINDOW
    assert decode_frame(data) == discover and isinstance(decode_frame(data), DiscoverFrame)
    # No hops at all (as the origin sends it).
    bare = DiscoverFrame(sender_id=G, target_id=3, sequence=1, origin_id=G, window=10, spread=0)
    assert len(bare.encode()) == 8 and DiscoverFrame.from_bytes(bare.encode()) == bare


def test_extended_keeps_the_kind_and_spread_and_makes_the_relay_the_sender():
    join = JoinFrame(sender_id=3, target_id=G, sequence=7, origin_id=3, window=10, epoch=5)
    step = join.extended(2, LINK)
    assert isinstance(step, JoinFrame) and step.kind == FrameKind.JOIN
    assert step.sender_id == 2 and step.hops == (ProbeHop(2, LINK),)
    assert step.origin_id == 3 and step.message_id == join.message_id
    assert JoinFrame.from_bytes(step.encode()) == step

    discover = DiscoverFrame(sender_id=G, target_id=BROADCAST_ID, sequence=9, origin_id=G, window=12, spread=6)
    first = discover.extended(1, LINK)
    second = first.extended(2, LinkQuality(-100, -4))
    assert isinstance(second, DiscoverFrame) and second.kind == FrameKind.DISCOVER and second.spread == 6
    assert second.sender_id == 2 and [h.node_id for h in second.hops] == [1, 2]
    assert DiscoverFrame.from_bytes(second.encode()) == second
    assert discover.hops == ()  # the original is left alone


def test_a_path_frames_sender_must_be_its_last_hop():
    with pytest.raises(ValueError):
        JoinFrame(sender_id=9, target_id=G, sequence=1, origin_id=3, window=10, hops=hops(2))
    with pytest.raises(ValueError):
        DiscoverFrame(sender_id=9, target_id=G, sequence=1, origin_id=G, window=10, spread=1, hops=hops(2))


def test_path_frames_reject_a_wrong_kind_or_length():
    join = JoinFrame(sender_id=G, target_id=G, sequence=1, origin_id=G, window=10).encode()
    probe = ProbeFrame(sender_id=G, target_id=3, sequence=1, origin_id=G, window=10).encode()
    discover = DiscoverFrame(sender_id=G, target_id=3, sequence=1, origin_id=G, window=10, spread=2).encode()
    with pytest.raises(ValueError):
        JoinFrame.from_bytes(probe)
    with pytest.raises(ValueError):
        JoinFrame.from_bytes(discover)
    with pytest.raises(ValueError):
        DiscoverFrame.from_bytes(join)
    with pytest.raises(ValueError):
        DiscoverFrame.from_bytes(probe)
    # A JOIN's head is 7 bytes, a DISCOVER's 8: neither tolerates a part hop.
    with pytest.raises(ValueError):
        JoinFrame.from_bytes(join + b"\x01")
    with pytest.raises(ValueError):
        DiscoverFrame.from_bytes(discover + b"\x01\x02")
    with pytest.raises(ValueError):
        DiscoverFrame.from_bytes(discover[:7])  # no SPREAD byte
    with pytest.raises(ValueError):
        JoinFrame.from_bytes(join[:6])
    # More than 16 hops never fits.
    too_long = join + bytes(3 * 17)
    with pytest.raises(ValueError):
        JoinFrame.from_bytes(too_long)
    with pytest.raises(ValueError):
        decode_frame(b"")


def test_discover_spread_is_counted_in_quarter_seconds():
    assert DISCOVER_SPREAD_UNIT == 0.25
    frame = DiscoverFrame(sender_id=G, target_id=BROADCAST_ID, sequence=1, origin_id=G, window=10, spread=6)
    assert frame.spread_s == pytest.approx(1.5)
    assert DiscoverFrame(sender_id=G, target_id=3, sequence=1, origin_id=G, window=10).spread_s == 0.0
    assert frame.window_s == pytest.approx(0.5)
    with pytest.raises(ValueError):
        DiscoverFrame(sender_id=G, target_id=3, sequence=1, origin_id=G, window=10, spread=256)


@pytest.mark.parametrize("relays", [0, 3])
@pytest.mark.parametrize("neighbours", [0, 1, 2, 3])
def test_a_discover_reply_round_trips_with_its_route(relays, neighbours):
    route = (1, 2, 3)[:relays]
    reply = DiscoverReplyFrame(
        sender_id=9, target_id=G, sequence=11, state=int(NodeState.READY), links=links(relays + 1),
        neighbours=hops(*(20, 21, 22)[:neighbours]), epoch=6,
    )
    plain = reply.encode()
    assert plain[0] == FrameKind.DISCOVER_REPLY == 0x0F
    assert len(plain) == 6 + 2 * (relays + 1) + 3 * neighbours
    wire = add_route(plain, route)
    assert is_routed(wire) and wire[0] == FrameKind.DISCOVER_REPLY | ROUTED
    assert len(wire) == len(plain) + relays + 1
    decoded, got_route = decode_routed(wire)
    assert decoded == reply and got_route == route
    assert DiscoverReplyFrame.from_bytes(plain, relays + 1) == reply


def test_a_discover_reply_decodes_only_with_its_route():
    reply = DiscoverReplyFrame(sender_id=9, target_id=G, sequence=1, state=1, links=links(1))
    with pytest.raises(ValueError, match="route"):
        decode_frame(reply.encode())
    with pytest.raises(ValueError):
        decode_frame(add_route(reply.encode(), ()))  # routed: needs decode_routed


def test_a_discover_reply_holds_at_most_three_neighbours():
    with pytest.raises(ValueError):
        DiscoverReplyFrame(
            sender_id=9, target_id=G, sequence=1, state=1, links=links(1), neighbours=hops(20, 21, 22, 23)
        )
    assert MAX_NEIGHBOURS == 3
    # On the wire, a fourth neighbour is rejected as well.
    long = DiscoverReplyFrame(
        sender_id=9, target_id=G, sequence=1, state=1, links=links(1), neighbours=hops(20, 21, 22)
    ).encode() + bytes(3)
    with pytest.raises(ValueError):
        DiscoverReplyFrame.from_bytes(long, 1)
    with pytest.raises(ValueError):
        DiscoverReplyFrame(sender_id=9, target_id=G, sequence=1, state=1, links=())


def test_a_discover_reply_with_the_wrong_length_for_its_route_is_rejected():
    # Two links (4 B) cannot follow a route of no relays (1 link): 2 B left, not a neighbour.
    two = DiscoverReplyFrame(sender_id=9, target_id=G, sequence=1, state=1, links=links(2))
    with pytest.raises(ValueError):
        add_route(two.encode(), ())
    # One link on a route of one relay (needs 2).
    one = DiscoverReplyFrame(sender_id=9, target_id=G, sequence=1, state=1, links=links(1))
    with pytest.raises(ValueError):
        add_route(one.encode(), (1,))
    # A routed frame whose trailer claims another route length than its links fit.
    wire = bytearray(add_route(two.encode(), (1,)))
    wire[-1] = 0  # route length 0
    with pytest.raises(ValueError):
        decode_routed(bytes(wire[:-2]) + bytes([0]))
    # A frame cut short.
    good = add_route(DiscoverReplyFrame(sender_id=9, target_id=G, sequence=1, state=1, links=links(3)).encode(), (1, 2))
    with pytest.raises(ValueError):
        decode_routed(good[:5] + good[6:])
    with pytest.raises(ValueError):
        DiscoverReplyFrame.from_bytes(one.encode(), 0)
    with pytest.raises(ValueError):
        DiscoverReplyFrame.from_bytes(b"\x08" + one.encode()[1:], 1)  # not a DISCOVER_REPLY


def test_join_and_discover_cannot_carry_a_route():
    join = JoinFrame(sender_id=3, target_id=G, sequence=1, origin_id=3, window=10).encode()
    discover = DiscoverFrame(sender_id=G, target_id=3, sequence=1, origin_id=G, window=10, spread=1).encode()
    for plain in (join, discover):
        with pytest.raises(ValueError):
            add_route(plain, (1,))
        with pytest.raises(ValueError):
            add_route(plain, ())
    assert FrameKind.JOIN in PATH_KINDS and FrameKind.DISCOVER in PATH_KINDS
    assert FrameKind.DISCOVER_REPLY not in PATH_KINDS
    # A JOIN with ROUTED set in its kind is no JOIN.
    with pytest.raises(ValueError):
        decode_frame(bytes([join[0] | ROUTED]) + join[1:] + b"\x00")


# -------------------------------------------------------------- relay level


def mesh(positions, **options):
    """A network without a gateway layer: tests hook the relays' ``path_found`` themselves."""

    clock = FakeClock()
    network = MeshNetwork(positions, key=KEY, clock=clock, airtime=SF7, seed=5, record=True, **options)
    found: list[tuple[int, ProbeFrame, tuple[LinkQuality, ...]]] = []
    for node, node_relay in network.relays.items():
        node_relay.path_found = lambda frame, path, node=node: found.append((node, frame, path))
    return clock, network, found


def step(clock, network, seconds, controller=None) -> None:
    for _ in range(round(seconds / 0.005)):
        clock.now += 0.005
        network.tick(clock.now)
        if controller is not None:
            controller.tick(clock.now)


def originate(network, node, frame) -> None:
    relay = network.relays[node]
    relay.send(relay.sealer(frame.encode()))


def on_air(network, kind) -> list[tuple[int, bytes]]:
    """``(transmitter, plain)`` of every ``kind`` frame sent, in order."""

    out = []
    for _, sender, data in network.medium.log:
        plain, _ = open_sealed(AEAD, data)
        if plain[0] == kind:
            out.append((sender, plain))
    return out


def test_a_join_reaches_the_gateway_with_its_hops_in_order():
    clock, network, found = mesh(line(3))
    originate(network, 3, JoinFrame(sender_id=3, target_id=G, sequence=7, origin_id=3, window=10, epoch=1))
    step(clock, network, 3.0)
    assert [node for node, _, _ in found] == [G]  # nobody else is the JOIN's target
    _, frame, path = found[0]
    assert isinstance(frame, JoinFrame) and frame.origin_id == 3 and frame.sequence == 7
    # Appended from the node's side inward: 3 -> 2 -> 1 -> gateway.
    assert [hop.node_id for hop in frame.hops] == [2, 1]
    assert frame.sender_id == 1
    assert len(path) == len(frame.hops) + 1
    assert path[:2] == tuple(hop.link for hop in frame.hops)
    # The relays pass it on as frames of their own; the gateway never does.
    senders = [sender for sender, _ in on_air(network, FrameKind.JOIN)]
    assert senders[0] == 3 and set(senders) == {3, 2, 1}


def test_a_broadcast_discover_is_forwarded_once_and_collected_by_every_node():
    clock, network, found = mesh(line(3))
    originate(
        network, G,
        DiscoverFrame(sender_id=G, target_id=BROADCAST_ID, sequence=9, origin_id=G, window=10, spread=4, epoch=1),
    )
    step(clock, network, 4.0)
    assert Counter(node for node, _, _ in found) == {1: 1, 2: 1, 3: 1}  # one answer slot per node
    by_node = {node: (frame, path) for node, frame, path in found}
    for node, (frame, path) in by_node.items():
        assert isinstance(frame, DiscoverFrame) and frame.spread == 4
        assert [hop.node_id for hop in frame.hops] == list(range(1, node))
        assert len(path) == node  # one link per hop
    # Every node passed the scan on once (the last one repeats it unseen, same content).
    sent = on_air(network, FrameKind.DISCOVER)
    assert Counter(sender for sender, _ in sent)[G] == 1
    for node in (1, 2, 3):
        assert len({plain for sender, plain in sent if sender == node}) == 1
    assert Counter(sender for sender, _ in sent)[1] == 1 and Counter(sender for sender, _ in sent)[2] == 1


def test_parallel_paths_still_give_each_node_one_collected_scan():
    clock, network, found = mesh(ladder(3))
    originate(
        network, G,
        DiscoverFrame(sender_id=G, target_id=BROADCAST_ID, sequence=9, origin_id=G, window=20, spread=4, epoch=1),
    )
    step(clock, network, 6.0)
    counts = Counter(node for node, _, _ in found)
    assert set(counts) == set(network.devices) and set(counts.values()) == {1}
    for node, frame, path in found:
        assert len(path) == len(frame.hops) + 1


def test_a_discover_to_one_node_makes_only_that_node_collect_it():
    clock, network, found = mesh(line(3))
    originate(
        network, G,
        DiscoverFrame(sender_id=G, target_id=2, sequence=9, origin_id=G, window=10, spread=0, epoch=1),
    )
    step(clock, network, 4.0)
    assert [(node, [hop.node_id for hop in frame.hops]) for node, frame, _ in found] == [(2, [1])]
    senders = {sender for sender, _ in on_air(network, FrameKind.DISCOVER)}
    assert 1 in senders  # node 1 still carries it outward
    assert 3 not in senders  # the target keeps it; nobody beyond hears a copy


def test_a_probe_still_gets_its_automatic_reply_and_opens_a_session():
    clock = FakeClock()
    network = MeshNetwork(
        line(3), key=KEY, clock=clock, airtime=SF7, seed=5, gateway_options={},
        discovery_options={"join": False},
    )
    controller = network.controller(network.sessions, clock=clock, timeout=8.0, max_retries=2)
    network.sessions.attach(controller)
    session = network.sessions.open_session(3)
    for _ in range(4000):
        if session.state == "open":
            break
        step(clock, network, 0.005, controller)
    assert session.state == "open" and session.route == (1, 2)
    assert network.relays[3].stats["probes_answered"] == 1


# --------------------------------------------------------- agent, unit level


def agent(node: int = 5, **options):
    clock = FakeClock()
    node_relay = MeshRelay(Radio(), KEY, node, clock=clock, rng=random.Random(1), sealer=Sealer())
    transport = StubTransport()
    options.setdefault("epoch", 7)
    discovery = DiscoveryAgent(transport, node_relay, node, clock=clock, rng=random.Random(2), **options)
    return discovery, transport, node_relay, clock


def hear(node_relay: MeshRelay, plain: bytes, rssi: float = -90.0) -> None:
    """What ``MeshRelay`` does for each new authenticated frame: tell its observers."""

    for observer in node_relay.observers:
        observer(plain, rssi)


def join_ack(node: int, sequence: int, epoch: int = 7, sender: int = G) -> bytes:
    return add_route(ReplyFrame(sender, node, sequence, ReplyResult.OK, epoch=epoch).encode(), (1,))


def other_join(origin: int = 9, sequence: int = 3) -> bytes:
    return JoinFrame(sender_id=origin, target_id=G, sequence=sequence, origin_id=origin, window=10).encode()


def scan_copy(sender: int, sequence: int = 9, target: int = BROADCAST_ID, window: int = 10, spread: int = 4) -> bytes:
    path = hops(sender) if sender != G else ()
    return DiscoverFrame(
        sender_id=sender, target_id=target, sequence=sequence, origin_id=G, window=window, spread=spread,
        hops=path, epoch=1,
    ).encode()


def test_a_join_is_not_sent_before_its_spread_time():
    discovery, transport, _, clock = agent(join_spread=10.0)
    assert 0.0 <= discovery._join_at <= 10.0
    due = discovery._join_at
    clock.now = max(0.0, due - 0.01)
    if due > 0.0:
        discovery.tick(clock.now)
        assert transport.sent == [] and discovery.stats["joins"] == 0
    clock.now = due
    discovery.tick(clock.now)
    assert len(transport.sent) == 1 and discovery.stats["joins"] == 1
    frame = decode_frame(transport.sent[0])
    assert isinstance(frame, JoinFrame)
    assert (frame.sender_id, frame.origin_id, frame.target_id, frame.hops) == (5, 5, G, ())
    assert frame.window == 10 and frame.epoch == 7


def test_a_node_with_join_off_never_sends_one():
    discovery, transport, _, clock = agent(join=False)
    assert discovery._join_at is None
    for _ in range(100):
        clock.now += 10.0
        discovery.tick(clock.now)
    assert transport.sent == []


def test_an_unacknowledged_join_is_retried_with_doubling_backoff_and_fresh_ids():
    discovery, transport, _, clock = agent(join_spread=5.0, join_backoff=(30.0, 100.0), island_after=None)
    times = []
    for _ in range(6):
        clock.now = discovery._join_at
        times.append(clock.now)
        discovery.tick(clock.now)
    assert len(transport.sent) == 6
    gaps = [round(b - a, 6) for a, b in zip(times, times[1:])]
    assert gaps == [30.0, 60.0, 100.0, 100.0, 100.0]  # doubles up to the bound
    sent = [decode_frame(data) for data in transport.sent]
    assert len({frame.message_id for frame in sent}) == 6  # a fresh message id every time
    assert all(frame.epoch == 7 for frame in sent)
    # Nothing in between.
    clock.now += 50.0
    discovery.tick(clock.now)
    assert len(transport.sent) == 6


def _join_gaps(discovery, clock, count):
    times = []
    for _ in range(count):
        clock.now = discovery._join_at
        times.append(clock.now)
        discovery.tick(clock.now)
    return [round(b - a, 6) for a, b in zip(times, times[1:])]


def test_a_node_that_never_hears_the_controller_joins_at_the_island_pace():
    discovery, transport, _, clock = agent(join_spread=5.0, join_backoff=(30.0, 100.0), island_after=3)
    # Three JOINs unanswered and nothing from the controller: an island.
    assert _join_gaps(discovery, clock, 5) == [30.0, 60.0, 3600.0, 3600.0]
    assert discovery.island and len(transport.sent) == 5


def test_hearing_the_controller_ends_the_island_pace():
    discovery, transport, node_relay, clock = agent(join_spread=5.0, join_backoff=(30.0, 100.0), island_after=3)
    _join_gaps(discovery, clock, 3)
    assert discovery.island and discovery._join_at - clock.now == 3600.0
    # Any frame the controller sent, here a flood to another node.
    clock.now += 10.0
    hear(node_relay, RequestFrame(G, 9, 1, 3, 1, epoch=0).encode())
    assert not discovery.island
    assert discovery._join_at <= clock.now + 5.0  # within join_spread, not an hour
    gaps = _join_gaps(discovery, clock, 3)
    assert gaps == [30.0, 60.0]  # the usual doubling again


def test_a_node_that_heard_the_controller_never_takes_the_island_pace():
    discovery, transport, node_relay, clock = agent(join_spread=5.0, join_backoff=(30.0, 100.0), island_after=3)
    # A scan passed on by a relay: its ORIGIN is the controller.
    scan = DiscoverFrame(
        sender_id=3, target_id=0x20, sequence=1, origin_id=G, window=1, hops=(ProbeHop(3, LINK),), spread=0, epoch=0
    )
    hear(node_relay, scan.encode())
    assert _join_gaps(discovery, clock, 6) == [30.0, 60.0, 100.0, 100.0, 100.0]
    assert not discovery.island


def test_the_gateways_reply_to_the_latest_join_ends_the_retries():
    discovery, transport, node_relay, clock = agent(join_spread=5.0, join_backoff=(30.0, 100.0))
    for _ in range(2):
        clock.now = discovery._join_at
        discovery.tick(clock.now)
    first, latest = (decode_frame(data) for data in transport.sent)
    hear(node_relay, join_ack(5, latest.sequence, sender=0x10))  # not the gateway
    hear(node_relay, join_ack(5, latest.sequence, epoch=8))  # another run
    hear(node_relay, join_ack(5, (latest.sequence + 1) & 0xFF))  # another message
    hear(node_relay, join_ack(6, latest.sequence))  # another node
    assert not discovery.joined
    hear(node_relay, join_ack(5, latest.sequence))
    assert discovery.joined and discovery.stats["joined"] == 1
    for _ in range(5):
        clock.now += 400.0
        discovery.tick(clock.now)
    assert len(transport.sent) == 2


def test_start_forgets_the_join_and_schedules_a_new_one():
    discovery, transport, node_relay, clock = agent(join_spread=5.0)
    clock.now = discovery._join_at
    discovery.tick(clock.now)
    hear(node_relay, join_ack(5, decode_frame(transport.sent[0]).sequence))
    assert discovery.joined
    clock.now += 100.0
    discovery.start()
    assert not discovery.joined and clock.now <= discovery._join_at <= clock.now + 5.0
    clock.now = discovery._join_at
    discovery.tick(clock.now)
    assert len(transport.sent) == 2


# ---------------------------------------------------------------- storm rules


def test_hearing_another_nodes_join_defers_ours():
    discovery, _, node_relay, clock = agent(join_spread=10.0, join_quiet=3.0)
    clock.now = discovery._join_at - 0.1 if discovery._join_at > 0.2 else 0.0
    due = discovery._join_at
    hear(node_relay, other_join(9))
    # At least `quiet` from now, at most twice that.
    assert discovery._join_at >= clock.now + 3.0 and discovery._join_at <= clock.now + 6.0
    assert discovery._join_at >= due
    # A JOIN of our own (heard back from a relay) is no reason to wait.
    pushed = discovery._join_at
    hear(node_relay, other_join(5))
    assert discovery._join_at == pushed


def test_a_join_already_further_out_is_not_pulled_in_by_a_quiet_wait():
    discovery, _, node_relay, clock = agent(join_spread=10.0, join_quiet=3.0)
    discovery._join_at = 100.0
    hear(node_relay, other_join(9))
    assert discovery._join_at == 100.0


def test_join_quiet_none_turns_the_deferral_off():
    discovery, _, node_relay, clock = agent(join_spread=10.0, join_quiet=None)
    discovery._join_at = 1.0
    hear(node_relay, other_join(9))
    assert discovery._join_at == 1.0


def test_a_joined_node_does_not_wait_for_anyone():
    discovery, transport, node_relay, clock = agent(join_spread=5.0, join_quiet=3.0)
    clock.now = discovery._join_at
    discovery.tick(clock.now)
    hear(node_relay, join_ack(5, decode_frame(transport.sent[0]).sequence))
    pushed = discovery._join_at
    hear(node_relay, other_join(9))
    assert discovery._join_at == pushed


def test_hearing_a_scan_for_everyone_or_for_us_holds_the_join():
    discovery, _, node_relay, clock = agent(join_spread=0.0)
    discovery._join_at = 0.0
    # window 10 (0.5 s) + spread 40 (10 s): hold for 10.5 s, plus as much again at random.
    hear(node_relay, scan_copy(G, spread=40))
    assert 10.5 <= discovery._join_at <= 21.0

    discovery._join_at = 0.0
    hear(node_relay, scan_copy(G, sequence=10, target=5, spread=40))  # addressed to us
    assert 10.5 <= discovery._join_at <= 21.0

    discovery._join_at = 0.0
    hear(node_relay, scan_copy(G, sequence=11, target=9, spread=40))  # for another node
    assert discovery._join_at == 0.0


def test_without_join_by_scan_a_scan_does_not_hold_the_join():
    discovery, _, node_relay, _ = agent(join_spread=0.0, join_by_scan=False)
    discovery._join_at = 0.0
    hear(node_relay, scan_copy(G, spread=40))
    assert discovery._join_at == 0.0


def path_for(spread: int = 4, sequence: int = 9, parent: int = 1) -> DiscoverFrame:
    return DiscoverFrame(
        sender_id=parent, target_id=BROADCAST_ID, sequence=sequence, origin_id=G, window=10, spread=spread,
        hops=hops(parent), epoch=1,
    )


def test_answering_a_scan_sets_joined_and_sends_a_routed_reply_within_the_spread():
    discovery, transport, node_relay, clock = agent(join_spread=50.0, state=lambda: NodeState.SLEEP)
    assert not discovery.joined
    node_relay.path_found(path_for(spread=4), links(2))
    assert discovery.stats["answers"] == 0  # not before its random slot
    clock.now = 1.0 + 1e-9  # SPREAD 4 * 0.25 s
    discovery.tick(clock.now)
    assert len(transport.sent) == 1 and discovery.stats["answers"] == 1
    assert discovery.joined and discovery._join_id is None
    reply, route = decode_routed(transport.sent[0])
    assert isinstance(reply, DiscoverReplyFrame)
    assert (reply.sender_id, reply.target_id, reply.sequence, reply.epoch) == (5, G, 9, 1)
    assert reply.state == int(NodeState.SLEEP) and reply.links == links(2)
    assert route == (1,)  # the path it kept, walked backwards
    # The same scan is answered once.
    node_relay.path_found(path_for(spread=4), links(2))
    clock.now += 10.0
    discovery.tick(clock.now)
    assert len(transport.sent) == 1


def test_the_answer_slot_is_random_within_the_spread():
    slots = set()
    for seed in range(6):
        discovery, _, node_relay, clock = agent(join_spread=50.0)
        discovery._rng = random.Random(seed)
        node_relay.path_found(path_for(spread=8), links(2))
        (slot, _), = discovery._answers
        assert 0.0 <= slot <= 2.0
        slots.add(round(slot, 6))
    assert len(slots) > 1


def test_without_join_by_scan_answering_a_scan_leaves_the_join_pending():
    discovery, transport, node_relay, clock = agent(join_spread=50.0, join_by_scan=False)
    node_relay.path_found(path_for(spread=0), links(2))
    clock.now = 1.0
    discovery.tick(clock.now)
    assert len(transport.sent) == 1 and discovery.stats["answers"] == 1
    assert not discovery.joined


def test_a_scan_answer_lists_the_three_strongest_neighbours_but_not_the_parent():
    heard_neighbours = {7: -50.0, 8: None, 20: -120.0}
    discovery, transport, node_relay, clock = agent(
        join_spread=50.0, neighbours=lambda: heard_neighbours
    )
    # Copies of the scan heard from others: parent 1 (strongest), 2, 3, 4, 5 (ourselves).
    for sender, rssi in ((1, -60.0), (2, -80.0), (3, -90.0), (4, -70.0), (5, -40.0), (6, -100.0)):
        hear(node_relay, scan_copy(sender), rssi)
    node_relay.path_found(path_for(spread=0, parent=1), links(2))
    clock.now = 0.1
    discovery.tick(clock.now)
    reply, _ = decode_routed(transport.sent[0])
    found = [hop.node_id for hop in reply.neighbours]
    # Strongest first: 7 (-50 from the HELLO table), 4 (-70), 2 (-80); no parent, no self, no unknown RSSI.
    assert found == [7, 4, 2]
    assert [hop.link.rssi for hop in reply.neighbours] == [-50, -70, -80]
    assert len(reply.neighbours) <= MAX_NEIGHBOURS


def test_a_node_beside_the_gateway_has_no_neighbours_to_list_when_it_heard_nobody():
    discovery, transport, node_relay, clock = agent(join_spread=50.0)
    node_relay.path_found(
        DiscoverFrame(sender_id=G, target_id=BROADCAST_ID, sequence=9, origin_id=G, window=10, spread=0, epoch=1),
        links(1),
    )
    clock.now = 0.1
    discovery.tick(clock.now)
    reply, route = decode_routed(transport.sent[0])
    assert reply.neighbours == () and route == () and len(reply.links) == 1


# --------------------------------------------------- whole networks: JOIN


def build(positions, **options):
    clock = FakeClock()
    gateway_options = options.pop("gateway_options", {})
    options.setdefault("record", True)
    network = MeshNetwork(
        positions, key=KEY, clock=clock, airtime=SF7, gateway_options=gateway_options, seed=3, **options
    )
    controller = network.controller(network.sessions, clock=clock, timeout=8.0, max_retries=2)
    network.sessions.attach(controller)
    events: list = []
    controller.events.subscribe(events.append)
    return clock, network, controller, events


def run(clock, network, controller, seconds, until=None) -> None:
    end = clock.now + seconds
    while clock.now < end and not (until is not None and until()):
        clock.now += 0.005
        network.tick(clock.now)
        controller.tick(clock.now)


def all_joined(network) -> bool:
    return all(a.joined for a in network.agents.values()) and set(network.sessions.nodes) >= set(network.agents)


def test_every_node_joins_and_the_gateway_learns_its_route():
    clock, network, controller, events = build(
        line(4), discovery_options={"join_spread": 5.0}, gateway_options={"join_storm": None}
    )
    run(clock, network, controller, 120.0, lambda: all_joined(network))
    assert all_joined(network)
    nodes = network.sessions.nodes
    assert set(nodes) == {1, 2, 3, 4}
    for node, known in nodes.items():
        assert known.how == "join"
        assert known.route == tuple(range(1, node))  # relays from the gateway outward
        assert len(known.links) == known.hops == node
        assert known.state is None
    assert network.sessions.scan_state is None  # two or three JOINs are no storm: none was started
    found = [event for event in events if isinstance(event, NodeFound)]
    assert sorted(event.node_id for event in found) == [1, 2, 3, 4]
    for event in found:
        assert event.new and event.how == "join"
        assert event.route == nodes[event.node_id].route
        assert len(event.links) == len(event.route) + 1
        assert event.neighbours == ()
    assert not [event for event in events if isinstance(event, ScanFinished)]


def test_a_rebooted_node_joins_again():
    clock, network, controller, events = build(
        line(3), discovery_options={"join_spread": 5.0}, gateway_options={"join_storm": None}
    )
    run(clock, network, controller, 120.0, lambda: all_joined(network))
    assert all_joined(network)
    before = network.agents[3].stats["joins"]
    network.set_power(3, False)
    run(clock, network, controller, 5.0)
    network.set_power(3, True)  # a reboot: RAM is gone, it must announce itself again
    assert not network.agents[3].joined
    mark = len(events)
    run(clock, network, controller, 120.0, lambda: network.agents[3].joined)
    assert network.agents[3].joined and network.agents[3].stats["joins"] > before
    again = [event for event in events[mark:] if isinstance(event, NodeFound) and event.node_id == 3]
    assert again and not again[0].new and again[0].how == "join"
    assert network.sessions.nodes[3].route == (1, 2)


def test_a_join_storm_starts_a_scan_that_finds_everyone():
    clock, network, controller, events = build(line(4), discovery_options={"join_spread": 5.0})
    gateway = network.sessions
    assert gateway.scan_state is None
    run(clock, network, controller, 60.0, lambda: gateway.scan_state is not None)
    assert gateway.scan_state is not None  # three JOINs inside the window
    run(clock, network, controller, 200.0, lambda: gateway.scan_state.done)
    assert gateway.scan_state.done
    assert set(gateway.nodes) == {1, 2, 3, 4}
    finished = [event for event in events if isinstance(event, ScanFinished)]
    assert len(finished) == 1 and finished[0].missing == ()
    run(clock, network, controller, 60.0, lambda: all(a.joined for a in network.agents.values()))
    assert all(a.joined for a in network.agents.values())


def test_two_joins_are_no_storm():
    clock, network, controller, _ = build(line(2), discovery_options={"join_spread": 5.0})
    run(clock, network, controller, 120.0, lambda: all_joined(network))
    assert all_joined(network) and network.sessions.scan_state is None


def test_three_joins_within_the_window_start_a_scan_but_spread_out_ones_do_not():
    clock, network, controller, _ = build(
        line(3), discovery_options={"join": False}, gateway_options={"join_storm": 3, "join_storm_window": 15.0}
    )
    gateway = network.sessions
    for index in range(3):
        join = JoinFrame(sender_id=1, target_id=G, sequence=index, origin_id=1, window=10, epoch=1)
        gateway._on_path(join, (LINK,))
        assert (gateway.scan_state is not None) == (index == 2)
        clock.now += 1.0
    assert not gateway.scan_state.done

    clock2, network2, controller2, _ = build(
        line(3), discovery_options={"join": False}, gateway_options={"join_storm": 3, "join_storm_window": 15.0}
    )
    for index in range(5):
        join = JoinFrame(sender_id=1, target_id=G, sequence=index, origin_id=1, window=10, epoch=1)
        network2.sessions._on_path(join, (LINK,))
        clock2.now += 10.0  # never three inside 15 s
    assert network2.sessions.scan_state is None


# --------------------------------------------------- whole networks: scan


def adjacent(network, path) -> bool:
    return all(network.medium.distance(a, b) <= network.medium.radio_range for a, b in zip(path, path[1:]))


def scan_all(positions, count):
    clock, network, controller, events = build(
        positions, discovery_options={"join": False}, gateway_options={"expected_nodes": count}
    )
    scan = network.sessions.scan()
    run(clock, network, controller, 200.0, lambda: scan.done)
    return clock, network, controller, events, scan


@pytest.mark.parametrize(
    ("name", "positions", "count"),
    [("line", line(5), 5), ("ladder", ladder(4), 8)],
)
def test_a_scan_finds_every_node_with_a_working_route(name, positions, count):
    clock, network, controller, events, scan = scan_all(positions, count)
    assert scan.done and scan.rounds == 1
    nodes = network.sessions.nodes
    assert set(nodes) == set(positions)
    for node, known in nodes.items():
        assert known.how == "scan"
        assert known.state == int(NodeState.READY)
        assert len(known.links) == known.hops == len(known.route) + 1
        assert node not in known.route and G not in known.route
        assert adjacent(network, (G, *known.route, node))  # every hop of it is a real link
        assert len(known.neighbours) <= MAX_NEIGHBOURS
        parent = known.route[-1] if known.route else G
        ids = [hop.node_id for hop in known.neighbours]
        assert node not in ids and parent not in ids
        for neighbour in known.neighbours:
            assert network.medium.distance(node, neighbour.node_id) <= network.medium.radio_range
        strengths = [hop.link.rssi for hop in known.neighbours]
        assert strengths == sorted(strengths, reverse=True)
    if name == "line":
        assert {node: known.route for node, known in nodes.items()} == {n: tuple(range(1, n)) for n in range(1, 6)}
        assert nodes[1].neighbours[0].node_id == 2
    finished = [event for event in events if isinstance(event, ScanFinished)]
    everyone = tuple(sorted(positions))
    assert len(finished) == 1
    assert finished[0].found == everyone and finished[0].missing == () and finished[0].new == everyone
    found = [event for event in events if isinstance(event, NodeFound)]
    assert sorted({event.node_id for event in found}) == list(everyone)
    first = {event.node_id: event for event in reversed(found)}
    assert all(event.how == "scan" for event in found)
    assert all(first[node].state == int(NodeState.READY) for node in everyone)
    assert sum(event.new for event in found) == len(everyone)  # new exactly once per node
    assert all(a.joined for a in network.agents.values())  # answering a scan stands in for a JOIN


@pytest.mark.parametrize(("positions", "count"), [(line(5), 5), (ladder(4), 8)])
def test_a_known_node_that_goes_silent_is_asked_again_and_reported_missing(positions, count):
    clock, network, controller, events, first = scan_all(positions, count)
    assert first.done
    silent = max(positions)
    network.set_power(silent, False)
    second = network.sessions.scan()
    assert second is not first
    run(clock, network, controller, 300.0, lambda: second.done)
    assert second.done
    finished = [event for event in events if isinstance(event, ScanFinished)][-1]
    others = tuple(sorted(set(positions) - {silent}))
    assert finished.missing == (silent,)
    assert finished.found == others and finished.new == ()
    assert second.known == frozenset(positions)
    # After the round to everyone, the silent node was asked on its own:
    # along its known path first, then by a DISCOVER flood.
    assert second.routed == 1
    assert len(second.ids) > second.rounds
    assert silent in network.sessions.nodes  # still on the books, just unheard of


class _Deaf(list):
    """A ``DiscoveryAgent._answers`` that drops every answer: the node is there but its scan answer is lost."""

    def append(self, item) -> None:
        pass


def test_a_node_whose_scan_answer_was_lost_is_asked_along_its_known_path():
    clock, network, controller, events, first = scan_all(line(5), 5)
    before = network.sessions.nodes[5]
    network.agents[5]._answers = _Deaf()
    network.medium.log = []
    second = network.sessions.scan()
    run(clock, network, controller, 300.0, lambda: second.done)
    assert second.done
    finished = [event for event in events if isinstance(event, ScanFinished)][-1]
    assert finished.found == (1, 2, 3, 4, 5) and finished.missing == ()
    # One routed PROBE answered it: no DISCOVER flood to that node.
    assert second.routed == 1 and len(second.ids) == second.rounds
    (sender, plain), *_ = on_air(network, FrameKind.PROBE | ROUTED)
    probe, relays = decode_routed(plain)
    assert sender == G and probe.target_id == 5 and relays == (1, 2, 3, 4)
    after = network.sessions.nodes[5]
    assert after.how == "scan" and after.route == (1, 2, 3, 4) and after.seen_at > before.seen_at
    # A PROBE answer carries no state or neighbours: the scan answer's stand.
    assert after.state == before.state and after.neighbours == before.neighbours


def test_a_neighbour_named_by_an_answer_is_asked_through_the_node_that_named_it():
    clock, network, controller, events = build(
        line(5), discovery_options={"join": False}, gateway_options={"expected_nodes": 5}
    )
    gateway = network.sessions
    network.agents[5]._answers = _Deaf()  # 5 never answers a DISCOVER: only 4's answer names it
    network.medium.log = []
    scan = gateway.scan()
    run(clock, network, controller, 300.0, lambda: scan.done)
    assert 5 in scan.mentioned and 5 in scan.answered
    assert scan.routed == 1 and len(scan.ids) == scan.rounds
    (_, plain), *_ = on_air(network, FrameKind.PROBE | ROUTED)
    assert decode_routed(plain)[1] == gateway.nodes[4].route + (4,)


def test_without_scan_routed_silent_nodes_are_asked_by_a_discover_flood():
    clock, network, controller, events = build(
        line(5), discovery_options={"join": False}, gateway_options={"expected_nodes": 5, "scan_routed": False}
    )
    network.agents[5]._answers = _Deaf()
    scan = network.sessions.scan()
    run(clock, network, controller, 300.0, lambda: scan.done)
    assert scan.routed == 0 and len(scan.ids) > scan.rounds
    assert not on_air(network, FrameKind.PROBE | ROUTED)


def test_scan_while_one_runs_returns_the_same_scan():
    clock, network, controller, events = build(
        line(3), discovery_options={"join": False}, gateway_options={"expected_nodes": 3}
    )
    scan = network.sessions.scan()
    run(clock, network, controller, 1.0)
    assert not scan.done
    assert network.sessions.scan() is scan
    assert network.sessions.scan(expected_nodes=50) is scan
    run(clock, network, controller, 200.0, lambda: scan.done)
    assert scan.done and len(scan.ids) == 1
    assert network.sessions.scan() is not scan  # a finished one is replaced


def test_expected_nodes_floors_the_size_of_the_answer_window():
    def spread_of(gateway_expected, asked=None):
        clock, network, controller, _ = build(
            line(3), discovery_options={"join": False}, gateway_options={"expected_nodes": gateway_expected}
        )
        gateway = network.sessions
        scan = gateway.scan(asked)
        # No node known yet: each unknown one counts as 4 hops out.
        model, factor = gateway.model, gateway.scan_spread_factor
        count = max(1, gateway_expected, asked or 0)
        units = math.ceil(model.scan_spread([4] * count, factor=factor) / DISCOVER_SPREAD_UNIT)
        assert scan.spread == pytest.approx(units * DISCOVER_SPREAD_UNIT)
        # The DISCOVER on the air says the same.
        run(clock, network, controller, 0.5)
        sent = [DiscoverFrame.from_bytes(plain) for _, plain in on_air(network, FrameKind.DISCOVER)]
        assert sent == [] or sent[0].spread_s == pytest.approx(scan.spread)
        return scan.spread

    small, big = spread_of(0), spread_of(0, asked=10)
    assert big > small
    assert spread_of(10) == pytest.approx(big)
    assert spread_of(10, asked=3) == pytest.approx(big)  # max(), not a sum or the latest


def test_the_first_scan_goes_out_as_a_broadcast_discover_with_the_modelled_window():
    clock, network, controller, _ = build(
        line(3), discovery_options={"join": False}, gateway_options={"expected_nodes": 3}
    )
    network.medium.log = []
    scan = network.sessions.scan()
    run(clock, network, controller, 1.0)
    (sender, plain), *_ = on_air(network, FrameKind.DISCOVER)
    frame = DiscoverFrame.from_bytes(plain)
    assert sender == G and frame.target_id == BROADCAST_ID and frame.origin_id == G
    assert frame.spread_s == pytest.approx(scan.spread)
    assert (frame.sequence, frame.epoch) in scan.ids


def test_too_many_silent_nodes_for_one_by_one_questions_trigger_another_full_round():
    clock, network, controller, events = build(
        line(4),
        discovery_options={"join": False},
        gateway_options={"expected_nodes": 4, "scan_followups": 2, "scan_rounds": 2},
    )
    gateway = network.sessions
    ghosts = (100, 101, 102, 103)
    for ghost in ghosts:
        gateway.nodes[ghost] = KnownNode(ghost, (), (LINK,), 0.0, "join")
    scan = gateway.scan()
    run(clock, network, controller, 600.0, lambda: scan.done)
    assert scan.done
    assert scan.rounds == 2  # capped by scan_rounds
    # Two rounds to everyone, then only the first scan_followups silent nodes asked.
    assert len(scan.ids) == 2 + 2
    finished = [event for event in events if isinstance(event, ScanFinished)][0]
    assert finished.found == (1, 2, 3, 4) and finished.missing == ghosts


def test_a_node_that_never_spoke_and_is_named_by_nobody_stays_unknown():
    clock, network, controller, events = build(
        line(3), discovery_options={"join": False}, gateway_options={"expected_nodes": 3}
    )
    network.set_power(3, False)
    scan = network.sessions.scan()
    run(clock, network, controller, 300.0, lambda: scan.done)
    finished = [event for event in events if isinstance(event, ScanFinished)][0]
    assert finished.found == (1, 2) and finished.missing == () and finished.new == (1, 2)
    # Nobody named node 3 (it never sent anything), so the gateway cannot know it exists.
    assert 3 not in network.sessions.nodes


# ------------------------------------------------------------ latency model


def model() -> LatencyModel:
    return LatencyModel(SF7, jitter=0.05, slack=0.1, answer=0.1)


def test_answer_length_counts_links_neighbours_route_and_the_seal():
    assert LatencyModel.answer_length(1, 3) == 6 + 2 * 1 + 3 * 3 + 0 + 1 + SEAL_OVERHEAD
    assert LatencyModel.answer_length(4, 0) == 6 + 2 * 4 + 0 + 3 + 1 + SEAL_OVERHEAD
    assert LatencyModel.answer_length(2) == LatencyModel.answer_length(2, MAX_NEIGHBOURS)
    # It is what really goes on the wire.
    for relays in (0, 3):
        for neighbours in (0, 3):
            reply = DiscoverReplyFrame(
                sender_id=9, target_id=G, sequence=1, state=1, links=links(relays + 1),
                neighbours=hops(*(20, 21, 22)[:neighbours]),
            )
            sealed = seal(AEAD, add_route(reply.encode(), (1, 2, 3)[:relays]), 1)
            assert len(sealed) == LatencyModel.answer_length(relays + 1, neighbours)


def test_scan_sends_counts_discovers_and_answers():
    assert LatencyModel.scan_sends([1, 2]) == 2 + 1 + (2 + 3)
    assert LatencyModel.scan_sends([]) == 1
    assert LatencyModel.scan_sends([4]) == 1 + 1 + 5


def test_answer_busy_stops_growing_at_three_hops():
    m = model()
    for hops_out in (1, 2, 3, 5, 8):
        expected = (min(hops_out, 3) + 1) * SF7(LatencyModel.answer_length(hops_out))
        assert m.answer_busy(hops_out) == pytest.approx(expected)
    # Beyond three hops only the longer frame costs more, not a longer queue.
    assert m.answer_busy(8) / SF7(LatencyModel.answer_length(8)) == pytest.approx(4)
    assert m.answer_busy(2) / SF7(LatencyModel.answer_length(2)) == pytest.approx(3)


def test_scan_spread_grows_with_the_nodes_and_with_the_factor():
    m = model()
    one, two, five = m.scan_spread([2]), m.scan_spread([2, 2]), m.scan_spread([2] * 5)
    assert 0 < one < two < five
    assert two == pytest.approx(2 * one) and five == pytest.approx(5 * one)
    assert m.scan_spread([3, 1]) == pytest.approx(2.0 * (m.answer_busy(3) + m.answer_busy(1)))
    assert m.scan_spread([2], factor=4.0) == pytest.approx(2 * m.scan_spread([2], factor=2.0))
    assert m.scan_spread([2], factor=0.0) == 0.0
    assert m.scan_spread([]) == 0.0


def test_scan_time_adds_the_spread_one_for_one():
    m = model()
    base = m.scan(3, 0.5, 0.15, 0.0)
    assert m.scan(3, 0.5, 0.15, 2.0) == pytest.approx(base + 2.0)
    assert m.scan(5, 0.5, 0.15, 0.0) > base  # further out: more hops each way


def test_a_scan_that_finds_fewer_than_expected_is_repeated_a_bounded_number_of_times():
    clock = FakeClock()
    network = MeshNetwork(line(3), key=KEY, clock=clock, airtime=SF7, seed=5,
                          gateway_options={"expected_nodes": 4, "rescan_after": 30.0, "rescans": 2},
                          discovery_options={"join": False})
    controller = network.controller(network.sessions, clock=clock)
    network.sessions.attach(controller)
    gateway = network.sessions
    first = gateway.scan()
    scans = {id(first)}
    for _ in range(int(400 / 0.005)):
        clock.now += 0.005
        network.tick(clock.now)
        controller.tick(clock.now)
        scans.add(id(gateway.scan_state))
    assert set(gateway.nodes) == {1, 2, 3}  # a fourth node does not exist
    assert len(scans) == 3 and gateway.scan_state.done  # the scan and two more, then it stops


def test_a_scan_that_finds_everyone_expected_is_not_repeated():
    clock = FakeClock()
    network = MeshNetwork(line(3), key=KEY, clock=clock, airtime=SF7, seed=5,
                          gateway_options={"expected_nodes": 3, "rescan_after": 30.0},
                          discovery_options={"join": False})
    controller = network.controller(network.sessions, clock=clock)
    network.sessions.attach(controller)
    first = network.sessions.scan()
    for _ in range(int(200 / 0.005)):
        clock.now += 0.005
        network.tick(clock.now)
        controller.tick(clock.now)
    assert network.sessions.scan_state is first and first.done
