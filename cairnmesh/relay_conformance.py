"""Behaviour vectors of the relay (``protocol_spec.md`` section 16.2).

    python -m cairnmesh.relay_conformance > tests/vectors/lora_relay_behaviour.json

``conformance.py`` fixes what a device endpoint does; this fixes what one
node's ``MeshRelay`` does with what it hears on air: each case scripts
sealed frames arriving at the relay (with their RSSI), frames the layer
above hands it, and the expected trace of everything it puts on air (with
the preamble length), hands up, and reports. Frames on air are sealed under
the network key ``vectors.NETWORK_KEY``, so the firmware's relay can be fed
the same bytes and must send the same bytes. Rules, as in ``conformance.py``:

- time advances event by event: at each instant the timers due strictly
  before it fire first, then the scripted input, then whatever is due at it;
- every random draw takes its middle: ``uniform(a, b)`` is ``(a + b) / 2``,
  ``random()`` is 0.5 (the firmware needs a jitter-off switch for this);
- SNR is RSSI minus the noise floor (``noise_floor``, -117 dBm; the firmware
  reads SnrPkt, the bench feeds RSSI - noise floor as SNR);
- the radio has no channel activity detection (no ``busy_until``): listen
  before talk is off, every send goes out at once;
- airtime is SF7, 125 kHz, CR 4/5 (``airtime.airtime_for(7)``, spec 11.4).

The relay's own frames (re-sealed probes, route errors) take FCNTs from
``FCNT`` on, as does the layer above, through one counter (spec 17).

Behaviour is not frozen like the wire format (section 16): when it changes
on purpose, regenerate the file and note it in spec 16.2;
``tests/conformance/test_lora_cc_relay_conformance.py`` keeps the file in step with ``mesh.py``.
"""

from __future__ import annotations

import json
import sys

from cryptography.hazmat.primitives.ciphers.aead import AESCCM

from . import frames as F
from .mesh import MeshRelay
from .secure import MIC_LEN, open_sealed, seal, transmitter
from .vectors import NETWORK_KEY, _fields

GATEWAY = F.DEFAULT_CONTROLLER_ID
#: The relay under test, and its neighbours: ``INNER`` towards the gateway, ``OUTER`` further out.
NODE = 0x05
INNER = 0x03
OUTER = 0x07
TARGET = 0x09
EPOCH = 9
#: The relay's own first FCNT (frames the layer above seals and frames the relay originates).
FCNT = 100

_AEAD = AESCCM(NETWORK_KEY, tag_length=MIC_LEN)


class Middle:
    """The jitter-off random source."""

    @staticmethod
    def uniform(a: float, b: float) -> float:
        return (a + b) / 2

    @staticmethod
    def random() -> float:
        return 0.5


class _Radio:
    def __init__(self, log) -> None:
        self._log = log
        self.receive = None

    def set_receiver(self, callback) -> None:
        self.receive = callback

    def send(self, data: bytes, preamble: int = 8) -> None:
        self._log("tx", bytes(data), preamble)

    def set_radio(self, channel: int, sf: int) -> None:
        self._log("radio", channel, sf)


def describe_plain(plain: bytes) -> dict:
    """A plain frame's fields (with its route, if any), its kind by name first."""

    kind = plain[0] & ~(F.ROUTED | F.APP) & 0xFF
    out: dict = {"kind": F.FrameKind(kind).name}
    if plain[0] & F.APP:
        out["app"] = True  # the relay reads only the clear head
        out.update(sender_id=plain[1], target_id=plain[2], sequence=plain[3], epoch=plain[4])
        return out
    if plain[0] & F.ROUTED:
        frame, route = F.decode_routed(plain)
        out.update(_fields(frame))
        out["route"] = list(route)
    else:
        out.update(_fields(F.decode_frame(plain)))
    return out


def describe_sealed(raw: bytes) -> dict:
    plain, fcnt = open_sealed(_AEAD, raw)
    return {"fcnt": fcnt, "tx_node": transmitter(raw), **describe_plain(plain)}


def _at(value: float) -> float:
    return round(value, 6)


def _options(config: dict) -> dict:
    # JSON has no tuples: the window options go back to the relay as tuples.
    return {k: tuple(v) if isinstance(v, list) else v for k, v in config.items()}


def replay(case: dict) -> list[dict]:
    """Run one case; the trace of what the relay sent, handed up and reported, in order."""

    now = 0.0
    trace: list[dict] = []
    counter = [FCNT]

    def log(what: str, *args) -> None:
        if what == "tx":
            raw, preamble = args
            entry = {"at": _at(now), "tx": raw.hex(), "frame": describe_sealed(raw)}
            if preamble != 8:
                entry["preamble"] = preamble
        else:
            entry = {"at": _at(now), "radio": {"channel": args[0], "sf": args[1]}}
        trace.append(entry)

    def sealer(plain: bytes) -> bytes:
        raw = seal(_AEAD, plain, counter[0])
        counter[0] += 1
        return raw

    def up(raw: bytes) -> None:
        trace.append({"at": _at(now), "up": open_sealed(_AEAD, raw)[0].hex(), "frame": describe_sealed(raw)})

    def failed(plain: bytes, hop: int) -> None:
        trace.append({"at": _at(now), "route_failed": {"frame": describe_plain(plain), "next_hop": hop}})

    radio = _Radio(log)
    relay = MeshRelay(radio, NETWORK_KEY, NODE, rng=Middle(), clock=lambda: now, **_options(case.get("config", {})))
    relay.sealer = sealer
    relay.route_failed = failed
    relay.set_receiver(up)
    if case.get("asleep"):
        relay.asleep = True

    def advance(until: float, inclusive: bool) -> None:
        nonlocal now
        for _ in range(10_000):
            due = max(now, relay.next_due())  # -inf: "tick now"
            if due > until or (due == until and not inclusive):
                return
            now = due
            before = len(trace), relay.next_due()
            relay.tick(now)
            if relay.next_due() <= now and (len(trace), relay.next_due()) == before:
                # next_due must never name a time at which tick does nothing:
                # firmware sleeping until next_due would spin.
                raise RuntimeError(f"{case['name']}: next_due {relay.next_due()} at {now} but tick did nothing")
        raise RuntimeError(f"{case['name']}: timers never settle")

    for step in case["steps"]:
        advance(step["at"], inclusive=False)
        now = step["at"]
        if "rx" in step:
            radio.receive(bytes.fromhex(step["rx"]), step.get("rssi"))
        elif "send" in step:
            relay.send(sealer(bytes.fromhex(step["send"])))
        relay.tick(now)
    advance(case["end"], inclusive=True)
    return trace


# ------------------------------------------------------------------ cases


def heard(at: float, frame, fcnt: int, tx: int, rssi: float = -90.0, *, route=None) -> dict:
    """A copy of ``frame`` (its SENDER's FCNT ``fcnt``) on air from ``tx``."""

    plain = frame.encode() if route is None else F.add_route(frame.encode(), route)
    raw = seal(_AEAD, plain, fcnt, tx)
    return {"at": at, "rx": raw.hex(), "rssi": rssi, "frame": describe_sealed(raw)}


def hand_down(at: float, frame, *, route=None) -> dict:
    """The layer above hands the relay a frame to send (sealed with the node's next FCNT)."""

    plain = frame.encode() if route is None else F.add_route(frame.encode(), route)
    return {"at": at, "send": plain.hex(), "frame": describe_plain(plain)}


def request(sequence: int = 10, target: int = TARGET) -> F.RequestFrame:
    return F.RequestFrame(GATEWAY, target, sequence, 1, 0, epoch=EPOCH)


def reply(sequence: int = 10, sender: int = TARGET) -> F.ReplyFrame:
    return F.ReplyFrame(sender, GATEWAY, sequence, F.ReplyResult.OK, epoch=EPOCH)


def probe(target: int = TARGET, hops=(), sender: int = GATEWAY, window: int = 10, kind=F.ProbeFrame) -> F.ProbeFrame:
    return kind(sender, target, 20, GATEWAY, window, tuple(hops), epoch=EPOCH)


def hop(node: int, rssi: int) -> F.ProbeHop:
    return F.ProbeHop(node, F.LinkQuality(rssi, round((rssi - NOISE) * 4)))


NOISE = -117
#: A REQUEST relayed by this node, so that its REPLY finds the backbone.
RELAYED = [heard(1.0, request(), 500, GATEWAY, -100)]
KEEPALIVE = F.KeepaliveFrame(GATEWAY, TARGET, 30, 20, epoch=EPOCH, radio=F.radio_byte(3, 9))
KEEPALIVE_REPLY = F.ReplyFrame(TARGET, GATEWAY, 30, F.ReplyResult.OK, epoch=EPOCH)
WAKE_NET = F.WakeNetFrame(GATEWAY, 40, 60, epoch=EPOCH)
LONG = {"wake_preamble": 64}
#: A TRACE about the routed request REQUEST(10) to TARGET through this node, and the target's answer.
TRACE = F.TraceFrame(GATEWAY, TARGET, 30, 10, EPOCH, epoch=EPOCH)
TRACE_ANSWER = F.TraceReplyFrame(TARGET, GATEWAY, 30, (F.TraceRecord(-88, 20, -86, 0, 1, 0, 1),), epoch=EPOCH)  # a wake preamble: 64 symbols here (``energy.preamble_for`` on a real sniffer)


def cases() -> list[dict]:
    R = request()
    out = [
        # ------------------------------------------------ flooding (spec 7)
        {
            "name": "flood_relayed_once_after_rssi_backoff",
            "spec": "7",
            "description": "A REQUEST for someone else is relayed once, TX = this node, after a backoff that grows with RSSI; a later copy is a duplicate.",
            "steps": [heard(1.0, R, 500, GATEWAY, -100), heard(1.2, R, 500, INNER, -95)],
            "end": 5.0,
        },
        {
            "name": "outward_flood_suppressed_by_three_copies",
            "spec": "7",
            "description": "suppress_after (3) other copies of an outward flood heard during the backoff cancel this node's relay.",
            "steps": [
                heard(1.0, R, 500, GATEWAY, -100),
                heard(1.1, R, 500, INNER),
                heard(1.2, R, 500, OUTER),
                heard(1.3, R, 500, 0x0B),
            ],
            "end": 5.0,
        },
        {
            "name": "reply_follows_the_backbone_and_is_confirmed",
            "spec": "7",
            "description": "A REPLY is relayed only by a node that relayed its request; a copy from the next relay confirms it.",
            "steps": RELAYED + [heard(2.0, reply(), 700, TARGET, -90), heard(2.8, reply(), 700, INNER, -90)],
            "end": 8.0,
        },
        {
            "name": "unconfirmed_reply_repeated_once",
            "spec": "7",
            "description": "confirm_replies: a relayed REPLY nobody passes on is repeated once, hop_timeout later.",
            "steps": RELAYED + [heard(2.0, reply(), 700, TARGET, -90)],
            "end": 8.0,
        },
        {
            "name": "reply_off_the_backbone_dropped",
            "spec": "7",
            "description": "guided_replies: a REPLY to a request this node never relayed is not relayed.",
            "steps": [heard(2.0, reply(), 700, TARGET, -90)],
            "end": 8.0,
        },
        {
            "name": "reply_cancels_the_waiting_request",
            "spec": "7",
            "description": "The REPLY heard while this node still waits to relay its request cancels that relay; never having relayed it, the node is not on the backbone and leaves the REPLY alone too.",
            "steps": [heard(1.0, R, 500, GATEWAY, -60), heard(1.3, reply(), 700, TARGET, -90)],
            "end": 8.0,
        },
        {
            "name": "frame_for_this_node_goes_up_once",
            "spec": "7",
            "description": "A frame addressed to this node goes up (first copy only) and is not relayed.",
            "steps": [heard(1.0, request(target=NODE), 500, GATEWAY, -100), heard(1.5, request(target=NODE), 500, INNER)],
            "end": 5.0,
        },
        {
            "name": "hello_is_one_hop",
            "spec": "7, 8",
            "description": "A HELLO goes up (broadcast) and is never relayed.",
            "steps": [heard(1.0, F.HelloFrame(OUTER, 3, F.NodeState.READY), 900, OUTER, -90)],
            "end": 5.0,
        },
        {
            "name": "replays_forgeries_and_plain_frames_dropped",
            "spec": "5, 7, 9",
            "description": "Per origin a window of the highest FCNT and the 32 below it: an unseen FCNT inside it is fresh, one 32 or more behind is dropped, as are a bad MIC and an unsealed frame; nothing is recorded for them.",
            "steps": [
                heard(1.0, R, 600, GATEWAY, -100),
                heard(1.1, request(11), 590, GATEWAY, -100),
                heard(1.2, request(12), 500, GATEWAY, -100),
                {**heard(1.3, request(13), 601, GATEWAY, -100), "rx": _flip_mic(heard(0, request(13), 601, GATEWAY)["rx"]), "invalid": "MIC"},
                {"at": 1.4, "rx": request(14).encode().hex(), "rssi": -100.0, "frame": describe_plain(request(14).encode()), "invalid": "unsealed"},
            ],
            "end": 5.0,
        },
        {
            "name": "own_frame_repeated_without_a_copy",
            "spec": "7",
            "description": "A frame this node originates is repeated (hop_retries 1) after hop_timeout unless a copy of it comes back.",
            "steps": [hand_down(1.0, F.HeartbeatFrame(NODE, GATEWAY, 3, state=F.NodeState.READY))],
            "end": 8.0,
        },
        {
            "name": "own_frame_acknowledged_by_a_relay_copy",
            "spec": "7",
            "description": "Any relay's copy of this node's own frame is its acknowledgement: no repeat.",
            "steps": [
                hand_down(1.0, F.HeartbeatFrame(NODE, GATEWAY, 3, state=F.NodeState.READY)),
                heard(1.6, F.HeartbeatFrame(NODE, GATEWAY, 3, state=F.NodeState.READY), FCNT, INNER, -95),
            ],
            "end": 8.0,
        },
        # ------------------------------------------------ routed frames (spec 11.2)
        {
            "name": "routed_forward_acknowledged_by_next_hop",
            "spec": "11.2",
            "description": "A listed relay forwards at once (jitter); the next hop's copy acknowledges it.",
            "steps": [heard(1.0, R, 500, GATEWAY, -100, route=(NODE, OUTER)), heard(1.2, R, 500, OUTER, -90, route=(NODE, OUTER))],
            "end": 5.0,
        },
        {
            "name": "routed_repeats_then_route_error",
            "spec": "11.2",
            "description": "No acknowledgement: route_retries (3) repeats, route_hop_timeout apart, then a ROUTE_ERROR (BROKEN = next hop) back along the route; the gateway's echo acknowledges it.",
            "steps": [
                heard(1.0, R, 500, GATEWAY, -100, route=(NODE, OUTER)),
                heard(2.3, F.RouteErrorFrame(NODE, GATEWAY, 10, OUTER, epoch=EPOCH), FCNT, GATEWAY, -100, route=()),
            ],
            "end": 8.0,
        },
        {
            "name": "routed_frame_not_on_route_ignored",
            "spec": "11.2",
            "description": "A routed frame that does not list this node is neither relayed nor handed up.",
            "steps": [heard(1.0, R, 500, GATEWAY, -100, route=(INNER, OUTER))],
            "end": 5.0,
        },
        {
            "name": "routed_retry_answered_with_the_furthest_copy",
            "spec": "11.2",
            "description": "Acknowledged, then the previous hop sends again (it missed our copy): answered once with TX = the furthest node heard, which nobody further on takes for a retry.",
            "steps": [
                heard(1.0, R, 500, GATEWAY, -100, route=(NODE, OUTER)),
                heard(1.2, R, 500, OUTER, -90, route=(NODE, OUTER)),
                heard(1.5, R, 500, GATEWAY, -100, route=(NODE, OUTER)),
            ],
            "end": 5.0,
        },
        {
            "name": "last_hop_into_gateway_acknowledged_by_echo",
            "spec": "11.2",
            "description": "The last relay before the gateway waits for the gateway's echo of the inbound frame (TX = gateway) like any other hop.",
            "steps": [
                heard(1.0, reply(), 700, OUTER, -90, route=(OUTER, NODE)),
                heard(1.1, reply(), 700, GATEWAY, -100, route=(OUTER, NODE)),
            ],
            "end": 5.0,
        },
        {
            "name": "skipped_hop_waits_a_slot",
            "spec": "11.2",
            "description": "Heard from before the previous hop (a skipped hop): wait one slot per relay in between; that relay's copy then acknowledges ours.",
            "steps": [
                heard(1.0, reply(), 700, TARGET, -100, route=(OUTER, NODE)),
                heard(1.01, reply(), 700, OUTER, -90, route=(OUTER, NODE)),
                heard(1.2, reply(), 700, GATEWAY, -100, route=(OUTER, NODE)),
            ],
            "end": 5.0,
        },
        {
            "name": "routed_frame_for_this_node_goes_up",
            "spec": "11.2",
            "description": "A routed request to this node goes up; the answer the layer above hands down goes back on the reversed route and is acknowledged by the next hop's copy.",
            "steps": [
                heard(1.0, request(target=NODE), 500, INNER, -95, route=(INNER,)),
                hand_down(1.1, F.ReplyFrame(NODE, GATEWAY, 10, F.ReplyResult.OK, epoch=EPOCH), route=(INNER,)),
                heard(1.2, F.ReplyFrame(NODE, GATEWAY, 10, F.ReplyResult.OK, epoch=EPOCH), FCNT, INNER, -95, route=(INNER,)),
            ],
            "end": 5.0,
        },
        {
            "name": "target_answers_every_repeat_of_the_last_relay",
            "spec": "11.2",
            "description": "The target's answer goes again only when the last relay repeats the request; each repeat heard keeps it answerable for another full repeat cycle (linger_span).",
            "steps": [
                heard(1.0, request(target=NODE), 500, INNER, -95, route=(INNER,)),
                hand_down(1.1, F.ReplyFrame(NODE, GATEWAY, 10, F.ReplyResult.OK, epoch=EPOCH), route=(INNER,)),
                heard(1.5, request(target=NODE), 500, INNER, -95, route=(INNER,)),
                heard(2.1, request(target=NODE), 500, INNER, -95, route=(INNER,)),
                heard(2.7, request(target=NODE), 500, INNER, -95, route=(INNER,)),
            ],
            "end": 8.0,
        },
        {
            "name": "target_stops_answering_after_retry_cap",
            "spec": "11.2",
            "description": "The target sends the answer once and four repeats, five sends in all; a sixth request still reaches it after the fifth send, but it does not answer: route_retries+1 caps the repeats.",
            "steps": [
                heard(1.0, request(target=NODE), 500, INNER, -95, route=(INNER,)),
                hand_down(1.1, F.ReplyFrame(NODE, GATEWAY, 10, F.ReplyResult.OK, epoch=EPOCH), route=(INNER,)),
                heard(1.5, request(target=NODE), 500, INNER, -95, route=(INNER,)),
                heard(2.1, request(target=NODE), 500, INNER, -95, route=(INNER,)),
                heard(2.7, request(target=NODE), 500, INNER, -95, route=(INNER,)),
                heard(3.3, request(target=NODE), 500, INNER, -95, route=(INNER,)),
                heard(3.9, request(target=NODE), 500, INNER, -95, route=(INNER,)),
            ],
            "end": 8.0,
        },
        # ------------------------------------------------ probes and JOIN (spec 11.3, 12.2)
        {
            "name": "probe_relayed_with_own_hop",
            "spec": "11.3",
            "description": "A PROBE is passed on once, re-sealed as this node's own frame (its FCNT) with its hop and the link appended, after a wait that is shorter the cheaper the path; no copy from further out: sent once more.",
            "steps": [heard(1.0, probe(), 500, GATEWAY, -100)],
            "end": 5.0,
        },
        {
            "name": "probe_keeps_the_best_copy",
            "spec": "11.3",
            "description": "Copies heard while waiting: the cheapest path (ETX) is the one passed on.",
            "steps": [
                heard(1.0, probe(), 500, GATEWAY, -123),
                heard(1.02, probe(hops=[hop(INNER, -95)], sender=INNER), 300, INNER, -95),
            ],
            "end": 5.0,
        },
        {
            "name": "probe_passed_on_is_not_repeated",
            "spec": "11.3",
            "description": "A copy from further out that lists this node confirms its forward: no second copy.",
            "steps": [
                heard(1.0, probe(), 500, GATEWAY, -100),
                heard(1.3, probe(hops=[hop(NODE, -100), hop(OUTER, -95)], sender=OUTER), 800, OUTER, -95),
            ],
            "end": 5.0,
        },
        {
            "name": "probe_target_answers_the_best_path",
            "spec": "11.3",
            "description": "The target collects copies for WINDOW after the first and answers the cheapest path with a routed PROBE_REPLY (back through INNER), which INNER's copy acknowledges.",
            "steps": [
                heard(1.0, probe(target=NODE), 500, GATEWAY, -123),
                heard(1.1, probe(target=NODE, hops=[hop(INNER, -95)], sender=INNER), 300, INNER, -95),
                heard(1.62, F.ProbeReplyFrame(NODE, GATEWAY, 20, (F.LinkQuality(-95, 88), F.LinkQuality(-95, 88)), epoch=EPOCH), FCNT, INNER, -95, route=(INNER,)),
            ],
            "end": 5.0,
        },
        {
            "name": "join_relayed_like_a_probe",
            "spec": "12.2",
            "description": "A JOIN floods towards the gateway like a probe: this node appends its hop and seals it as its own.",
            "steps": [heard(1.0, F.JoinFrame(0x0B, GATEWAY, 2, 0x0B, 10, (), epoch=EPOCH), 50, 0x0B, -100)],
            "end": 5.0,
        },
        # ------------------------------------------------ session radio (spec 15.2)
        {
            "name": "keepalive_moves_after_the_answer_and_reverts",
            "spec": "15.2",
            "description": "A KEEPALIVE naming a radio setting: the path moves there once it has passed that keepalive's answer on (here: the gateway's echo), and back to the common setting after radio_timeout (6 s) of silence.",
            "steps": [
                heard(1.0, KEEPALIVE, 500, GATEWAY, -100, route=(NODE, OUTER)),
                heard(1.1, KEEPALIVE, 500, OUTER, -90, route=(NODE, OUTER)),
                heard(1.5, KEEPALIVE_REPLY, 700, OUTER, -90, route=(OUTER, NODE)),
                heard(1.6, KEEPALIVE_REPLY, 700, GATEWAY, -100, route=(OUTER, NODE)),
            ],
            "end": 12.0,
        },
        # ------------------------------------------------ wake (spec 14.3)
        {
            "name": "wake_net_relayed_with_long_preamble",
            "spec": "14.3",
            "description": "WAKE_NET goes up (broadcast) and is relayed with the wake preamble.",
            "config": LONG,
            "steps": [heard(1.0, WAKE_NET, 500, GATEWAY, -100)],
            "end": 8.0,
        },
        {
            "name": "wake_net_repeated_for_a_silent_neighbour",
            "spec": "14.3",
            "description": "A neighbour known strong enough (wake_expect) and not heard since: this node sends the WAKE_NET again, wake_copies (3) in all.",
            "config": LONG,
            "steps": [
                heard(0.5, F.HelloFrame(OUTER, 3, F.NodeState.READY), 900, OUTER, -90),
                heard(1.0, WAKE_NET, 500, GATEWAY, -100),
            ],
            "end": 10.0,
        },
        {
            "name": "wake_net_not_repeated_once_the_neighbour_is_heard",
            "spec": "14.3",
            "description": "The known neighbour passes it on: no further copy.",
            "config": LONG,
            "steps": [
                heard(0.5, F.HelloFrame(OUTER, 3, F.NodeState.READY), 900, OUTER, -90),
                heard(1.0, WAKE_NET, 500, GATEWAY, -100),
                heard(2.2, WAKE_NET, 500, OUTER, -90),
            ],
            "end": 10.0,
        },
        {
            "name": "dormant_node_hears_only_wake_frames",
            "spec": "14.3",
            "description": "Dormant: a flooded REQUEST a CAD look caught is dropped unrecorded; a WAKE_NET is taken (the wake agent then wakes the node).",
            "config": LONG,
            "asleep": True,
            "steps": [heard(1.0, R, 500, GATEWAY, -100), heard(2.0, WAKE_NET, 501, GATEWAY, -100)],
            "end": 8.0,
        },
        # ------------------------------------- path diagnostics (spec 11.8)
        {
            "name": "trace_relayed_and_its_answer_carries_our_record",
            "spec": "11.8",
            "description": "A relay passes a routed request and its answer on (its route log notes both), then the TRACE asking about them; the target's TRACE_REPLY gets this relay's record appended and goes into the gateway twice, blind.",
            "steps": [
                heard(1.0, request(), 500, GATEWAY, -90, route=(NODE,)),
                heard(1.2, request(), 500, TARGET, -95, route=(NODE,)),
                heard(1.5, reply(), 700, TARGET, -85, route=(NODE,)),
                heard(1.7, reply(), 700, GATEWAY, -80, route=(NODE,)),
                heard(3.0, TRACE, 501, GATEWAY, -90, route=(NODE,)),
                heard(3.3, TRACE_ANSWER, 701, TARGET, -85, route=(NODE,)),
            ],
            "end": 8.0,
        },
        {
            "name": "trace_target_answers_with_its_record",
            "spec": "11.8",
            "description": "The TRACE's target answers along the route back with its own record; a copy from the next node, with its record added, acknowledges it.",
            "steps": [
                heard(1.0, F.TraceFrame(GATEWAY, NODE, 31, 10, EPOCH, epoch=EPOCH), 502, INNER, -90, route=(INNER,)),
                heard(1.3, F.TraceReplyFrame(NODE, GATEWAY, 31, (F.TraceRecord(), F.TraceRecord(-90, 12)), epoch=EPOCH), 600, INNER, -90, route=()),
            ],
            "end": 8.0,
        },
        # --------------------------------- probes along a route (spec 15.1, C5)
        {
            "name": "routed_probe_passed_on_by_the_next_listed_relay",
            "spec": "15.1",
            "description": "A probe along a route: only the next listed relay passes it on, with its own hop; the next relay's copy naming it confirms it.",
            "steps": [
                heard(1.0, probe(), 500, GATEWAY, -100, route=(NODE, OUTER)),
                heard(1.5, probe(hops=[hop(NODE, -100), hop(OUTER, -95)], sender=OUTER), 900, OUTER, -95, route=()),
            ],
            "end": 8.0,
        },
        {
            "name": "own_routed_probe_repeated_until_passed_on",
            "spec": "15.1",
            "description": "This node's own probe along a route: repeated (route_retries) while the first relay does not pass it on.",
            "steps": [hand_down(1.0, F.ProbeFrame(NODE, TARGET, 21, NODE, 10, (), epoch=EPOCH), route=(OUTER,))],
            "end": 12.0,
        },
    ]
    return out


def _flip_mic(raw_hex: str) -> str:
    raw = bytearray.fromhex(raw_hex)
    raw[-1] ^= 0x01
    return raw.hex()


def generate() -> dict:
    return {
        "layer": "one node's relay, sealed frames on air (protocol_spec.md 16.2)",
        "node": NODE,
        "first_fcnt": FCNT,
        "network_key": NETWORK_KEY.hex(),
        "rules": [
            "time advances event by event; at an instant: timers due before it, then the input, then timers due at it",
            "every random draw: uniform(a, b) takes (a + b) / 2, random() takes 0.5",
            "SNR = RSSI - noise_floor (-117 dBm); no channel activity detection; airtime SF7 / 125 kHz / CR 4/5",
            "the relay's own frames and the layer above's share one FCNT counter from first_fcnt",
            "expect lists every frame put on air ('tx', with 'preamble' when not 8 symbols), every frame handed up ('up'), "
            "every route failure reported ('route_failed') and radio setting change ('radio'), in order",
        ],
        "cases": [{**case, "expect": replay(case)} for case in cases()],
    }


def main() -> None:
    json.dump(generate(), sys.stdout, indent=1, ensure_ascii=False)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
