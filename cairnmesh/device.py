"""The device endpoint: at-most-once execution, status queries, ACCEPTED + RESULT (spec 4.1; the reference for the 16.1 vectors)."""

from __future__ import annotations

import heapq
import itertools
import random
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable, Optional, Union

from .frames import (
    DEFAULT_CONTROLLER_ID,
    QUERY_ACTION,
    HeartbeatFrame,
    NodeState,
    ReplyFrame,
    ReplyResult,
    RequestFrame,
    ResultFrame,
    WakeFrame,
    decode_frame,
)
from .transport import Transport

#: How long a handled request is remembered at most (Q21, 2026-10-21: 7 days,
#: counted from boot). Records normally leave the cache only when the same
#: sender's newer requests push them out; this bound only matters when the
#: controller lost its state file and may reuse a message id (layer 3).
DEFAULT_RECORD_KEEP = 7 * 24 * 3600.0
#: Senders whose records are kept (the controller plus one spare).
DEFAULT_MAX_SENDERS = 2
#: A RESULT goes again after this (doubling, ±25 %) until the controller
#: acknowledges it, ``DEFAULT_RESULT_RETRIES`` times at most; the record keeps
#: the result for the controller's query after that.
DEFAULT_RESULT_TIMEOUT = 10.0
DEFAULT_RESULT_RETRIES = 4
#: Records per sender that are still open (running, or the result not yet
#: acknowledged): never pushed out. A slow request past them is answered
#: NOT_RUN (refused, and recorded so).
DEFAULT_MAX_OPEN = 4
#: An action at most this long is waited for, its result in the answer.
#: Measured (5 hops, SF7, 6 seeds × 10 commands back to back): ACCEPTED +
#: RESULT for 0.1–0.5 s actions met the next command head on (up to 13
#: breaks in 60, ×3 the sends); waited for, 0 breaks at the plain cost. The
#: last relay gives up on an answer after about 0.8–1 s at SF7 (four tries
#: of ``route_hop_timeout`` with one hop left), so the wait stays below that.
DEFAULT_ANSWER_BUDGET = 0.6
#: ``perform_action`` returns this when the action's result is known only
#: later: the application reports it with ``Device.complete``. Internal, it
#: never goes on air (spec 4.1, "结果后知的动作").
PENDING = -1

IncomingFrame = Union[WakeFrame, RequestFrame, ReplyFrame]


@dataclass
class _Outgoing:
    """A RESULT waiting to be sent or acknowledged."""

    due: float
    sent: int = 0


class Device:
    """A minimal endpoint that executes each request at most once.

    The device is non-blocking and single-threaded: frames arrive through
    the transport callback and time advances through ``tick()``, mirroring
    a main loop on a microcontroller.

    Duplicate suppression: for each sender (at most ``max_senders``, the one
    heard longest ago is forgotten first) the (message id, result) of its
    last ``dedup_window`` closed requests are remembered (open ones, below,
    on top of them). A request whose
    sender and message id (EPOCH and SEQUENCE) are among them is a
    retransmission (possibly one that arrived out of order) and is answered
    with the stored result without running the action again.

    Layer 3 (2026-10-21): a record stays until the same sender's newer
    requests push it out, so a status query (below) finds it however long
    the node was unreachable. The controller is stop-and-wait per node and
    holds the node's queue while a command is unconfirmed, so nothing pushes
    that record out meanwhile; other senders cannot either. ``dedup_ttl``
    (default 7 days, counted from boot) only bounds the case where the
    controller lost its state file and may reuse an id; ``None`` keeps
    records forever.

    Status query (Q18): a REQUEST whose ACTION is ``QUERY_ACTION`` asks for
    the result of the request with SEQUENCE = VALUE (same SENDER and EPOCH).
    It runs nothing and is not cached itself (like WAKE). A remembered
    request is answered with its result; an unknown one with ``NOT_RUN``,
    and that answer is cached under the asked id, so a late copy of the
    request is answered ``NOT_RUN`` instead of running: "not run" is final.

    Commit, then act (2026-10-21, user: plan A): the relays wait about
    ``route_answer`` (20 ms) for the answer; a device that answered only
    once a slow action was over had its path declared broken (5 hops, SF7:
    every command at 1 s, 2.9 × the sends). An action that takes longer
    than ``answer_budget`` (``action_time``) is logged ACCEPTED first, the
    answer ACCEPTED goes at once, the action runs, and its result follows in
    a RESULT frame, sent again until the controller acknowledges it. The
    record then holds the final result; a query meanwhile is answered
    ACCEPTED. A record still open (running, or its RESULT not acknowledged
    or asked for) is never pushed out; past ``max_open`` of them a slow
    request is refused with NOT_RUN. A fast action answers its result at
    once, as before, and sends no RESULT.

    Result known later (2026-10-22, user: "可以，改吧"): an action whose
    outcome only shows while it runs (a motor that may stall, a sensor that
    may not answer) returns ``PENDING`` from ``perform_action`` and reports
    with ``complete(sender, message id, result)`` once it knows. Its
    ``action_time`` is then its deadline, not its length: the answer (within
    the budget) or the RESULT (past it) goes as soon as it completes, and at
    the deadline without a completion the record turns ERROR. A completion
    after the deadline or after a reboot changes nothing (counted): every
    request still ends with exactly one final result. An instant action
    (``action_time`` 0) cannot complete later: ``PENDING`` there is ERROR.

    The cache survives a reboot (``reset`` keeps it), as if persisted: a
    retry of a request executed just before a reboot can still be in a
    relay's queue and must not run again (measured at 30 % loss: 2 double
    executions in 8 runs with a RAM-only cache). Firmware logs each entry to
    Flash before running the action (8 B per entry, two alternating pages);
    the board has no VBAT, so a supply cut loses the RTC as well: entries
    read back at boot count as written at boot and are kept ``dedup_ttl``
    from then (conservative: a reboot only keeps them longer; ``reset`` does
    the same here). A reboot between logging and running answers
    the retry with the stored result without running it (at most once). An
    action cut short by a reboot is recorded INTERRUPTED (it never runs again),
    and every open record's RESULT is sent again after the boot.

    While in ``SLEEP`` every frame is processed only after a random
    ``sleep_delay`` (a duty-cycled receiver); a WAKE frame switches the
    device to ``READY``. Heartbeats are sent every ``heartbeat_interval``
    seconds (±15 % jitter) in both states.
    """

    def __init__(
        self,
        transport: Transport,
        device_id: int = 1,
        *,
        controller_id: int = DEFAULT_CONTROLLER_ID,
        heartbeat_interval: float = 3.0,
        sleep_delay: tuple[float, float] = (1.0, 3.0),
        awake: bool = False,
        dedup_window: int = 4,
        dedup_ttl: Optional[float] = DEFAULT_RECORD_KEEP,
        max_senders: int = DEFAULT_MAX_SENDERS,
        max_deferred: int = 8,
        action_time: Union[float, Callable[[RequestFrame], float]] = 0.0,
        answer_budget: float = DEFAULT_ANSWER_BUDGET,
        result_timeout: float = DEFAULT_RESULT_TIMEOUT,
        result_retries: int = DEFAULT_RESULT_RETRIES,
        max_open: int = DEFAULT_MAX_OPEN,
        rng: Optional[random.Random] = None,
        clock: Callable[[], float] = time.monotonic,
        log: Optional[Callable[[str], None]] = None,
    ) -> None:
        if dedup_window < 1:
            raise ValueError("dedup_window must be at least 1")
        if max_senders < 1:
            raise ValueError("max_senders must be at least 1")
        if max_deferred < 1:
            raise ValueError("max_deferred must be at least 1")
        if max_open < 1:
            raise ValueError("max_open must be at least 1")
        self.transport = transport
        self.device_id = device_id
        self.controller_id = controller_id
        self.heartbeat_interval = heartbeat_interval
        self.sleep_delay = sleep_delay
        self.dedup_window = dedup_window
        self.dedup_ttl = dedup_ttl
        self.max_senders = max_senders
        self.max_deferred = max_deferred
        #: Seconds the action of a request takes (a number, or a function of the request).
        self.action_time = action_time
        #: An action at most this long is waited for: the answer carries its
        #: result (no RESULT frame). Longer ones answer ACCEPTED at once.
        self.answer_budget = answer_budget
        self.result_timeout = result_timeout
        self.result_retries = result_retries
        self.max_open = max_open
        self.rng = rng or random.Random()
        self.clock = clock
        self.log = log
        self._awake_on_reset = awake
        # [MEM] request dedup cache | 端: device(持久: Flash 日志) | 上限: max_senders(默认 2) × dedup_window 项(默认 4，未结束的另计、≤ max_open) | 每项: 4 B (sender, message_id 2 B, result) + 4 B 开机计时 + 1 位未结束
        # One list per sender, oldest entry first, like a fixed array in C;
        # dict order is the senders' last-use order. Each entry is
        # (message id, result, logged at, open); an open one is never pushed out.
        # Not cleared by ``reset``: it outlives reboots (see the class docstring).
        self._recent: dict[int, deque[tuple[int, int, float, bool]]] = {}
        self._last_entry: Optional[tuple[int, int]] = None
        self.stats = {
            "accepted": 0,
            "results_sent": 0,
            "results_acked": 0,
            "results_abandoned": 0,
            "refused_busy": 0,
            "deadline_missed": 0,
            "late_completions": 0,
        }
        self.reset()
        transport.set_receiver(self.on_frame)

    def reset(self) -> None:
        """Return to power-on state (used by the simulator's power switch); the dedup cache is kept."""

        # Entries read back at boot count as written at boot (no RTC). An
        # action the reboot cut short is over: INTERRUPTED (no final result;
        # the action may have completed or not; never run again). Not ERROR:
        # that says the action failed (2026-11-08, wire format v5).
        now = self.clock()
        reopened = []
        for sender, records in self._recent.items():
            for index, (message_id, result, _, open_) in enumerate(records):
                if result == ReplyResult.ACCEPTED:
                    result = ReplyResult.INTERRUPTED
                records[index] = (message_id, result, now, open_)
                if open_:
                    reopened.append((sender, message_id))

        self.state = NodeState.READY if self._awake_on_reset else NodeState.SLEEP
        self.handled_count = 0
        #: Frames dropped because the sleep queue was full or already held them.
        self.deferred_dropped = 0
        self.heartbeat_sequence = 0
        # Spread first heartbeats so nodes do not all report at once.
        self._next_heartbeat = self.clock() + self.rng.uniform(
            0.2, max(0.2, self.heartbeat_interval)
        )
        # [MEM] frames deferred while asleep | 端: device(模拟占空比接收) | 上限: max_deferred(默认 8) | 每项: 1 帧(≤7 B) + 到期时间
        # Heap of (due, arrival order, frame); the counter keeps equal due
        # times in arrival order and stops heapq from comparing frames.
        self._deferred: list[tuple[float, int, IncomingFrame]] = []
        self._arrivals = itertools.count()
        # [MEM] actions running | 端: device | 上限: max_senders × max_open | 每项: (sender, message_id) 3 B + 完成时刻 4 B + 结果 1 B
        self._running: dict[tuple[int, int], tuple[float, int]] = {}
        # [MEM] answers waiting for a short action | 端: device | 上限: 同上 | 每项: 3 B + 完成时刻 4 B + 结果 1 B + 请求 7 B
        self._answers: dict[tuple[int, int], tuple[float, int, RequestFrame]] = {}
        self._told: set[tuple[int, int]] = set()
        # [MEM] RESULT frames to send or be acknowledged | 端: device | 上限: 同上 | 每项: 3 B + 计时 4 B + 次数 1 B
        self._results: dict[tuple[int, int], _Outgoing] = {
            key: _Outgoing(now + self.rng.uniform(0.0, self.result_timeout)) for key in reopened
        }

    @property
    def last_sequence(self) -> Optional[int]:
        """Sequence number of the most recently handled request."""

        return self._last_entry[1] & 0xFF if self._last_entry else None

    # ------------------------------------------------------------------ input

    def on_frame(self, raw_data: bytes) -> None:
        """Transport callback: accept one frame addressed to this device."""

        try:
            frame = decode_frame(raw_data)
        except ValueError as exc:
            self._log(f"[DEV] 帧无效: {exc}")
            return
        if not isinstance(frame, (WakeFrame, RequestFrame, ReplyFrame)):
            return
        if frame.target_id != self.device_id:
            return
        if isinstance(frame, ReplyFrame):
            self._on_ack(frame)  # handled at once: it only stops a repeat
            return

        if self.state == NodeState.SLEEP and self.sleep_delay[1] > 0:
            self._defer(frame)
            return
        self.handle_frame(frame)

    def _on_ack(self, frame: ReplyFrame) -> None:
        key = (frame.sender_id, frame.message_id)
        if self._results.pop(key, None) is not None:
            self.stats["results_acked"] += 1
            self._close(key)

    def _defer(self, frame: IncomingFrame) -> None:
        # A retransmission of a frame that is already waiting adds nothing:
        # the waiting copy will be answered. When the queue is full the
        # frame is dropped like a lost one and the controller retries.
        waiting = any(entry[2] == frame for entry in self._deferred)
        if waiting or len(self._deferred) >= self.max_deferred:
            self.deferred_dropped += 1
            return
        due = self.clock() + self.rng.uniform(*self.sleep_delay)
        heapq.heappush(self._deferred, (due, next(self._arrivals), frame))

    def next_due(self) -> float:
        """The earliest time ``tick`` does anything (``MeshNetwork`` skips idle ticks)."""

        due = min(self._deferred[0][0], self._next_heartbeat) if self._deferred else self._next_heartbeat
        for done_at, _ in self._running.values():
            due = min(due, done_at)
        for done_at, _, _ in self._answers.values():
            due = min(due, done_at)
        for out in self._results.values():
            due = min(due, out.due)
        return due

    def tick(self, now: Optional[float] = None) -> None:
        """Advance time: process deferred frames, finish actions, send RESULTs and heartbeats."""

        now = self.clock() if now is None else now
        while self._deferred and self._deferred[0][0] <= now:
            _, _, frame = heapq.heappop(self._deferred)
            self.handle_frame(frame)

        for key, (done_at, result, frame) in list(self._answers.items()):
            if now >= done_at:
                # Over within the budget: the answer carries the result, no RESULT.
                del self._answers[key]
                result = self._deadline(result)
                self._set_result(key, result)
                if key in self._told:  # a retry was answered ACCEPTED meanwhile: report it as well
                    self._told.discard(key)
                    self._results[key] = _Outgoing(now)
                else:
                    self._close(key)
                self.transport.send(self._reply(frame, result).encode())

        for key, (done_at, result) in list(self._running.items()):
            if now >= done_at:
                del self._running[key]
                self._set_result(key, self._deadline(result))
                self._results[key] = _Outgoing(now)

        for key, out in list(self._results.items()):
            if now < out.due:
                continue
            if out.sent > self.result_retries:
                # The record keeps the result (still open): the controller asks for it.
                del self._results[key]
                self.stats["results_abandoned"] += 1
                continue
            self._send_result(key)
            out.due = now + self.result_timeout * (2 ** out.sent) * self.rng.uniform(0.75, 1.25)
            out.sent += 1

        if now >= self._next_heartbeat:
            self.send_heartbeat()
            jitter = self.rng.uniform(0.85, 1.15)
            self._next_heartbeat = now + self.heartbeat_interval * jitter

    # --------------------------------------------------------------- handling

    def handle_frame(self, frame: IncomingFrame) -> Optional[ReplyFrame]:
        """Process one decoded frame, send and return the reply (if any)."""

        if isinstance(frame, WakeFrame):
            reply = self._handle_wake(frame)
        elif isinstance(frame, RequestFrame):
            reply = self._handle_request(frame)
        else:
            reply = None
        if reply is not None:
            self.transport.send(reply.encode())
        return reply

    def _handle_wake(self, frame: WakeFrame) -> ReplyFrame:
        # WAKE 是幂等的,不参与去重,也不写入去重缓存:
        # 休眠期间积压的 WAKE 重发可能在 REQUEST 之后才被处理,
        # 若在这里记录序号,会挤掉缓存里的 REQUEST,使随后到达的 REQUEST 重发被重复执行。
        self.state = NodeState.READY
        self._log(f"[DEV 0x{self.device_id:02X}] 已进入就绪状态")
        return self._reply(frame, ReplyResult.OK)

    def _handle_request(self, frame: RequestFrame) -> ReplyFrame:
        now = self.clock()
        self._heard(frame.sender_id)
        if frame.action == QUERY_ACTION:
            return self._handle_query(frame, now)
        previous_result = self._recent_result(frame.sender_id, frame.message_id, now)
        if previous_result is not None:
            self._log(
                f"[DEV 0x{self.device_id:02X}] EPOCH={frame.epoch} SEQUENCE={frame.sequence} "
                "已经处理过，判定为重复帧"
            )
            # Replay the original result: a lost ERROR reply must not turn
            # into OK just because the retry skipped the action.
            if previous_result == ReplyResult.ACCEPTED and (frame.sender_id, frame.message_id) in self._answers:
                self._told.add((frame.sender_id, frame.message_id))  # it heard ACCEPTED: a RESULT will follow
            elif previous_result != ReplyResult.ACCEPTED:
                # The final result goes out: the record closes, as for a query
                # (spec 4.1). Left open, the controller would take the id as
                # free while the device still keeps it (STATUS P4).
                self._answered((frame.sender_id, frame.message_id))
            return self._reply(frame, previous_result)

        duration = self.action_time(frame) if callable(self.action_time) else self.action_time
        if duration > 0:
            return self._accept(frame, duration, now)
        result = self._run(frame)
        if result == PENDING:
            # No time to complete later: an instant action knows its result.
            self._log(f"[DEV 0x{self.device_id:02X}] 即时动作返回 PENDING，记为 ERROR")
            result = ReplyResult.ERROR
        # Remembered even on failure: the retry must get the same ERROR
        # instead of running a half-done action a second time.
        self._remember(frame.sender_id, frame.message_id, result, now)
        return self._reply(frame, result)

    def _accept(self, frame: RequestFrame, duration: float, now: float) -> Optional[ReplyFrame]:
        """An action that takes time: log it ACCEPTED first, then run it.

        Within ``answer_budget`` the answer waits for it and carries the
        result; past that, ACCEPTED goes at once and a RESULT follows.
        """

        sender = frame.sender_id
        if sum(entry[3] for entry in self._recent.get(sender, ())) >= self.max_open:
            # Every open slot is taken: refused, and final (a late copy is refused too).
            self.stats["refused_busy"] += 1
            self._remember(sender, frame.message_id, ReplyResult.NOT_RUN, now)
            return self._reply(frame, ReplyResult.NOT_RUN)
        self._remember(sender, frame.message_id, ReplyResult.ACCEPTED, now, open_=True)  # logged before it runs
        key = (sender, frame.message_id)
        if duration <= self.answer_budget:
            self._answers[key] = (now + duration, self._run(frame), frame)
            return None
        self.stats["accepted"] += 1
        self._running[key] = (now + duration, self._run(frame))
        return self._reply(frame, ReplyResult.ACCEPTED)

    def complete(self, sender_id: int, message_id: int, result: int) -> bool:
        """Report the result of an action that returned ``PENDING``.

        ``message_id`` is the request's (``frame.message_id``); ``result`` is
        ``OK`` or ``ERROR``. The answer or RESULT goes at the next ``tick``.
        False when the action is not waiting any more (deadline passed, a
        reboot cut it, or it never returned ``PENDING``): nothing changes.
        """

        if result not in (ReplyResult.OK, ReplyResult.ERROR):
            raise ValueError("an action completes with OK or ERROR")
        key = (sender_id, message_id)
        now = self.clock()
        answer = self._answers.get(key)
        if answer is not None and answer[1] == PENDING:
            self._answers[key] = (now, int(result), answer[2])
            return True
        running = self._running.get(key)
        if running is not None and running[1] == PENDING:
            self._running[key] = (now, int(result))
            return True
        self.stats["late_completions"] += 1
        self._log(f"[DEV 0x{self.device_id:02X}] 迟到的完成 0x{message_id:04X}，忽略")
        return False

    def _deadline(self, result: int) -> int:
        """The result at the action's end: still ``PENDING`` means the deadline passed."""

        if result == PENDING:
            self.stats["deadline_missed"] += 1
            return ReplyResult.ERROR
        return result

    def _run(self, frame: RequestFrame) -> int:
        try:
            return self.perform_action(frame)
        except Exception as exc:  # noqa: BLE001 - a failing action is an ERROR reply
            self._log(f"[DEV 0x{self.device_id:02X}] 动作异常: {type(exc).__name__}: {exc}")
            return ReplyResult.ERROR

    def _handle_query(self, frame: RequestFrame, now: float) -> ReplyFrame:
        asked = frame.epoch << 8 | frame.value
        result = self._recent_result(frame.sender_id, asked, now)
        if result is None:
            # Never seen: say so and make it final, a late copy must not run.
            result = ReplyResult.NOT_RUN
            self._remember(frame.sender_id, asked, result, now)
        elif result != ReplyResult.ACCEPTED:
            self._answered((frame.sender_id, asked))
        elif (frame.sender_id, asked) in self._answers:
            # It met a short action mid-run and heard ACCEPTED: a RESULT will follow, as for a retry (review A6).
            self._told.add((frame.sender_id, asked))
        self._log(
            f"[DEV 0x{self.device_id:02X}] 查询 EPOCH={frame.epoch} SEQUENCE={frame.value} → 0x{result:02X}"
        )
        return self._reply(frame, result)

    def _recent_result(self, sender_id: int, message_id: int, now: float) -> Optional[int]:
        records = self._recent.get(sender_id)
        if not records:
            return None
        if self.dedup_ttl is not None:
            while records and now - records[0][2] > self.dedup_ttl:
                records.popleft()
        for handled_id, result, _, _ in records:
            if handled_id == message_id:
                return result
        return None

    def _heard(self, sender_id: int) -> None:
        """A REQUEST (a retry, a query too) from ``sender_id``: it becomes the one heard from last.

        Past ``max_senders`` the sender heard from longest ago is forgotten (spec
        4.1). Only new records counted before (Q23, 2026-10-22): a sender just
        queried or retrying could go first, and its next retry ran again.
        """

        records = self._recent.pop(sender_id, None)
        if records is not None:
            self._recent[sender_id] = records

    def _remember(self, sender_id: int, message_id: int, result: int, now: float, open_: bool = False) -> None:
        records = self._recent.pop(sender_id, None)
        if records is None:
            records = deque()
            if len(self._recent) >= self.max_senders:
                del self._recent[next(iter(self._recent))]
        records.append((message_id, result, now, open_))
        self._recent[sender_id] = records
        self._trim(sender_id)
        self._last_entry = (sender_id, message_id)

    def _set_result(self, key: tuple[int, int], result: int) -> None:
        """The action is over: its record holds the result (still open until acknowledged)."""

        records = self._recent.get(key[0], ())
        for index, (message_id, _, logged, open_) in enumerate(records):
            if message_id == key[1]:
                records[index] = (message_id, result, logged, open_)

    def _answered(self, key: tuple[int, int]) -> None:
        """A final result went out from the record: no RESULT to send any more, the record closes."""

        if self._results.pop(key, None) is not None:
            self.stats["results_acked"] += 1
        self._close(key)

    def _close(self, key: tuple[int, int]) -> None:
        records = self._recent.get(key[0], ())
        for index, (message_id, result, logged, open_) in enumerate(records):
            if message_id == key[1] and open_ and result != ReplyResult.ACCEPTED:
                records[index] = (message_id, result, logged, False)
        if records:
            self._trim(key[0])

    def _trim(self, sender_id: int) -> None:
        # ``dedup_window`` closed records per sender, the oldest pushed out first;
        # open ones (running, or their RESULT not acknowledged or asked for) come
        # on top of them, at most ``max_open``, and are never pushed out (spec 4.1,
        # 9). Counting open ones in the window ran a request twice (review A2).
        records = self._recent[sender_id]
        while sum(not entry[3] for entry in records) > self.dedup_window:
            del records[next(index for index, entry in enumerate(records) if not entry[3])]

    def perform_action(self, frame: RequestFrame) -> int:
        """Perform the action (default: count the call and report OK; an application overrides this).

        The example only counts the call and reports success. A real
        application can replace this method with its own business logic.
        """

        self.handled_count += 1
        self._log(
            f"[DEV 0x{self.device_id:02X}] >>> 处理 ACTION={frame.action}, "
            f"VALUE={frame.value}"
        )
        return ReplyResult.OK

    # ----------------------------------------------------------------- output

    def send_heartbeat(self) -> None:
        frame = HeartbeatFrame(
            sender_id=self.device_id,
            target_id=self.controller_id,
            sequence=self.heartbeat_sequence,
            state=self.state,
        )
        self.heartbeat_sequence = (self.heartbeat_sequence + 1) & 0xFF
        self.transport.send(frame.encode())

    def _send_result(self, key: tuple[int, int]) -> None:
        sender, message_id = key
        result = self._recent_result(sender, message_id, self.clock())
        if result is None or result == ReplyResult.ACCEPTED:
            self._results.pop(key, None)  # the record is gone (keep period): nothing to say
            return
        self.stats["results_sent"] += 1
        frame = ResultFrame(
            sender_id=self.device_id,
            target_id=sender,
            sequence=message_id & 0xFF,
            epoch=message_id >> 8,
            result=result,
        )
        self.transport.send(frame.encode())

    def _reply(self, frame: IncomingFrame, result: int) -> ReplyFrame:
        return ReplyFrame(
            sender_id=self.device_id,
            target_id=frame.sender_id,
            sequence=frame.sequence,
            epoch=frame.epoch,
            result=result,
        )

    def _log(self, message: str) -> None:
        if self.log is not None:
            self.log(message)
