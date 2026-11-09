"""TRACE / TRACE_REPLY codec: TraceRecord packing, routed-only frames, size bounds."""

from __future__ import annotations

import pytest

from cairnmesh.frames import (
    MAX_RELAYS,
    ROUTED,
    ROUTED_ONLY,
    TRACE_DONE,
    TRACE_GAVE_UP,
    TRACE_OTHER,
    TRACE_UNSEEN,
    FrameKind,
    TraceFrame,
    TraceRecord,
    TraceReplyFrame,
    add_route,
    decode_frame,
    decode_routed,
)

G = 0x42


# ------------------------------------------------------------- TraceRecord


def test_trace_states_are_distinct_two_bit_values():
    assert sorted({TRACE_UNSEEN, TRACE_DONE, TRACE_GAVE_UP, TRACE_OTHER}) == [0, 1, 2, 3]


def test_an_all_none_record_encodes_as_not_heard_and_round_trips():
    blank = TraceRecord()
    assert blank.encode() == bytes([0x00, 0x80, 0x00, 0x00])
    assert TraceRecord.from_bytes(blank.encode()) == blank
    decoded = TraceRecord.from_bytes(bytes([0, 0x80, 0, 0]))
    assert decoded.rssi_out is None and decoded.snr_out is None and decoded.rssi_back is None


@pytest.mark.parametrize(
    "record",
    [
        TraceRecord(-255, -127, -255, 3, 3, 3, 3),
        TraceRecord(-1, 127, -1, 0, 0, 0, 0),
        TraceRecord(-104, 0, -104, 1, TRACE_DONE, 2, TRACE_GAVE_UP),
        TraceRecord(-90, -1, None, 2, TRACE_OTHER, 0, TRACE_UNSEEN),
        TraceRecord(None, None, -120, 0, TRACE_UNSEEN, 3, TRACE_DONE),
        TraceRecord(-60, 40, -61, 3, TRACE_GAVE_UP, 1, TRACE_OTHER),
    ],
)
def test_a_record_round_trips_in_four_bytes(record):
    data = record.encode()
    assert len(data) == 4
    assert TraceRecord.from_bytes(data) == record


def test_every_two_bit_field_combination_round_trips():
    for out_repeats in range(4):
        for out_state in range(4):
            for back_repeats in range(4):
                for back_state in range(4):
                    record = TraceRecord(-100, -20, -101, out_repeats, out_state, back_repeats, back_state)
                    assert TraceRecord.from_bytes(record.encode()) == record


def test_record_wire_layout():
    record = TraceRecord(-104, -8, -99, 1, TRACE_DONE, 2, TRACE_GAVE_UP)
    flags = 1 << 6 | TRACE_DONE << 4 | 2 << 2 | TRACE_GAVE_UP
    assert record.encode() == bytes([104, 0xF8, 99, flags])


@pytest.mark.parametrize(
    "fields",
    [
        {"rssi_out": 0},
        {"rssi_out": 5},
        {"rssi_out": -256},
        {"rssi_back": 0},
        {"rssi_back": 1},
        {"rssi_back": -256},
        {"snr_out": -128},
        {"snr_out": 128},
        {"out_repeats": 4},
        {"out_state": 4},
        {"back_repeats": 4},
        {"back_state": 4},
        {"out_repeats": -1},
        {"back_state": -1},
    ],
)
def test_record_rejects_values_that_do_not_fit(fields):
    with pytest.raises(ValueError):
        TraceRecord(**fields)


# ------------------------------------------------------------- TraceFrame


def test_trace_and_reply_only_ever_travel_routed():
    assert FrameKind.TRACE == 0x0B and FrameKind.TRACE_REPLY == 0x0C
    assert FrameKind.TRACE in ROUTED_ONLY and FrameKind.TRACE_REPLY in ROUTED_ONLY


def test_trace_frame_is_seven_bytes_in_field_order():
    frame = TraceFrame(G, 5, 9, query_sequence=7, query_epoch=3, epoch=2)
    assert frame.encode() == bytes([0x0B, G, 5, 9, 2, 7, 3])
    assert TraceFrame.from_bytes(frame.encode()) == frame
    assert frame.message_id == 2 << 8 | 9


@pytest.mark.parametrize("relays", [(), (1,), (1, 2, 3, 4), tuple(range(1, MAX_RELAYS + 1))])
def test_trace_frame_round_trips_with_a_route(relays):
    frame = TraceFrame(G, 0x30, 0xFF, query_sequence=0, query_epoch=0xFF, epoch=0x80)
    routed = add_route(frame.encode(), relays)
    assert routed[0] == FrameKind.TRACE | ROUTED == 0x4B
    assert len(routed) == 7 + len(relays) + 1
    assert decode_routed(routed) == (frame, relays)


def test_trace_frame_rejects_bad_fields_and_layout():
    with pytest.raises(ValueError):
        TraceFrame(G, 5, 256, query_sequence=1, query_epoch=1)
    with pytest.raises(ValueError):
        TraceFrame(G, 5, 1, query_sequence=-1, query_epoch=1)
    with pytest.raises(ValueError):
        TraceFrame.from_bytes(bytes([0x0B, G, 5, 1, 0, 1]))  # 6 B
    with pytest.raises(ValueError):
        TraceFrame.from_bytes(bytes([0x0C, G, 5, 1, 0, 1, 1]))  # wrong kind


def test_decode_frame_rejects_unrouted_trace_frames():
    trace = TraceFrame(G, 5, 1, query_sequence=1, query_epoch=0).encode()
    reply = TraceReplyFrame(5, G, 1, (TraceRecord(),)).encode()
    for plain in (trace, reply):
        with pytest.raises(ValueError, match="route"):
            decode_frame(plain)
    with pytest.raises(ValueError):
        decode_frame(add_route(trace, (1, 2)))  # routed: decode_routed's job


# --------------------------------------------------------- TraceReplyFrame


@pytest.mark.parametrize("count", [1, 2, 5, MAX_RELAYS + 1])
def test_trace_reply_is_five_plus_four_per_record_and_round_trips(count):
    records = tuple(
        TraceRecord(-60 - n, n - 8, -61 - n, n % 4, (n + 1) % 4, (n + 2) % 4, (n + 3) % 4)
        for n in range(count)
    )
    frame = TraceReplyFrame(5, G, 9, records, epoch=4)
    plain = frame.encode()
    assert len(plain) == 5 + 4 * count
    assert plain[:5] == bytes([0x0C, 5, G, 9, 4])
    assert TraceReplyFrame.from_bytes(plain) == frame
    relays = tuple(range(10, 10 + min(count, MAX_RELAYS)))
    routed = add_route(plain, relays)
    assert routed[0] == FrameKind.TRACE_REPLY | ROUTED
    assert decode_routed(routed) == (frame, relays)


def test_trace_reply_record_count_bounds():
    assert MAX_RELAYS + 1 == 17
    TraceReplyFrame(5, G, 1, (TraceRecord(),) * 17)
    with pytest.raises(ValueError):
        TraceReplyFrame(5, G, 1, ())
    with pytest.raises(ValueError):
        TraceReplyFrame(5, G, 1, (TraceRecord(),) * 18)
    with pytest.raises(ValueError):
        TraceReplyFrame.from_bytes(bytes([0x0C, 5, G, 1, 0]) + TraceRecord().encode() * 18)


def test_trace_reply_from_bytes_rejects_bad_lengths_and_kind():
    head = bytes([0x0C, 5, G, 1, 0])
    record = TraceRecord(-90, 4, -91).encode()
    with pytest.raises(ValueError):
        TraceReplyFrame.from_bytes(head)  # no record
    with pytest.raises(ValueError):
        TraceReplyFrame.from_bytes(head + record[:3])  # not a whole record
    with pytest.raises(ValueError):
        TraceReplyFrame.from_bytes(head + record + b"\x00")
    with pytest.raises(ValueError):
        TraceReplyFrame.from_bytes(bytes([0x0B]) + head[1:] + record)


def test_trace_reply_takes_records_as_any_iterable():
    records = [TraceRecord(-70), TraceRecord(-80)]
    frame = TraceReplyFrame(5, G, 1, records)
    assert frame.records == tuple(records)
