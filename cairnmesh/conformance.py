"""Behaviour vectors of the device endpoint (``protocol_spec.md`` section 16.1).

    python -m cairnmesh.conformance > tests/vectors/lora_device_behaviour.json

The wire vectors (``vectors.py``) fix the bytes; these fix what a device
does with them: each case scripts frames arriving at one ``Device`` (plain
frames, above the security layer) and reboots, and lists every frame the
device sends and every action it runs, with the time. The firmware's
endpoint is checked against the same file with the same rules:

- time advances event by event: at each instant the timers due strictly
  before it fire first, then the scripted input, then whatever is due at
  that instant (so a frame arriving exactly when a timer expires is seen
  first);
- every random draw ``uniform(a, b)`` takes the midpoint ``(a + b) / 2``
  (jitter off: the firmware needs the same switch for this test);
- the application is a table per ACTION: how long the action takes and
  what it returns (absent: instant, OK); ``pending`` makes it return
  ``PENDING`` (result known later), its time a deadline, and a ``complete``
  step reports the result (``Device.complete``).

Behaviour is not frozen like the wire format (section 16): when it changes
on purpose, regenerate the file and say so in the spec;
``tests/conformance/test_lora_cc_conformance.py`` keeps the file in step with ``device.py``.
"""

from __future__ import annotations

import json
import sys
from typing import Callable

from . import frames as F
from .device import PENDING, Device
from .vectors import _fields

CONTROLLER = F.DEFAULT_CONTROLLER_ID
OTHER = 0x43  # a second controller (sender)
THIRD = 0x44  # a third sender
NODE = 0x01
EPOCH = 9
#: Heartbeats far away: only the heartbeat case sees one.
QUIET = 1e6


class Midpoint:
    """The jitter-off random source: ``uniform(a, b)`` is ``(a + b) / 2``."""

    @staticmethod
    def uniform(a: float, b: float) -> float:
        return (a + b) / 2


class _Wire:
    def __init__(self, sent: Callable[[bytes], None]) -> None:
        self._sent = sent

    def set_receiver(self, callback) -> None:
        pass

    def send(self, data: bytes) -> None:
        self._sent(bytes(data))


class _Endpoint(Device):
    """A ``Device`` whose application is the case's action table."""

    def __init__(self, transport, actions: dict, ran: Callable[[F.RequestFrame, int], None], **options) -> None:
        self._actions = actions
        self._ran = ran
        super().__init__(transport, action_time=lambda frame: self._action(frame).get("time", 0.0), **options)

    def _action(self, frame: F.RequestFrame) -> dict:
        return self._actions.get(str(frame.action), {})

    def perform_action(self, frame: F.RequestFrame) -> int:
        action = self._action(frame)
        result = PENDING if action.get("pending") else action.get("result", int(F.ReplyResult.OK))
        self._ran(frame, result)
        return result


def _at(value: float) -> float:
    return round(value, 6)


def describe(frame) -> dict:
    """The frame's fields, its kind by name first (for reading the file)."""

    return {"kind": frame.kind.name, **_fields(frame)}


def replay(case: dict) -> list[dict]:
    """Run one case; the trace of sent frames and run actions, in order."""

    now = 0.0
    trace: list[dict] = []

    def sent(data: bytes) -> None:
        trace.append({"at": _at(now), "tx": data.hex(), "frame": describe(F.decode_frame(data))})

    def ran(frame: F.RequestFrame, result: int) -> None:
        trace.append({"at": _at(now), "run": {"action": frame.action, "value": frame.value, "result": int(result)}})

    device = _Endpoint(_Wire(sent), case.get("actions", {}), ran, rng=Midpoint(), clock=lambda: now, **case["config"])

    def advance(until: float, inclusive: bool) -> None:
        nonlocal now
        for _ in range(10_000):
            due = device.next_due()
            if due > until or (due == until and not inclusive):
                return
            now = due
            device.tick(now)
        raise RuntimeError(f"{case['name']}: timers never settle")

    for step in case["steps"]:
        advance(step["at"], inclusive=False)
        now = step["at"]
        if "rx" in step:
            device.on_frame(bytes.fromhex(step["rx"]))
        elif step.get("reboot"):
            device.reset()
        elif "complete" in step:
            done = step["complete"]
            taken = device.complete(done["sender"], done["epoch"] << 8 | done["sequence"], done["result"])
            trace.append({"at": _at(now), "complete": {**done, "taken": taken}})
        device.tick(now)
    advance(case["end"], inclusive=True)
    return trace


# ------------------------------------------------------------------ cases


def _rx(at: float, frame) -> dict:
    return {"at": at, "rx": frame.encode().hex(), "frame": describe(frame)}


def request(at: float, sequence: int, action: int = 1, value: int = 0, *, sender: int = CONTROLLER) -> dict:
    return _rx(at, F.RequestFrame(sender, NODE, sequence, action, value, epoch=EPOCH))


def query(at: float, sequence: int, asked: int) -> dict:
    return _rx(at, F.RequestFrame(CONTROLLER, NODE, sequence, F.QUERY_ACTION, asked, epoch=EPOCH))


def wake(at: float, sequence: int) -> dict:
    return _rx(at, F.WakeFrame(CONTROLLER, NODE, sequence, epoch=EPOCH))


def ack(at: float, sequence: int) -> dict:
    """The controller's REPLY acknowledging a RESULT."""

    return _rx(at, F.ReplyFrame(CONTROLLER, NODE, sequence, F.ReplyResult.OK, epoch=EPOCH))


def complete(at: float, sequence: int, result: int, *, sender: int = CONTROLLER) -> dict:
    """The application reports the result of a PENDING action."""

    return {"at": at, "complete": {"sender": sender, "sequence": sequence, "epoch": EPOCH, "result": int(result)}}


def config(**options) -> dict:
    base = {"device_id": NODE, "controller_id": CONTROLLER, "awake": True, "heartbeat_interval": QUIET}
    base.update(options)
    return base


SLOW = {"7": {"time": 5.0}}  # past the answer budget: ACCEPTED, then RESULT
SHORT = {"6": {"time": 0.4}}  # within the budget: the answer waits for it
FAILS = {"3": {"result": int(F.ReplyResult.ERROR)}}
LATER = {"8": {"time": 5.0, "pending": True}, "9": {"time": 0.5, "pending": True}, "4": {"pending": True}}
OK, ERROR = int(F.ReplyResult.OK), int(F.ReplyResult.ERROR)


def cases() -> list[dict]:
    return [
        {
            "name": "request_runs_once_and_answers",
            "spec": "4.1",
            "description": "A fast action runs at once and its result is the answer.",
            "config": config(),
            "steps": [request(1.0, 10, 1, 5)],
            "end": 5.0,
        },
        {
            "name": "retry_replays_the_original_result",
            "spec": "4.1",
            "description": "A retry is answered with the stored result (ERROR stays ERROR) and does not run again.",
            "config": config(),
            "actions": FAILS,
            "steps": [request(1.0, 10, 3), request(2.0, 10, 3)],
            "end": 5.0,
        },
        {
            "name": "window_pushes_out_the_oldest_closed_record",
            "spec": "4.1",
            "description": "dedup_window closed records per sender: the oldest is pushed out, its late copy runs again.",
            "config": config(dedup_window=2),
            "steps": [request(1.0, 10), request(2.0, 11), request(3.0, 12), request(4.0, 11), request(5.0, 10)],
            "end": 8.0,
        },
        {
            "name": "senders_are_kept_least_recently_used",
            "spec": "4.1",
            "description": "max_senders lists; a new sender pushes out the least recently used one.",
            "config": config(max_senders=1),
            "steps": [request(1.0, 10), request(2.0, 10, sender=OTHER), request(3.0, 10)],
            "end": 5.0,
        },
        {
            "name": "a_retry_keeps_its_sender",
            "spec": "4.1",
            "description": "A retry counts as hearing from its sender: a third sender pushes out the other one, the retry after it is answered from the record.",
            "config": config(max_senders=2),
            "steps": [
                request(1.0, 10), request(2.0, 10, sender=OTHER), request(3.0, 10),
                request(4.0, 10, sender=THIRD), request(5.0, 10),
            ],
            "end": 8.0,
        },
        {
            "name": "a_query_keeps_its_sender",
            "spec": "4.1",
            "description": "A query counts as hearing from its sender too: the third sender pushes out the other one.",
            "config": config(max_senders=2),
            "steps": [
                request(1.0, 10), request(2.0, 10, sender=OTHER), query(3.0, 20, 10),
                request(4.0, 10, sender=THIRD), request(5.0, 10),
            ],
            "end": 8.0,
        },
        {
            "name": "records_expire_after_the_keep_period",
            "spec": "4.1",
            "description": "Past dedup_ttl a record is forgotten: a copy arriving later runs again.",
            "config": config(dedup_ttl=60.0),
            "steps": [request(1.0, 10), request(30.0, 10), request(62.0, 10)],
            "end": 65.0,
        },
        {
            "name": "query_answers_the_stored_result",
            "spec": "4.1, 4.2",
            "description": "A status query (ACTION 0xFF, VALUE = asked SEQUENCE) gets the stored result and runs nothing.",
            "config": config(),
            "actions": FAILS,
            "steps": [request(1.0, 10, 3), query(2.0, 11, 10)],
            "end": 5.0,
        },
        {
            "name": "query_of_an_unknown_request_makes_not_run_final",
            "spec": "4.1, 4.2",
            "description": "An unknown request is answered NOT_RUN and recorded so: a late copy is refused, not run.",
            "config": config(),
            "steps": [query(1.0, 11, 10), request(2.0, 10)],
            "end": 5.0,
        },
        {
            "name": "frames_for_others_and_broadcast_requests_are_ignored",
            "spec": "4.1",
            "description": "A REQUEST to another node or to broadcast, and an undecodable frame, get no answer.",
            "config": config(),
            "steps": [
                _rx(1.0, F.RequestFrame(CONTROLLER, 0x02, 10, 1, 0, epoch=EPOCH)),
                _rx(2.0, F.RequestFrame(CONTROLLER, F.BROADCAST_ID, 11, 1, 0, epoch=EPOCH)),
                {"at": 3.0, "rx": "ff00"},
            ],
            "end": 5.0,
        },
        {
            "name": "sleeping_device_defers_frames_and_wakes",
            "spec": "4.1",
            "description": "Asleep, a frame is handled after sleep_delay (midpoint 2 s); WAKE switches to READY.",
            "config": config(awake=False),
            "steps": [request(1.0, 10), wake(1.5, 11), request(4.0, 12)],
            "end": 8.0,
        },
        {
            "name": "sleep_queue_drops_repeats_and_overflow",
            "spec": "4.1",
            "description": "A copy of a waiting frame adds nothing; past max_deferred frames are dropped.",
            "config": config(awake=False, max_deferred=2),
            "steps": [request(1.0, 10), request(1.2, 10), request(1.4, 11), request(1.6, 12)],
            "end": 6.0,
        },
        {
            "name": "heartbeats_follow_the_interval",
            "spec": "4.1",
            "description": "First heartbeat at the midpoint of (0.2, interval), then every interval; it carries the state.",
            "config": config(heartbeat_interval=3.0, awake=False),
            "steps": [wake(4.0, 10)],
            "end": 9.0,
        },
        {
            "name": "slow_action_accepts_then_reports",
            "spec": "4.1",
            "description": "Past answer_budget: ACCEPTED at once, RESULT when done, repeated until acknowledged.",
            "config": config(),
            "actions": SLOW,
            "steps": [request(1.0, 10, 7), ack(17.0, 10)],
            "end": 60.0,
        },
        {
            "name": "unacknowledged_result_backs_off_then_stops",
            "spec": "4.1",
            "description": "RESULT again after result_timeout × 2^n, result_retries more times, then left to queries.",
            "config": config(),
            "actions": SLOW,
            "steps": [request(1.0, 10, 7), query(200.0, 11, 10)],
            "end": 260.0,
        },
        {
            "name": "query_while_running_then_after",
            "spec": "4.1, 4.2",
            "description": "A query meets the running action: ACCEPTED. Once over, the query gets the result and stops the RESULT repeats.",
            "config": config(),
            "actions": SLOW,
            "steps": [request(1.0, 10, 7), query(3.0, 11, 10), query(8.0, 12, 10)],
            "end": 40.0,
        },
        {
            "name": "short_action_answer_waits_for_the_result",
            "spec": "4.1",
            "description": "Within answer_budget the answer is sent when the action is over and carries the result; no RESULT.",
            "config": config(),
            "actions": SHORT,
            "steps": [request(1.0, 10, 6)],
            "end": 20.0,
        },
        {
            "name": "retry_during_short_action_hears_accepted_then_result",
            "spec": "4.1",
            "description": "A retry while the short action runs is answered ACCEPTED, so a RESULT follows (review A6).",
            "config": config(),
            "actions": SHORT,
            "steps": [request(1.0, 10, 6), request(1.2, 10, 6), ack(2.0, 10)],
            "end": 20.0,
        },
        {
            "name": "open_records_past_max_open_are_refused",
            "spec": "4.1, 9",
            "description": "Past max_open open records a slow request is refused NOT_RUN, finally (a late copy too).",
            "config": config(max_open=1),
            "actions": SLOW,
            "steps": [request(1.0, 10, 7), request(2.0, 11, 7), request(3.0, 11, 7), ack(7.0, 10)],
            "end": 10.0,
        },
        {
            "name": "reboot_keeps_records_and_ends_a_cut_action_interrupted",
            "spec": "4.1",
            "description": "Records survive a reboot: a retry is not run. An action cut short is INTERRUPTED (no final result; it may have completed; wire v5), not ERROR; the retry gets INTERRUPTED and closes the record, so no RESULT follows (STATUS P4; one not asked about sends its RESULT after boot, next case).",
            "config": config(),
            "actions": SLOW,
            "steps": [
                request(1.0, 10, 1),
                request(2.0, 11, 7),
                {"at": 3.0, "reboot": True},
                request(4.0, 10, 1),
                request(5.0, 11, 7),
                ack(9.0, 11),
            ],
            "end": 20.0,
        },
        {
            "name": "reboot_cuts_a_short_action_and_the_retry_gets_interrupted",
            "spec": "4.1",
            "description": "A reboot while a short action (within the budget) runs: its answer was not sent; the retry gets INTERRUPTED, not ERROR and not OK, and is not run again (wire v5). A failed action's ERROR still replays as ERROR (retry_replays_the_original_result).",
            "config": config(),
            "actions": SHORT,
            "steps": [request(1.0, 10, 6), {"at": 1.2, "reboot": True}, request(2.0, 10, 6), request(3.0, 10, 6)],
            "end": 6.0,
        },
        {
            "name": "pending_long_action_reports_when_it_completes",
            "spec": "4.1",
            "description": "Result known later, past the budget: ACCEPTED at once, the RESULT as soon as the application completes it (ERROR here), not at the deadline.",
            "config": config(),
            "actions": LATER,
            "steps": [request(1.0, 10, 8), complete(2.5, 10, ERROR), ack(3.0, 10)],
            "end": 20.0,
        },
        {
            "name": "pending_short_action_answers_when_it_completes",
            "spec": "4.1",
            "description": "Result known later, deadline within the budget: the answer waits and goes as soon as it completes.",
            "config": config(),
            "actions": LATER,
            "steps": [request(1.0, 10, 9), complete(1.2, 10, OK)],
            "end": 5.0,
        },
        {
            "name": "pending_action_past_its_deadline_is_error",
            "spec": "4.1",
            "description": "No completion by the deadline: ERROR (RESULT); a late completion changes nothing and a query gets ERROR.",
            "config": config(),
            "actions": LATER,
            "steps": [request(1.0, 10, 8), complete(7.0, 10, OK), query(8.0, 11, 10)],
            "end": 10.0,
        },
        {
            "name": "pending_action_cut_by_a_reboot_is_interrupted",
            "spec": "4.1",
            "description": "A reboot cuts a pending action: INTERRUPTED, its RESULT after boot; the completion that comes after is ignored.",
            "config": config(),
            "actions": LATER,
            "steps": [request(1.0, 10, 8), {"at": 2.0, "reboot": True}, complete(3.0, 10, OK), ack(8.0, 10)],
            "end": 20.0,
        },
        {
            "name": "pending_instant_action_is_error",
            "spec": "4.1",
            "description": "An instant action (no time) cannot complete later: PENDING there is answered ERROR.",
            "config": config(),
            "actions": LATER,
            "steps": [request(1.0, 10, 4), complete(1.5, 10, OK)],
            "end": 5.0,
        },
    ]


def generate() -> dict:
    out = []
    for case in cases():
        out.append({**case, "expect": replay(case)})
    return {
        "layer": "device endpoint, plain frames above the security layer (protocol_spec.md 16.1)",
        "rules": [
            "time advances event by event; at an instant: timers due before it, then the input, then timers due at it",
            "every random draw uniform(a, b) takes (a + b) / 2",
            "actions: per ACTION 'time' (s, default 0) and 'result' (default OK); 'run' is logged when the action starts, its result counts once 'time' is over",
            "'pending': the action returns PENDING (run result -1), 'time' is its deadline; a 'complete' step calls Device.complete and logs whether it was taken",
            "expect lists every sent frame ('tx') and every action run ('run'), in order",
        ],
        "cases": out,
    }


def main() -> None:
    json.dump(generate(), sys.stdout, indent=1, ensure_ascii=False)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
