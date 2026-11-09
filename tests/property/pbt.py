"""Stateful property-based testing (QuickCheck state-machine method) against models written from the spec.

* ``window``: ``secure.ReplayWindow`` against a set model: a counter is new iff it was never
  accepted and lies above the highest one or less than ``WINDOW`` below it.
* ``dedup``: ``device.Device`` against a model of spec 4.1 / 9 (per-sender records:
  ``dedup_window`` closed + up to ``max_open`` open, senders forgotten "最久没出现的" first,
  queries make NOT_RUN final, reboot turns ACCEPTED into INTERRUPTED (wire v5), ``dedup_ttl``). Random
  operation sequences (requests, retries, queries, RESULT acks, time, reboots, three senders);
  on a divergence the sequence is shrunk (delta debugging) to a minimal one.

    python tests/property/pbt.py KIND SECONDS SEED   # or through pytest: tests/property/test_properties.py
"""

from __future__ import annotations

import json
import random
import sys
import time

from cairnmesh import secure as S
from cairnmesh.device import Device
from cairnmesh.frames import QUERY_ACTION, FrameKind, ReplyFrame, ReplyResult, RequestFrame, decode_frame

OK, ERR, ACC, NOT_RUN, INTR = ReplyResult.OK, ReplyResult.ERROR, ReplyResult.ACCEPTED, ReplyResult.NOT_RUN, ReplyResult.INTERRUPTED

# ------------------------------------------------------------------ replay window


def window_case(rng: random.Random, n: int) -> list:
    ops, base = [], rng.choice((0, 1, 1_700_000_000, 0xFFFFFFFF - 200))
    for _ in range(n):
        r = rng.random()
        if r < 0.5:
            base = min(0xFFFFFFFF, base + rng.choice((1, 1, 2, 3, 31, 32, 33, 40)))
            ops.append(("rx", base))
        elif r < 0.85:
            ops.append(("rx", max(0, base - rng.randrange(0, 40))))
        elif r < 0.95:
            ops.append(("rx", max(0, min(0xFFFFFFFF, base + rng.randrange(-100, 100)))))
        else:
            ops.append(("restore", None))
    return ops


def window_check(ops) -> str | None:
    real, accepted, floor = S.ReplayWindow(), set(), None
    for k, (op, f) in enumerate(ops):
        if op == "restore":
            last = real.last
            if last is not None:
                real, floor = S.ReplayWindow.restored(last), last
            continue
        top = max(accepted) if accepted else None
        fresh = f not in accepted and (floor is None or f > floor) and (top is None or f > top or top - f < S.WINDOW)
        got = real.check(f)
        if got != fresh:
            return f"op {k}: check({f}) = {got}, model {fresh} (top={top}, floor={floor})"
        if got:
            real.accept(f)
            accepted.add(f)
    return None


# ------------------------------------------------------------------ device dedup model

CTL, MOBILE, THIRD = 0xF0, 0xF1, 0xF2
DEV = 5
SLOW = 5.0
TTL = 300.0


class Model:
    """Spec 4.1 / 9, written from the text, not from device.py."""

    def __init__(self, recency: str) -> None:
        self.recency = recency  # "spec": any appearance refreshes a sender; "remember": only new records
        self.recs: dict[int, list[list]] = {}
        self.order: list[int] = []
        self.done_at: dict[tuple[int, int], float] = {}

    def _touch(self, s, new_record: bool):
        if s not in self.recs:
            if len(self.recs) >= 2:
                victim = self.order.pop(0)
                del self.recs[victim]
            self.recs[s] = []
            self.order.append(s)
        elif self.recency == "spec" or new_record:
            self.order.remove(s)
            self.order.append(s)

    def _expire(self, s, now):
        recs = self.recs.get(s, [])
        while recs and now - recs[0][2] > TTL:
            recs.pop(0)

    def find(self, s, mid, now):
        self._expire(s, now)
        return next((r for r in self.recs.get(s, []) if r[0] == mid), None)

    def _add(self, s, mid, result, now, open_):
        self.recs[s].append([mid, result, now, open_])
        recs = self.recs[s]
        while sum(not r[3] for r in recs) > 4:
            recs.remove(next(r for r in recs if not r[3]))

    def request(self, s, mid, slow, result, now):
        """(runs?, answer or None)"""
        rec = self.find(s, mid, now) if s in self.recs else None
        self._touch(s, rec is None)
        if rec is not None:
            if rec[1] != ACC:
                self._close(s, mid)  # the final result went out, as for a query (STATUS P4)
            return False, rec[1]
        if slow:
            if sum(r[3] for r in self.recs[s]) >= 4:
                self._add(s, mid, NOT_RUN, now, False)
                return False, NOT_RUN
            self._add(s, mid, ACC, now, True)
            self.done_at[(s, mid)] = (now + SLOW, result)
            return True, ACC
        self._add(s, mid, result, now, False)
        return True, result

    def query(self, s, mid, now):
        rec = self.find(s, mid, now) if s in self.recs else None
        self._touch(s, rec is None)
        if rec is None:
            self._add(s, mid, NOT_RUN, now, False)
            return NOT_RUN
        if rec[1] != ACC:
            self._close(s, mid)
        return rec[1]

    def _close(self, s, mid):
        for r in self.recs.get(s, []):
            if r[0] == mid and r[1] != ACC:
                r[3] = False
        recs = self.recs.get(s, [])
        while sum(not r[3] for r in recs) > 4:
            recs.remove(next(r for r in recs if not r[3]))

    def ack(self, s, mid):
        self._close(s, mid)

    def advance(self, now):
        for key, (t, result) in list(self.done_at.items()):
            if now >= t:
                del self.done_at[key]
                for r in self.recs.get(key[0], []):
                    if r[0] == key[1]:
                        r[1] = result

    def reboot(self, now):
        self.done_at.clear()
        for recs in self.recs.values():
            for r in recs:
                if r[1] == ACC:
                    r[1] = INTR
                r[2] = now


class Clock:
    now = 0.0

    def __call__(self):
        return self.now


class Sink:
    def __init__(self):
        self.sent = []

    def set_receiver(self, cb):
        pass

    def send(self, data):
        self.sent.append(bytes(data))


def dedup_case(rng: random.Random, n: int) -> list:
    ops, issued = [], []
    for _ in range(n):
        r = rng.random()
        s = rng.choice((CTL, CTL, CTL, MOBILE, THIRD)) if rng.random() < 0.3 else CTL
        if r < 0.45 or not issued:
            mid = (rng.choice((1, 1, 2)) << 8) | rng.randrange(0, 24)
            ops.append(("req", s, mid, rng.random() < 0.3, rng.choice((1, 2, 3))))
            issued.append((s, mid))
        elif r < 0.65:
            s, mid = rng.choice(issued)
            ops.append(("req", s, mid, rng.random() < 0.3, rng.choice((1, 2, 3))))  # retry (or reuse) of an id
        elif r < 0.78:
            s, mid = rng.choice(issued) if rng.random() < 0.7 else (s, (1 << 8) | rng.randrange(24))
            ops.append(("query", s, mid))
        elif r < 0.86:
            s, mid = rng.choice(issued)
            ops.append(("ack", s, mid))
        elif r < 0.97:
            ops.append(("time", rng.choice((0.5, 2.0, 6.0, 60.0, 400.0))))
        else:
            ops.append(("reboot",))
    return ops


def _result_of(value):
    return ERR if value == 3 else OK


def dedup_check(ops, recency="spec") -> str | None:
    clock, sink = Clock(), Sink()
    ran = []
    dev = Device(sink, DEV, awake=True, sleep_delay=(0.0, 0.0), dedup_ttl=TTL, heartbeat_interval=1e9,
                 action_time=lambda f: SLOW if f.action == 2 else 0.0, rng=random.Random(0), clock=clock)
    dev.perform_action = lambda f: (ran.append((f.sender_id, f.message_id)), _result_of(f.value))[1]
    model = Model(recency)
    for k, op in enumerate(ops):
        sink.sent.clear()
        before = len(ran)
        if op[0] == "req":
            _, s, mid, slow, value = op
            exp_run, exp_ans = model.request(s, mid, slow, _result_of(value), clock.now)
            dev.on_frame(RequestFrame(s, DEV, mid & 0xFF, 2 if slow else 1, value, mid >> 8).encode())
            got_run = len(ran) > before
            answers = [decode_frame(b) for b in sink.sent if b[0] == FrameKind.REPLY]
            got_ans = answers[0].result if answers else None
            if got_run != exp_run:
                return f"op {k} {op}: ran={got_run}, model {exp_run}"
            if got_ans is not None and got_ans != exp_ans:
                return f"op {k} {op}: answered {ReplyResult(got_ans).name}, model {ReplyResult(exp_ans).name}"
        elif op[0] == "query":
            _, s, mid = op
            exp = model.query(s, mid, clock.now)
            dev.on_frame(RequestFrame(s, DEV, mid & 0xFF, QUERY_ACTION, mid & 0xFF, mid >> 8).encode())
            answers = [decode_frame(b) for b in sink.sent if b[0] == FrameKind.REPLY]
            got = answers[0].result if answers else None
            if len(ran) > before:
                return f"op {k} {op}: a query ran an action"
            if got != exp:
                return f"op {k} {op}: query answered {got}, model {ReplyResult(exp).name}"
        elif op[0] == "ack":
            _, s, mid = op
            model.ack(s, mid)
            dev.on_frame(ReplyFrame(s, DEV, mid & 0xFF, OK, mid >> 8).encode())
        elif op[0] == "time":
            end = clock.now + op[1]
            while clock.now < end:
                clock.now = min(end, clock.now + 0.25)
                dev.tick(clock.now)
                model.advance(clock.now)
        elif op[0] == "reboot":
            model.reboot(clock.now)
            dev.reset()
    return None


def shrink(ops, check):
    """Delta debugging: drop chunks, then single ops, while the failure stays."""
    n = 2
    while len(ops) >= 2:
        chunk = max(1, len(ops) // n)
        for start in range(0, len(ops), chunk):
            cand = ops[:start] + ops[start + chunk:]
            if check(cand):
                ops, n = cand, max(n - 1, 2)
                break
        else:
            if chunk == 1:
                break
            n = min(len(ops), n * 2)
    return ops


def run(kind: str, seconds: float, seed: int, max_cases: int | None = None) -> dict:
    """Random cases until ``seconds`` pass or ``max_cases`` ran (then the outcome depends on ``seed`` only)."""

    rng = random.Random(seed)
    end = time.monotonic() + seconds
    cases, failures = 0, {}
    while time.monotonic() < end and (max_cases is None or cases < max_cases):
        cases += 1
        if kind == "window":
            ops = window_case(rng, rng.randrange(5, 120))
            err = window_check(ops)
            if err:
                small = shrink(ops, lambda o: window_check(o) is not None)
                key = "window:" + window_check(small).split(":")[1][:40]
                failures.setdefault(key, {"count": 0, "ops": small, "error": window_check(small)})["count"] += 1
        else:
            ops = dedup_case(rng, rng.randrange(5, 80))
            err = dedup_check(ops)
            if err:
                small = shrink(ops, lambda o: dedup_check(o) is not None)
                msg = dedup_check(small)
                # Same divergence with the code's notion of recency? Then it is only the sender-order rule.
                variant = "recency" if dedup_check(small, "remember") is None else "other"
                key = f"dedup:{variant}:{msg.split(':')[1].split(',')[0][:50]}"
                f = failures.setdefault(key, {"count": 0, "ops": small, "error": msg})
                f["count"] += 1
                if len(small) < len(f["ops"]):
                    f.update(ops=small, error=msg)
    return {"kind": kind, "seed": seed, "cases": cases, "failures": failures}


if __name__ == "__main__":
    print(json.dumps(run(sys.argv[1], float(sys.argv[2]), int(sys.argv[3])), indent=1, default=str))
