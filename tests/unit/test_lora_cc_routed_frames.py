"""Wire format of routed sessions: new kinds, the route trailer, sizes.

Only the format; relaying and session behaviour are tested elsewhere.
"""

from __future__ import annotations

import random

import pytest

from cairnmesh.frames import (
    MAX_RELAYS,
    ROUTED,
    FrameKind,
    KeepaliveFrame,
    LinkQuality,
    ProbeFrame,
    ProbeHop,
    ProbeReplyFrame,
    ReplyFrame,
    RequestFrame,
    RouteErrorFrame,
    WakeFrame,
    add_route,
    decode_frame,
    decode_routed,
    reply_route,
    split_route,
)

G, T = 0x42, 0x30
RELAYS = tuple(range(1, MAX_RELAYS + 1))
GOOD = LinkQuality(-97, -14)  # -97 dBm, -3.5 dB


def probe(hops: int = 0) -> ProbeFrame:
    frame = ProbeFrame(sender_id=G, target_id=T, sequence=9, origin_id=G, window=20, epoch=4)
    for node in RELAYS[:hops]:
        frame = frame.extended(node, GOOD)
    return frame


# ------------------------------------------------------------- new kinds


@pytest.mark.parametrize(
    "frame, length",
    [
        (KeepaliveFrame(G, T, 1, lease=60, epoch=3), 7),
        (RouteErrorFrame(5, G, 1, broken=6, epoch=3), 6),
        (probe(0), 7),
        (probe(16), 7 + 3 * 16),
    ],
)
def test_new_frames_round_trip_with_their_documented_length(frame, length):
    data = frame.encode()
    assert len(data) == length
    if frame.kind == FrameKind.PROBE:
        assert decode_frame(data) == frame
    else:
        routed = add_route(data, (7,))
        assert decode_routed(routed) == (frame, (7,))


def test_link_quality_uses_whole_dbm_and_quarter_db():
    assert GOOD.encode() == bytes([97, 0xF2]) and LinkQuality.from_bytes(GOOD.encode()) == GOOD
    assert GOOD.snr_db == -3.5
    assert LinkQuality.measured(-300.4, 99.0) == LinkQuality(-255, 127)  # clamped
    assert LinkQuality.measured(-96.6, -3.4) == LinkQuality(-97, -14)
    with pytest.raises(ValueError):
        LinkQuality(5, 0)


def test_a_probe_grows_by_one_hop_per_relay_and_stops_at_sixteen():
    frame = probe(3)
    assert frame.sender_id == 3 and [hop.node_id for hop in frame.hops] == [1, 2, 3]
    assert frame.origin_id == G and frame.message_id == (4 << 8 | 9)
    assert frame.window_s == pytest.approx(1.0)
    assert probe(16).full and not probe(15).full
    with pytest.raises(ValueError):
        probe(16).extended(99, GOOD)


@pytest.mark.parametrize(
    "hops",
    [
        (ProbeHop(1, GOOD), ProbeHop(1, GOOD)),  # loop
        (ProbeHop(T, GOOD),),  # the target never relays its own probe
        (ProbeHop(G, GOOD),),
    ],
)
def test_a_probe_path_may_not_loop_or_contain_its_ends(hops):
    with pytest.raises(ValueError):
        ProbeFrame(hops[-1].node_id, T, 1, origin_id=G, window=1, hops=hops)


def test_a_probe_sender_is_its_last_hop():
    with pytest.raises(ValueError):
        ProbeFrame(sender_id=8, target_id=T, sequence=1, origin_id=G, window=1, hops=(ProbeHop(1, GOOD),))


def test_a_probe_may_follow_a_route_its_relays_still_have_to_pass():
    # Task C5: the gateway sends a probe along a path from its node book.
    routed = add_route(probe(1).encode(), (5, 6))
    frame, relays = decode_routed(routed)
    assert frame == probe(1) and relays == (5, 6)


# ---------------------------------------------------------- route trailer


def test_a_routed_request_is_the_plain_request_plus_its_route():
    request = RequestFrame(G, T, 7, action=3, value=1, epoch=2)
    routed = add_route(request.encode(), (11, 12, 13))
    assert routed[0] == FrameKind.REQUEST | ROUTED
    assert routed[1:7] == request.encode()[1:] and routed[7:] == bytes([11, 12, 13, 3])
    assert split_route(routed) == (request.encode(), (11, 12, 13))
    assert decode_routed(routed) == (request, (11, 12, 13))


def test_the_answer_walks_the_route_backwards():
    reply = ReplyFrame(T, G, 7, result=0, epoch=2)
    routed = add_route(reply.encode(), reply_route((11, 12, 13)))
    assert decode_routed(routed) == (reply, (13, 12, 11))


def test_largest_frames_fit_the_memory_budget():
    pytest.importorskip("cryptography")
    from cryptography.hazmat.primitives.ciphers.aead import AESCCM

    from cairnmesh.secure import seal

    aead = AESCCM(bytes(16), tag_length=4)
    request = add_route(RequestFrame(G, T, 1, 3, 1).encode(), RELAYS)
    path = ProbeReplyFrame(T, G, 1, links=(GOOD,) * (MAX_RELAYS + 1))
    answer = add_route(path.encode(), reply_route(RELAYS))
    frames = (request, probe(16).encode(), answer)
    assert [len(f) for f in frames] == [24, 55, 56]
    assert [len(seal(aead, f, 1)) for f in frames] == [33, 64, 65]


@pytest.mark.parametrize(
    "relays",
    [
        tuple(range(1, MAX_RELAYS + 2)),  # 17 relays: 18 hops
        (5, 5),
        (G,),
        (T,),
        (0xFF,),
    ],
)
def test_bad_routes_are_refused(relays):
    with pytest.raises(ValueError):
        add_route(WakeFrame(G, T, 1).encode(), relays)


def test_route_kinds_are_enforced_both_ways():
    keepalive = KeepaliveFrame(G, T, 1, lease=30).encode()
    with pytest.raises(ValueError):
        decode_frame(keepalive)  # a keepalive always travels routed
    with pytest.raises(ValueError):
        decode_frame(add_route(WakeFrame(G, T, 1).encode(), ()))  # routed: not for decode_frame
    with pytest.raises(ValueError):
        add_route(bytes([FrameKind.HELLO, 5, 0xFF, 1, 1]), ())
    hello_flagged = bytes([FrameKind.HELLO | ROUTED, 5, 0xFF, 1, 1, 0])
    with pytest.raises(ValueError):
        decode_routed(hello_flagged)


def test_a_probe_reply_lists_one_link_per_hop():
    path = ProbeReplyFrame(T, G, 1, links=(GOOD, GOOD, GOOD))
    assert decode_routed(add_route(path.encode(), (2, 1)))[0] == path
    with pytest.raises(ValueError):
        add_route(path.encode(), (1,))


@pytest.mark.parametrize(
    "trailer",
    [
        bytes([0x21]),  # format 1 is reserved for per-hop data rates
        bytes([0x11]),  # 17 relays
        bytes([9]),  # longer than the frame
    ],
)
def test_bad_trailers_are_refused(trailer):
    with pytest.raises(ValueError):
        split_route(bytes([FrameKind.WAKE | ROUTED, G, T, 1, 0]) + trailer)


def test_random_bytes_never_crash_the_routed_decoder():
    rng = random.Random(7)
    kinds = (0x41, 0x42, 0x43, 0x47, 0x48, 0x49, 0x4A)
    for _ in range(5000):
        data = bytes([rng.choice(kinds)]) + bytes(
            rng.randrange(256) for _ in range(rng.randrange(0, 40))
        )
        try:
            decode_routed(data)
        except ValueError:
            pass
