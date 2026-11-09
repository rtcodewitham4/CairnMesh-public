"""Test vectors of the wire format (v5, ``protocol_spec.md`` section 16).

    python -m cairnmesh.vectors > tests/vectors/lora_v5.json

v2 (2026-10-21, user) = v1 plus the RESULT frame and REPLY ``ACCEPTED``:
the v1 entries come first, byte for byte as in the frozen
``tests/vectors/lora_v1.json``, which is never regenerated.
v3 (2026-10-21, user, review C1) = v2 with one change: a REPORT under the
application layer carries SUBJECT and EVENT in clear, authenticated (inner
AAD) instead of encrypted, so neighbours can suppress duplicate reports.
Only the ``report`` entry differs from the frozen ``lora_v2.json``.
v4 (2026-10-22, user) = v3 plus the EVENT frame (0x13, section 19): a
module's event id, sealed end to end, acknowledged by a network-only REPLY.
The v3 entries come first, byte for byte as in the frozen ``lora_v3.json``.
v5 (2026-11-08) = v4 plus the result code ``INTERRUPTED = 0x04``
(the device reset with the action open: no final result, the action may have
completed). No byte layout changes; the v4 entries come first, byte for byte as
in the frozen ``lora_v4.json``, which is never regenerated.

One example of every frame kind, plain (what the layers above the security
layer see), below the security layer (what a relay sees after opening the
network envelope: with the application layer of section 15.4 where it
applies) and on air (sealed); plus semantic vectors: a relay passing a frame
on (TX changed, nothing else), the replay window, and a recorded command
re-sealed under a fresh counter (refused by the application layer). The
firmware (``firmware/proto/vectors_test.go``, board ``cmd/selftest``) is checked against the same file; ``tests/conformance/test_lora_cc_vectors.py``
keeps it in step with this code.
"""

from __future__ import annotations

import dataclasses
import json
import sys
from enum import IntEnum

from cryptography.hazmat.primitives.ciphers.aead import AESCCM

from . import frames as F
from .secure import MIC_LEN, AppKeys, ReplayWindow, SecureTransport, open_sealed, reseal, seal

VERSION = "v5"
NETWORK_KEY = b"0123456789abcdef"
#: Application key of node 0x01 (section 15.4); the controller holds it too.
APP_KEY = bytes(range(0x10, 0x20))
G, NODE, BROADCAST = 0x42, 0x01, 0xFF
#: Every sender's counter starts here.
FIRST_FCNT = 77


class _Wire:
    def __init__(self) -> None:
        self.sent: list[bytes] = []

    def set_receiver(self, callback) -> None:
        pass

    def send(self, data: bytes) -> None:
        self.sent.append(bytes(data))


def _fields(frame) -> dict:
    def plain(value):
        if isinstance(value, IntEnum):
            return int(value)
        if dataclasses.is_dataclass(value):
            return {f.name: plain(getattr(value, f.name)) for f in dataclasses.fields(value) if f.init}
        if isinstance(value, (tuple, list)):
            return [plain(item) for item in value]
        return value

    return {"class": type(frame).__name__, **plain(frame)}


def _examples() -> list:
    """``(name, frame, route or None)``; a route lists the relays from SENDER to TARGET."""

    link = F.LinkQuality(-97, 22)
    weak = F.LinkQuality(-118, -20)
    hop = F.ProbeHop(0x07, link)
    record = F.TraceRecord(rssi_out=-97, snr_out=22, rssi_back=-99, out_repeats=1, out_state=1, back_state=1)
    return [
        ("wake", F.WakeFrame(G, NODE, 0x05, epoch=0x64), None),
        ("request", F.RequestFrame(G, NODE, 0x05, action=3, value=100, epoch=0x64), None),
        ("reply_to_request", F.ReplyFrame(NODE, G, 0x05, result=0, epoch=0x64), None),
        ("reply_to_wake", F.ReplyFrame(NODE, G, 0x06, result=0, epoch=0x64), None),
        ("heartbeat", F.HeartbeatFrame(NODE, G, 0x09, state=F.NodeState.READY), None),
        ("hello", F.HelloFrame(NODE, 0x0A, state=F.NodeState.READY), None),
        ("report", F.ReportFrame(NODE, G, 0x02, subject=0x03, event=F.ReportEvent.LOST, epoch=0x11), None),
        ("request_routed", F.RequestFrame(G, NODE, 0x07, action=3, value=1, epoch=0x64), (0x07, 0x08)),
        ("reply_routed", F.ReplyFrame(NODE, G, 0x07, result=0, epoch=0x64), (0x08, 0x07)),
        ("keepalive", F.KeepaliveFrame(G, NODE, 0x08, lease=15, epoch=0x64, radio=F.radio_byte(1, 8)), (0x07, 0x08)),
        ("probe_flood", F.ProbeFrame(0x07, NODE, 0x0B, origin_id=G, window=20, hops=(hop,), epoch=0x64), None),
        ("probe_routed", F.ProbeFrame(G, NODE, 0x0C, origin_id=G, window=1, epoch=0x64), (0x07, 0x08)),
        ("probe_reply", F.ProbeReplyFrame(NODE, G, 0x0B, links=(link, weak, link), epoch=0x64), (0x08, 0x07)),
        ("route_error", F.RouteErrorFrame(0x07, G, 0x07, broken=0x08, epoch=0x64), ()),
        ("trace", F.TraceFrame(G, NODE, 0x0D, query_sequence=0x07, query_epoch=0x64, epoch=0x64), (0x07, 0x08)),
        ("trace_reply", F.TraceReplyFrame(NODE, G, 0x0D, records=(record,), epoch=0x64), (0x08, 0x07)),
        ("join", F.JoinFrame(NODE, BROADCAST, 0x01, origin_id=NODE, window=20, epoch=0x01), None),
        ("discover", F.DiscoverFrame(G, BROADCAST, 0x0E, origin_id=G, window=20, epoch=0x64, spread=160), None),
        (
            "discover_reply",
            F.DiscoverReplyFrame(
                NODE, G, 0x0E, state=F.NodeState.READY, links=(link, weak, link),
                neighbours=(F.ProbeHop(0x09, weak),), epoch=0x64,
            ),
            (0x08, 0x07),
        ),
        ("wake_net", F.WakeNetFrame(G, 0x0F, lease=20, epoch=0x64), None),
        ("sleep_net", F.SleepNetFrame(G, 0x10, epoch=0x64), None),
        # v2 (2026-10-21): a slow action answers ACCEPTED, its RESULT
        # follows (flooded or back along the request's route), acknowledged
        # by the controller.
        ("reply_accepted", F.ReplyFrame(NODE, G, 0x05, result=F.ReplyResult.ACCEPTED, epoch=0x64), None),
        ("result", F.ResultFrame(NODE, G, 0x05, result=0, epoch=0x64), None),
        ("result_routed", F.ResultFrame(NODE, G, 0x07, result=0, epoch=0x64), (0x08, 0x07)),
        ("result_ack", F.ReplyFrame(G, NODE, 0x05, result=0, epoch=0x64), None),
        # v4 (2026-10-22): a module's event, flooded; its acknowledgement
        # echoes the node's own message id and stays network-only.
        ("event", F.EventFrame(NODE, G, 0x03, event_id=0x07, epoch=0x11), None),
        ("event_ack", F.ReplyFrame(G, NODE, 0x03, result=0, epoch=0x11), None),
        # v5 (2026-11-08): the device reset with the action open. A retry gets
        # INTERRUPTED as its REPLY; a RESULT not yet acknowledged is sent again
        # with it. Neither says the action did not run.
        ("reply_interrupted", F.ReplyFrame(NODE, G, 0x05, result=F.ReplyResult.INTERRUPTED, epoch=0x64), None),
        ("result_interrupted", F.ResultFrame(NODE, G, 0x05, result=F.ReplyResult.INTERRUPTED, epoch=0x64), None),
    ]


def build() -> dict:
    aead = AESCCM(NETWORK_KEY, tag_length=MIC_LEN)
    controller_keys = AppKeys({NODE: APP_KEY})
    # [MEM] one stack per sender | 端: 仅生成向量 | 上限: 4 | 每项: 一个安全层
    stacks: dict[int, tuple[SecureTransport, _Wire]] = {}

    def stack(sender: int) -> tuple[SecureTransport, _Wire]:
        if sender not in stacks:
            wire = _Wire()
            app = controller_keys if sender == G else AppKeys({NODE: APP_KEY}) if sender == NODE else None
            stacks[sender] = (SecureTransport(wire, NETWORK_KEY, initial_fcnt=FIRST_FCNT - 1, app=app), wire)
        return stacks[sender]

    sent_by_controller: list[bytes] = []
    sent_by_node: list[bytes] = []
    entries = []
    for name, frame, route in _examples():
        plain = frame.encode()
        if route is not None:
            plain = F.add_route(plain, route)
        transport, wire = stack(plain[1])
        if name in ("reply_to_request", "reply_routed", "reply_accepted", "reply_interrupted"):
            # Node 0x01 answers REQUESTs it opened: that decides its REPLY carries the layer.
            for raw in sent_by_controller:
                transport._on_frame(raw)
        if name == "result_ack":
            # The controller acknowledges a RESULT it opened: so does this REPLY carry the layer.
            for raw in sent_by_node:
                transport._on_frame(raw)
        transport.send(plain)
        sealed = wire.sent[-1]
        if plain[1] == G:
            sent_by_controller.append(sealed)
        elif plain[1] == NODE:
            sent_by_node.append(sealed)
        below, fcnt = open_sealed(aead, sealed)
        entries.append(
            {
                "name": name,
                "fields": _fields(frame),
                "route": None if route is None else list(route),
                "plain": plain.hex(),
                "below": below.hex(),
                "wire": sealed.hex(),
                "fcnt": fcnt,
                "tx": sealed[8],
                "app_layer": bool(below[0] & F.APP),
            }
        )
    by_name = {entry["name"]: entry for entry in entries}
    passed_on = reseal(aead, bytes.fromhex(by_name["request_routed"]["wire"]), 0x07)
    below, fcnt = open_sealed(aead, bytes.fromhex(by_name["request"]["wire"]))
    replayed = seal(aead, below, fcnt + 1000)
    window = ReplayWindow()
    steps = []
    for counter in (100, 101, 99, 101, 70, 133, 101, 102, 140):
        fresh = window.check(counter)
        if fresh:
            window.accept(counter)
        steps.append({"fcnt": counter, "accepted": fresh})
    return {
        "version": VERSION,
        "network_key": NETWORK_KEY.hex(),
        "app_keys": {f"{NODE:#04x}": {"0": APP_KEY.hex()}},
        "controller": G,
        "notes": (
            "plain: above the security layer; below: what a relay sees after opening the network envelope; "
            f"wire: on air. Each sender's FCNT counts from {FIRST_FCNT}."
        ),
        "frames": entries,
        "relay_passes_on": {
            "from": "request_routed",
            "tx": 0x07,
            "wire": passed_on.hex(),
            "note": "same SENDER, FCNT and plain bytes; only TX (and with it the nonce and MIC) changes",
        },
        "replay_window": {"size": 32, "steps": steps},
        "app_replay": {
            "from": "request",
            "fcnt": fcnt + 1000,
            "wire": replayed.hex(),
            "expect": "refused (app_failed): the inner MIC is bound to the original FCNT",
        },
    }


def main(argv: list[str]) -> None:
    json.dump(build(), sys.stdout, indent=1, ensure_ascii=False)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main(sys.argv[1:])
