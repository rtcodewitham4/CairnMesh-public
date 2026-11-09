"""Behaviour vectors of the layers above the relay (``protocol_spec.md`` section 16.3).

    python -m cairnmesh.node_conformance > tests/vectors/lora_node_behaviour.json

``relay_conformance.py`` fixes what one node's ``MeshRelay`` does; this
fixes what the rest of a multi-hop node does on top of it, assembled as
``node.NodeStack`` (section 17): the HELLO beacons, the neighbour table and
its LOST / RECOVERED reports (``NeighbourWatch``, section 8), JOIN and scan
answers (``DiscoveryAgent``, 12.3), and the dormant / awake state
(``WakeAgent``, 14.2). Each case scripts sealed frames heard on air (with
their RSSI) and module events raised, and the expected trace of what the
node puts on air (with the preamble length) and when it switches its radio
to sampling and back (``dormant``). The rules of ``relay_conformance.py``
hold (time event by event, midpoint draws, SNR = RSSI − noise floor, no
channel activity detection, SF7 airtime), and:

- every case starts with ``NodeStack.boot`` at 0 s: the firmware's start is
  a boot (in a sleeping network, the wake JOIN of Q22);
- every draw of a whole number ``randrange(n)`` is ``(n − 1) // 2``, as the
  firmware's ``Uniform(0, n − 1)`` is; the node's EPOCH is its id;
- the node's frames take FCNTs from ``FCNT`` + 1 on (one counter, spec 17).

Behaviour is not frozen like the wire format (section 16): when it changes
on purpose, regenerate the file and note it in spec 16.3;
``tests/conformance/test_lora_cc_node_conformance.py`` keeps the file in step.
"""

from __future__ import annotations

import json
import sys

from . import frames as F
from .node import NodeStack, radio_stack
from .relay_conformance import GATEWAY, INNER, NETWORK_KEY, NODE, OUTER, _at, describe_sealed, heard

#: The FCNT the node's counter stands at when it boots (the first frame takes the next).
FCNT = 100
#: A fourth node, heard weakly.
FAR = 0x0B
#: The wake preamble of a sleeping network (spec 18: T = 0.5 s at SF7).
SLEEPING = {"wake_preamble": 491}


class Middle:
    """The jitter-off random source, whole numbers included."""

    @staticmethod
    def uniform(a: float, b: float) -> float:
        return (a + b) / 2

    @staticmethod
    def random() -> float:
        return 0.5

    @staticmethod
    def randrange(n: int) -> int:
        return (n - 1) // 2


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

    def set_dormant(self, dormant: bool) -> None:
        self._log("dormant", dormant)


def _options(config: dict) -> dict:
    return {k: tuple(v) if isinstance(v, list) else v for k, v in config.items()}


def build(case: dict, clock):
    """The node of ``case``: which layers it has and their options come from its ``config``."""

    trace: list[dict] = []
    config = case.get("config", {})

    def log(what: str, *args) -> None:
        now = _at(clock())
        if what == "tx":
            raw, preamble = args
            entry = {"at": now, "tx": raw.hex(), "frame": describe_sealed(raw)}
            if preamble != 8:
                entry["preamble"] = preamble
        elif what == "radio":
            entry = {"at": now, "radio": {"channel": args[0], "sf": args[1]}}
        else:
            entry = {"at": now, "dormant": args[0]}
        trace.append(entry)

    radio = _Radio(log)
    relay, secure = radio_stack(
        radio, NETWORK_KEY, NODE, forward=True, clock=clock, rng=Middle(), initial_fcnt=FCNT, **_options(config.get("relay", {}))
    )
    stack = NodeStack(
        relay,
        secure,
        radio,
        NODE,
        controller_id=GATEWAY,
        clock=clock,
        rngs={layer: Middle() for layer in ("device", "watch", "events", "agent")},
        hello_interval=config.get("hello_interval"),
        watch_options=_options(config.get("watch", {})),
        wake_options=_options(config["wake"]) if "wake" in config else None,
        discovery_options=_options(config["discovery"]) if "discovery" in config else None,
    )
    return stack, trace


def replay(case: dict) -> list[dict]:
    """Run one case; the trace of what the node sent and how it switched its radio, in order."""

    now = 0.0
    stack, trace = build(case, lambda: now)
    radio = stack.relay.radio
    stack.boot()

    def advance(until: float, inclusive: bool) -> None:
        nonlocal now
        for _ in range(10_000):
            due = max(now, stack.next_due())
            if due > until or (due == until and not inclusive):
                return
            now = due
            before = len(trace), stack.next_due()
            stack.tick(now)
            if stack.next_due() <= now and (len(trace), stack.next_due()) == before:
                raise RuntimeError(f"{case['name']}: next_due {stack.next_due()} at {now} but tick did nothing")
        raise RuntimeError(f"{case['name']}: timers never settle")

    for step in case["steps"]:
        advance(step["at"], inclusive=False)
        now = step["at"]
        if "rx" in step:
            radio.receive(bytes.fromhex(step["rx"]), step.get("rssi"))
        elif "notify" in step:
            stack.notify(step["notify"])
        stack.tick(now)
    advance(case["end"], inclusive=True)
    return trace


# ------------------------------------------------------------------ cases


def hello(sender: int, sequence: int = 0) -> F.HelloFrame:
    return F.HelloFrame(sender, sequence, F.NodeState.READY)


def hellos(sender: int, start: float, until: float, fcnt: int, rssi: float = -90.0, every: float = 60.0) -> list[dict]:
    """``sender``'s HELLOs heard every ``every`` seconds from ``start`` up to ``until``."""

    out, at, n = [], start, 0
    while at <= until:
        out.append(heard(at, hello(sender, n), fcnt + n, sender, rssi))
        at, n = at + every, n + 1
    return out


def ack(sequence: int) -> F.ReplyFrame:
    """The controller's REPLY to this node's REPORT or EVENT ``sequence`` (EPOCH = the node's id)."""

    return F.ReplyFrame(GATEWAY, NODE, sequence, F.ReplyResult.OK, epoch=NODE)


def notify(at: float, event: int) -> dict:
    return {"at": at, "notify": event}


#: The node's first REPORT / EVENT sequence (``randrange(256)`` + 1).
SEQ = 128
WATCH = {"hello_interval": 60.0}


def cases() -> list[dict]:
    return [
        # ------------------------------------------------ HELLO and the neighbour table (spec 8)
        {
            "name": "hello_at_half_an_interval_then_every_interval",
            "spec": "8",
            "description": "The first HELLO at a random point of the first interval (midpoint 30 s), then every hello_interval ±15 % (midpoint 60 s); TARGET 0xFF, never repeated.",
            "config": WATCH,
            "steps": [],
            "end": 160.0,
        },
        {
            "name": "silent_neighbour_reported_lost_and_acknowledged",
            "spec": "8",
            "description": "A neighbour silent for more than miss_limit (5) intervals is reported LOST after report_delay; the controller's REPLY ends it.",
            "config": WATCH,
            "steps": hellos(INNER, 10.0, 70.0, 200) + [heard(390.0, ack(SEQ), 900, GATEWAY)],
            "end": 420.0,
        },
        {
            "name": "unacknowledged_report_repeated_then_abandoned",
            "spec": "8",
            "description": "A REPORT nobody acknowledges goes again every report_timeout (15 s), report_retries (3) more times, then is dropped.",
            "config": WATCH,
            "steps": hellos(INNER, 10.0, 10.0, 200),
            "end": 420.0,
        },
        {
            "name": "report_suppressed_by_another_nodes_report",
            "spec": "8",
            "description": "Another node's REPORT about the same subject and event heard during report_delay cancels ours.",
            "config": WATCH,
            "steps": hellos(INNER, 10.0, 10.0, 200)
            + [heard(312.0, F.ReportFrame(OUTER, GATEWAY, 40, INNER, F.ReportEvent.LOST, epoch=OUTER), 300, OUTER)],
            "end": 330.0,
        },
        {
            "name": "neighbour_heard_again_reported_recovered",
            "spec": "8",
            "description": "A neighbour reported LOST that is heard again is reported RECOVERED (a new sequence).",
            "config": WATCH,
            "steps": hellos(INNER, 10.0, 10.0, 200)
            + [heard(320.0, ack(SEQ), 900, GATEWAY), heard(400.0, hello(INNER, 9), 209, INNER), heard(410.0, ack(SEQ + 1), 901, GATEWAY)],
            "end": 420.0,
        },
        {
            "name": "flap_before_the_report_cancels_it",
            "spec": "8",
            "description": "Heard again before its LOST report went out: LOST and RECOVERED cancel, nothing is reported.",
            "config": WATCH,
            "steps": hellos(INNER, 10.0, 10.0, 200) + [heard(312.0, hello(INNER, 5), 205, INNER)],
            "end": 330.0,
        },
        {
            "name": "weak_neighbour_silent_not_reported",
            "spec": "8",
            "description": "A neighbour whose smoothed RSSI is below watch_rssi (−118.5 dBm), heard for less than weak_window intervals, is never reported.",
            "config": WATCH,
            "steps": hellos(FAR, 10.0, 10.0, 200, rssi=-121.0),
            "end": 420.0,
        },
        {
            "name": "weak_neighbour_watched_by_its_hello_rate",
            "spec": "8",
            "description": "A weak neighbour known for weak_window (10) intervals or more is watched on a longer fuse: LOST once silent for the least n with (1 − rate)ⁿ ≤ 1e-4, the rate (HELLOs after the first over the intervals since, the silence included) falling while it stays silent; heard every other interval for 40 min, n settles at 26.",
            "config": WATCH,
            "steps": hellos(FAR, 10.0, 2410.0, 200, rssi=-121.0, every=120.0),
            "end": 4000.0,
        },
        {
            "name": "full_table_forgets_the_least_recently_heard",
            "spec": "8",
            "description": "With the table full (max_neighbours, 2 here), a new neighbour replaces the one heard least recently (not the one first heard), which is then never reported.",
            "config": {**WATCH, "watch": {"max_neighbours": 2}},
            "steps": [
                heard(10.0, hello(INNER), 200, INNER),
                heard(11.0, hello(OUTER), 300, OUTER),
                heard(12.0, hello(INNER, 1), 201, INNER),
                heard(13.0, hello(FAR), 400, FAR),
            ],
            "end": 330.0,
        },
        {
            "name": "smoothed_rssi_decides_who_is_watched",
            "spec": "8",
            "description": "The watch threshold applies to the smoothed RSSI (EWMA, new HELLO weight 0.25): −115 then −125 dBm smooth to −117.5, above watch_rssi, so its silence is reported.",
            "config": WATCH,
            "steps": [heard(10.0, hello(INNER), 200, INNER, -115.0), heard(70.0, hello(INNER, 1), 201, INNER, -125.0)],
            "end": 390.0,
        },
        {
            "name": "weak_neighbour_rate_ignores_the_time_asleep",
            "spec": "8, 14.2",
            "description": "Woken, a weak neighbour's start moves on with its last HELLO (resume): its rate counts awake intervals only, so it is watched and reported LOST after 9 intervals; counted over the 29 min asleep, its rate would be too low to watch (n > max_miss).",
            "config": {**WATCH, "relay": SLEEPING, "wake": {"net_awake_quota": 100000.0}},
            "steps": [heard(10.0, F.WakeNetFrame(GATEWAY, 40, 255, epoch=9), 500, GATEWAY, -100.0)]
            + hellos(FAR, 20.0, 260.0, 400, rssi=-122.0)
            + [heard(2000.0, F.WakeNetFrame(GATEWAY, 41, 255, epoch=9), 501, GATEWAY, -100.0)]
            + sorted(
                hellos(FAR, 2010.0, 2610.0, 405, rssi=-122.0)
                + [heard(2240.0 + 240.0 * i, F.WakeNetFrame(GATEWAY, 42 + i, 255, epoch=9), 502 + i, GATEWAY, -100.0) for i in range(4)],
                key=lambda step: step["at"],
            ),
            "end": 3200.0,
        },
        # ------------------------------------------------ JOIN and scan answers (spec 12.3)
        {
            "name": "join_after_half_the_spread_and_acknowledged",
            "spec": "12.3",
            "description": "JOIN at a random point of join_spread (midpoint 15 s), WIN join_window; the gateway's routed REPLY echoing its id ends it.",
            "config": {"discovery": {}},
            "steps": [heard(16.0, F.ReplyFrame(GATEWAY, NODE, SEQ, F.ReplyResult.OK, epoch=NODE), 900, GATEWAY, route=())],
            "end": 200.0,
        },
        {
            "name": "unanswered_join_backs_off_with_fresh_ids",
            "spec": "12.3",
            "description": "Unanswered, JOIN again with a new message id after join_backoff, doubling from 30 s up to 300 s.",
            "config": {"discovery": {}},
            "steps": [],
            "end": 700.0,
        },
        {
            "name": "another_join_holds_ours",
            "spec": "12.3",
            "description": "join_quiet: another node's JOIN heard pushes ours back by 3 s plus as much again at random (midpoint 4.5 s).",
            "config": {"discovery": {}},
            "steps": [heard(14.0, F.JoinFrame(OUTER, GATEWAY, 50, OUTER, 10, epoch=OUTER), 300, OUTER)],
            "end": 30.0,
        },
        {
            "name": "scan_answered_along_its_path_with_neighbours",
            "spec": "12.3",
            "description": "A DISCOVER is answered once, after its window and at a random point of its SPREAD, back along its path; neighbours are the scan copies' senders and the HELLO table's strongest, not the parent; the answer stands in for the JOIN.",
            "config": {**WATCH, "discovery": {}},
            "steps": [
                heard(5.0, hello(FAR), 400, FAR, -100.0),
                heard(10.0, F.DiscoverFrame(INNER, F.BROADCAST_ID, 60, GATEWAY, 10, (F.ProbeHop(INNER, F.LinkQuality(-80, 100)),), epoch=9, spread=20), 210, INNER, -85.0),
                heard(10.2, F.DiscoverFrame(OUTER, F.BROADCAST_ID, 60, GATEWAY, 10, (F.ProbeHop(INNER, F.LinkQuality(-80, 100)), F.ProbeHop(OUTER, F.LinkQuality(-90, 60))), epoch=9, spread=20), 310, OUTER, -95.0),
            ],
            "end": 80.0,
        },
        # ------------------------------------------------ dormant and awake (spec 14.2)
        {
            "name": "boot_in_a_sleeping_network_sends_a_wake_join",
            "spec": "14.2, 12.3",
            "description": "Q22: a node booting dormant stays awake for join_spread + window + 15 s, its JOIN in that time is a wake JOIN (PROBE_WAKE, long preamble); then it samples.",
            "config": {"relay": SLEEPING, "wake": {}, "discovery": {}},
            "steps": [],
            "end": 120.0,
        },
        {
            "name": "wake_net_wakes_for_its_lease_then_samples",
            "spec": "14.2",
            "description": "A WAKE_NET wakes a dormant node until its LEASE (paid from the awake budget); it is relayed with the long preamble; the node samples again once the lease is over and the relay has nothing left.",
            "config": {**WATCH, "relay": SLEEPING, "wake": {}},
            "steps": [heard(100.0, F.WakeNetFrame(GATEWAY, 40, 60, epoch=9), 500, GATEWAY, -100.0)],
            "end": 200.0,
        },
        {
            "name": "sleep_net_ends_the_lease",
            "spec": "14.2",
            "description": "SLEEP_NET ends the lease at once; the node passes it on first.",
            "config": {"relay": SLEEPING, "wake": {}},
            "steps": [
                heard(100.0, F.WakeNetFrame(GATEWAY, 40, 60, epoch=9), 500, GATEWAY, -100.0),
                heard(110.0, F.SleepNetFrame(GATEWAY, 41, epoch=9), 501, GATEWAY, -100.0),
            ],
            "end": 200.0,
        },
        {
            "name": "fourth_wake_within_the_period_refused",
            "spec": "14.2",
            "description": "At most max_wakes (3) WAKE_NET per wake_period (60 s) are honoured; the fourth neither wakes nor is relayed.",
            "config": {"relay": SLEEPING, "wake": {}},
            "steps": [
                heard(100.0 + 3 * i, F.WakeNetFrame(GATEWAY, 40 + i, 1, epoch=9), 500 + i, GATEWAY, -100.0)
                for i in range(4)
            ],
            "end": 140.0,
        },
        {
            "name": "awake_budget_cuts_the_lease",
            "spec": "14.2",
            "description": "WAKE_NET leases draw on net_awake_quota (20 s here): the lease is cut to what is left, and with nothing left the next is refused.",
            "config": {"relay": SLEEPING, "wake": {"net_awake_quota": 20.0}},
            "steps": [
                heard(100.0, F.WakeNetFrame(GATEWAY, 40, 60, epoch=9), 500, GATEWAY, -100.0),
                heard(130.0, F.WakeNetFrame(GATEWAY, 41, 60, epoch=9), 501, GATEWAY, -100.0),
            ],
            "end": 200.0,
        },
        {
            "name": "keepalive_through_the_node_holds_it_awake",
            "spec": "14.2",
            "description": "A routed KEEPALIVE through the node holds it awake for the keepalive's LEASE + hold (5 s) past the network lease.",
            "config": {"relay": SLEEPING, "wake": {}},
            "steps": [
                heard(100.0, F.WakeNetFrame(GATEWAY, 40, 10, epoch=9), 500, GATEWAY, -100.0),
                heard(105.0, F.KeepaliveFrame(GATEWAY, OUTER, 30, 20, epoch=9), 501, GATEWAY, -90.0, route=(NODE,)),
                heard(105.5, F.ReplyFrame(OUTER, GATEWAY, 30, F.ReplyResult.OK, epoch=9), 600, OUTER, -90.0, route=(NODE,)),
            ],
            "end": 200.0,
        },
        {
            "name": "wake_probe_wakes_for_its_window",
            "spec": "14.2",
            "description": "A wake probe (PROBE_WAKE) wakes the node for its window + probe_hold (3 s); it is passed on with the long preamble.",
            "config": {"relay": SLEEPING, "wake": {}},
            "steps": [heard(100.0, F.ProbeFrame(GATEWAY, OUTER, 20, GATEWAY, 10 | F.PROBE_WAKE, epoch=9), 500, GATEWAY, -90.0)],
            "end": 140.0,
        },
        {
            "name": "woken_neighbour_table_resumes",
            "spec": "14.2, 8",
            "description": "Silence while dormant is not silence: woken, every neighbour counts as heard at the wake-up, and the first HELLO goes within one interval.",
            "config": {**WATCH, "relay": SLEEPING, "wake": {}},
            "steps": [
                heard(50.0, F.WakeNetFrame(GATEWAY, 40, 30, epoch=9), 500, GATEWAY, -100.0),
                heard(60.0, hello(INNER), 200, INNER),
                heard(700.0, F.WakeNetFrame(GATEWAY, 41, 120, epoch=9), 501, GATEWAY, -100.0),
            ],
            "end": 830.0,
        },
        {
            "name": "event_raised_dormant_goes_out_once_woken",
            "spec": "14.2, 19",
            "description": "A module event raised while dormant waits; woken, it goes out within wake_spread (midpoint 15.5 s) and its REPLY ends it; it shares the REPORTs' sequence counter.",
            "config": {**WATCH, "relay": SLEEPING, "wake": {}},
            "steps": [
                notify(40.0, 7),
                heard(100.0, F.WakeNetFrame(GATEWAY, 40, 60, epoch=9), 500, GATEWAY, -100.0),
                heard(120.0, ack(SEQ), 501, GATEWAY, -100.0),
            ],
            "end": 200.0,
        },
    ]


def generate() -> dict:
    out = []
    for case in cases():
        case = json.loads(json.dumps(case))  # what the file will hold
        case["expect"] = replay(case)
        out.append(case)
    return {
        "description": __doc__.split("\n\n")[0],
        "node": NODE,
        "controller": GATEWAY,
        "first_fcnt": FCNT,
        "network_key": NETWORK_KEY.hex(),
        "cases": out,
    }


def main() -> None:
    json.dump(generate(), sys.stdout, indent=1, ensure_ascii=False)
    sys.stdout.write("\n")


def _show() -> None:
    """Print each case's trace briefly (developing the cases)."""

    for case in generate()["cases"]:
        print(case["name"])
        for e in case["expect"]:
            f = e.get("frame")
            if f:
                extra = {k: v for k, v in f.items() if k != "tx_node"}
                print(f"  {e['at']:9.4f} tx p={e.get('preamble', 8)} {extra}")
            else:
                print(f"  {e['at']:9.4f} {({k: v for k, v in e.items() if k != 'at'})}")


if __name__ == "__main__":
    if sys.argv[1:] == ["--show"]:
        _show()
    else:
        main()
