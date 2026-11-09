"""The controller: builds requests, sends them reliably and settles every one with a certain outcome.

The controller is a non-blocking state machine:

- ``request()`` / ``wake()`` queue a frame and return a ``Future`` at once;
- ``on_frame()`` is the transport callback for incoming frames;
- ``tick()`` must be called periodically to drive retransmission.

Each node has its own stop-and-wait queue: at most one frame per node is in
flight, others wait their turn. Everything observable is published as an
event on ``self.events``; nothing is printed.
"""

from __future__ import annotations

import math
import threading
import time
from collections import OrderedDict, deque
from concurrent.futures import Future
from dataclasses import dataclass, field  # noqa: F401 - kept in the historical import surface
from random import Random
from typing import Callable, Optional, Union

from .events import (
    EventBus,
    FrameRejected,
    HeartbeatReceived,
    LinkError,
    LinkEvent,
    NodeEvent,
    NodeReported,
    ReplyIgnored,
    ReplyReceived,
    RequestCompleted,
    RequestConfirming,
    RequestFailed,
    RequestQuerying,
    RequestSent,
)
from .confirm_policy import (  # noqa: F401 - the confirmation constants remain public here
    CONFIRM_HEARD_FRESH,
    CONFIRM_NUDGE_GAP,
    DEFAULT_CONFIRM_LIMIT,
    DEFAULT_CONFIRM_QUIET_AFTER,
    DEFAULT_CONFIRM_SCHEDULE,
    DEFAULT_CONFIRM_WINDOW,
    RECORD_KEEP_MARGIN,
    ConfirmPolicy,
    HeardBook,
)
from .frames import (
    BROADCAST_ID,
    DEFAULT_CONTROLLER_ID,
    QUERY_ACTION,
    EventFrame,
    FrameKind,
    HeartbeatFrame,
    ReplyFrame,
    ReplyResult,
    ReportFrame,
    RequestFrame,
    ResultFrame,
    WakeFrame,
    decode_frame,
)
from .device import DEFAULT_RECORD_KEEP
from .id_ledger import IdLedger
from .late_ledger import LateLedger
from .state_store import LateRecord, PersistedRequest, PersistedState, StateStore
from .transport import Transport
from .pending import (  # noqa: F401 - historical controller import surface
    QUERY_IDS_KEPT,
    OutgoingFrame,
    Pending,
    Pending as _Pending,
    _Completion,
    _SideEffects,
    _uncancellable_future,
)
from .outcomes import (  # noqa: F401 - historical controller import surface
    ACCEPTED,
    DONE,
    INTERRUPTED,
    INTERRUPTED_NOTE,
    NOT_RUN,
    NOT_SENT,
    UNCONFIRMED,
    UNCONFIRMED_NOTE,
    Confirming,
    LateConfirmation,
    RequestOutcome,
    _KNOWN_REPLY_RESULTS,
    _is_unknown_reply_result,
    _unknown_reply_result_reason,
    confirming_event,
    failed_event,
    make_outcome,
    not_sent_pair,
)

#: A WAKE to a duty-cycled node may wait out its whole ``sleep_delay``
#: (default up to 3 s), so it gets at least this many retries by default.
MIN_WAKE_RETRIES = 10


#: Reports and module events remembered as published, per reporter (spec 4.2,
#: 19). A device has at most ``max_reports`` + ``max_events`` (8) waiting.
PUBLISHED_KEPT = 32

#: Default cap on one frame's retry window, in seconds (it came from the
#: device's former 30 s ``dedup_ttl``; records are now kept 7 days, spec 4.1).
DEFAULT_MAX_RETRY_WINDOW = 25.0

#: Closed records the device keeps per sender (its ``dedup_window``): a
#: record is pushed out once this many records made after it are closed.
DEDUP_WINDOW = 4
#: REQUEST ids per node, in this EPOCH, that the device may still keep;
#: past them the EPOCH moves on, so that a new id
#: never repeats one still kept. Kept are every id from the
#: ``DEDUP_WINDOW``-th last settled one on, and every one never settled: one
#: sent and lost entered nothing there, but one heard whose answer was lost
#: may stay open there however long (a slow action, its RESULT unasked).
#: The old fixed 16 last ids let commands abandoned while the node was
#: offline push the kept ones out: after a wrap a new command got a kept
#: id and was answered with the old result, never run.
KEPT_IDS_LIMIT = 128
#: REQUEST ids remembered per node across EPOCHs (persisted), the oldest
#: dropped past it: they matter again only when the EPOCH comes round.
KEPT_IDS_MAX = 256
#: Requests that may be sent (with ``force``) past one still confirming: the
#: device keeps ``dedup_window`` (4) per sender, so the fourth would push
#: its record out and a later query would wrongly say NOT_RUN.
MAX_PAST_CONFIRMING = 3
#: Without a state file and an explicit EPOCH, ``request`` refuses
#: (``NoStateFile``) unless this, or the controller's ``allow_volatile_epoch``,
#: says otherwise (2026-10-21 review D1, user's decision). Simulators and demos
#: pass ``allow_volatile_epoch=True``; the unit tests' ``conftest`` sets this.
VOLATILE_EPOCH_ALLOWED = False

#: A RESULT not in this long after ACCEPTED (2026-10-21): the controller asks
#: the device (a status query once the node's queue is free), then twice as
#: long each time, up to the slow round of ``confirm_schedule``.
COMPLETION_CHECK = 60.0


class QueueFull(RuntimeError):
    """A node already has ``max_queue`` frames queued or in flight."""


class NodeConfirming(RuntimeError):
    """The node has a request whose outcome is still being confirmed (layer 3); ``force`` sends anyway."""


class NoStateFile(RuntimeError):
    """No state file and no explicit EPOCH: a new command could repeat an id a device still keeps (review D1)."""


class Controller:
    """Build requests, send them, and match replies to what is in flight.

    Sequence numbers are allocated per node, so consecutive frames to one
    node always differ no matter how much traffic other nodes see. A retry
    reuses the original sequence number so the receiver can recognize it.

    Every frame also carries ``epoch``, which names this run of the
    controller; EPOCH and SEQUENCE together are the message id that devices
    deduplicate on. A restarted controller therefore cannot have a new
    request taken for a retry of an old one (and answered with the old
    result without running), and a reply from before the restart cannot
    resolve a new request. Devices keep records for days (layer 3), so an id
    must not repeat one still kept: with ``state_file`` the epoch is the
    saved one plus 1, and a REQUEST id never repeats one the device may
    still keep (every one from the ``DEDUP_WINDOW``-th last settled REQUEST
    on, and every one never settled; kept in the file too: the per-node
    counter, shared with WAKE, keepalives and probes, goes round in
    minutes). Past ``KEPT_IDS_LIMIT`` of them for a node the epoch moves on
    by 1 while running. Without a readable file ``epoch`` defaults to
    the Unix time in seconds, modulo 256, and ``state_warning`` says that
    only the devices' keep period then guards against a repeated id; each
    node's counter still starts at a random value (or ``initial_sequence``).
    ``max_queue`` bounds the frames queued per node; ``submit`` raises
    ``QueueFull`` beyond it. ``wake_max_retries`` defaults to
    ``max(max_retries, MIN_WAKE_RETRIES)``, trimmed (but never below
    ``max_retries``) so that the WAKE retry window fits ``max_retry_window``.

    A frame's whole retry window, ``(max_retries + 1) * timeout``, must stay
    within ``max_retry_window``, which has to stay below the devices'
    ``dedup_ttl`` (``DEFAULT_RECORD_KEEP``, 7 days): a retry arriving after the
    device forgot the request would run it again, so a longer window is
    refused with ``ValueError``.
    Raise both limits together, or pass ``None`` to switch the check off when
    the devices do not expire their cache.

    Status query (Q18): when a REQUEST's retries get no answer the device may
    still have run it (every reply lost). Until ``confirm_window`` seconds
    after its first send the controller then asks the device (a REQUEST with
    ``QUERY_ACTION``, VALUE = the asked SEQUENCE, same timeout) and resolves
    with the answer: the result (status ``DONE``), ``NOT_RUN`` (final: the
    device will not run a late copy either), or ``UNCONFIRMED`` with no
    answer or an unknown result code. The node's queue waits meanwhile, so
    no later request can push the asked one out of the device's cache.
    ``None`` turns queries off.

    Layer 3 (Q21): with ``confirm_forever`` the controller keeps asking. Past
    ``confirm_window`` the request is CONFIRMING
    (``RequestConfirming``) and asked in rounds (``confirm_schedule``, and at
    once when ``nudge`` says its node was heard) until the device answers;
    ``UNCONFIRMED`` comes from ``abandon`` (the operator), once the devices'
    ``record_keep`` is nearly over, from a result code this controller
    does not know, or, for an ordinary command, at ``confirm_limit`` (below).

    Importance: a request is **ordinary** unless
    the caller says ``important=True`` (a local property, not on the air; kept
    in ``state_file``). An ordinary one is confirmed at most ``confirm_limit``
    (30 min by default) after its first send, a hard point on the clock that
    the quiet rounds cannot push back: if its node was heard in the last
    ``CONFIRM_HEARD_FRESH`` one last round is asked, else it ends ``UNCONFIRMED``
    at once ("may have run", ``UNCONFIRMED_NOTE``), is never sent again and the
    queue goes on. After that nothing more is asked about it, but a late
    positive result is published (``LateConfirmed``, never changing the done
    ``Future``), and its ids stay protected for the keep period. An important
    one is confirmed on as described above. A restored command is asked at
    least one round before its limit is judged. A new REQUEST to the node is refused
    (``NodeConfirming``) meanwhile; ``force`` parks the confirming one, which
    is asked again once the node's queue is empty, for at most
    ``MAX_PAST_CONFIRMING`` requests. Confirming requests are kept in
    ``state_file`` and asked about again after a restart.
    """

    def __init__(
        self,
        transport: Transport,
        sender_id: int = DEFAULT_CONTROLLER_ID,
        *,
        timeout: float = 0.5,
        max_retries: int = 5,
        wake_max_retries: Optional[int] = None,
        max_queue: int = 32,
        initial_sequence: Optional[int] = None,
        epoch: Optional[int] = None,
        max_retry_window: Optional[float] = DEFAULT_MAX_RETRY_WINDOW,
        confirm_window: Optional[float] = DEFAULT_CONFIRM_WINDOW,
        confirm_forever: bool = True,
        confirm_schedule: tuple[float, float, float] = DEFAULT_CONFIRM_SCHEDULE,
        record_keep: Optional[float] = DEFAULT_RECORD_KEEP,
        confirm_quiet_after: Optional[float] = DEFAULT_CONFIRM_QUIET_AFTER,
        confirm_limit: Optional[float] = DEFAULT_CONFIRM_LIMIT,
        state_file: Optional[str] = None,
        allow_volatile_epoch: Optional[bool] = None,
        events: Optional[EventBus] = None,
        clock: Callable[[], float] = time.monotonic,
        rng: Optional[Random] = None,
    ) -> None:
        if max_queue < 1:
            raise ValueError("max_queue must be at least 1")
        self.max_retry_window = max_retry_window
        #: The mutable confirmation-rules object; gateway hooks stay on Controller.
        self.confirm = ConfirmPolicy(
            window=confirm_window,
            forever=confirm_forever,
            schedule=confirm_schedule,
            record_keep=record_keep,
            quiet_after=confirm_quiet_after,
            limit=confirm_limit,
        )
        #: ``round_wakes(node)``: would a query to it wake the whole network now?
        self.round_wakes: Optional[Callable[[int], bool]] = None
        #: ``timeouts_for(node)`` → ``(timeout, max_retries)`` for a round of
        #: queries (set by the gateway: the path changes while a request
        #: waits, and one restored from ``state_file`` has no timing of its own).
        self.timeouts_for: Optional[Callable[[int], tuple[float, int]]] = None
        #: ``None``: ``VOLATILE_EPOCH_ALLOWED`` decides (read at each request).
        self.allow_volatile_epoch = allow_volatile_epoch
        self._volatile = state_file is None and epoch is None
        #: Set when ``state_file`` could not be read (first run, or lost).
        self.state_warning: Optional[str] = None
        #: Times the EPOCH moved on while running (``KEPT_IDS_LIMIT``).
        self.epoch_moves = 0
        self.transport = transport
        self.sender_id = sender_id
        self.timeout = timeout
        self.max_retries = max_retries
        self.wake_max_retries = (
            max(max_retries, MIN_WAKE_RETRIES)
            if wake_max_retries is None
            else wake_max_retries
        )
        self.max_queue = max_queue
        self._check_window(timeout, max_retries)
        if wake_max_retries is None and max_retry_window is not None:
            # A slow link waits out the sleep delay in fewer, longer tries:
            # trim the default to the cap instead of refusing a valid timeout.
            fits = math.floor(max_retry_window / timeout) - 1
            if (fits + 1) * timeout > max_retry_window:  # float rounding
                fits -= 1
            self.wake_max_retries = max(max_retries, min(self.wake_max_retries, fits))
        self._check_window(timeout, self.wake_max_retries)
        if epoch is not None and not 0 <= epoch <= 0xFF:
            raise ValueError(f"epoch out of range 0..255: {epoch}")
        self.events = events or EventBus()
        self.clock = clock
        self._lock = threading.Lock()
        # The state file (``state_file``): snapshots are taken under ``_lock``, written outside it.
        self._store = None if state_file is None else StateStore(state_file, clock, kept_ids=KEPT_IDS_MAX, kept_queries=QUERY_IDS_KEPT)
        self._id_ledger = IdLedger(
            keep_limit=KEPT_IDS_MAX,
            dedup_window=DEDUP_WINDOW,
            initial_sequence=initial_sequence,
            rng=rng,
        )
        # [MEM] per-node stop-and-wait queues | 端: controller | 上限: max_queue(默认 32) × 节点数 | 每项: 1 帧(≤7 B) + 重传状态 + Future
        self._queues: dict[int, deque[_Pending]] = {}
        # [MEM] reports and module events already published, per reporter, least recently heard dropped first (retries are only acknowledged; one counter on the device) | 端: controller | 上限: PUBLISHED_KEPT(32) × 节点数 | 每项: 2 B message_id
        self._reports: dict[int, OrderedDict[int, None]] = {}
        # [MEM] confirming requests sent past with ``force`` | 端: controller | 上限: MAX_PAST_CONFIRMING × 节点数 | 每项: 1 帧 + 状态
        self._parked: dict[int, list[_Pending]] = {}
        self._heard_book = HeardBook()
        # [MEM] ACCEPTED requests awaiting their RESULT | 端: controller | 上限: 节点数 × 设备 max_open | 每项: 1 帧 + Future + 计时
        self._completions: dict[tuple[int, int], _Completion] = {}
        # Commands ended UNCONFIRMED: the ledger is the only one to read or write its records.
        self._late_ledger = LateLedger(keep_limit=KEPT_IDS_MAX)
        saved = None if self._store is None else self._store.load()
        self.state_warning = None if self._store is None else self._store.warning
        if epoch is not None:
            self.epoch = epoch
        elif saved is not None:
            self.epoch = (saved.epoch + 1) & 0xFF
        else:
            # Wall time, not ``clock``: the epoch must differ across processes.
            self.epoch = int(time.time()) & 0xFF
            if self._store is None:
                # Review D1: no state file is a choice, but not a safe one past the
                # devices' 7-day keep period; say so like a lost file does.
                self.state_warning = (
                    "没有配置控制端状态文件：EPOCH 由时钟推出（每 256 s 重复），也不记最近的消息编号。"
                    "设备记录保留 7 天，新命令可能撞上旧命令的编号，被设备当成重复帧、不执行却报已执行"
                    "（估算：40 个节点、每节点每次运行 20 条命令时，每次重启约 5 % 的概率出现一次）。"
                    "请配置 state_file"
                )
        if saved is not None:
            self._restore(saved)
        if self._store is not None:
            self._store.write(self._snapshot())
        transport.set_receiver(self.on_frame)

    # ------------------------------------------------------------ frame build
    #: The ledger's table of ended commands (node → message id → record), a read view for tests; only the ledger writes it.
    _late = property(lambda self: self._late_ledger.records_by_node)
    #: The state file path (``None`` without one), derived from ``_store``.
    state_file = property(lambda self: None if self._store is None else self._store.path)

    def build_request(self, action: int, value: int, target_id: int) -> RequestFrame:
        """Create a request once; the same object is reused for retries."""

        _require_unicast(target_id)
        if action == QUERY_ACTION:
            raise ValueError(f"action 0x{QUERY_ACTION:02X} is reserved for status queries")
        # Build with a placeholder first: an invalid field must not burn a
        # sequence number.
        frame = RequestFrame(
            sender_id=self.sender_id,
            target_id=target_id,
            sequence=0,
            epoch=self.epoch,
            action=action,
            value=value,
        )
        frame.sequence = self._take_sequence(target_id)
        return frame

    def build_wake(self, target_id: int) -> WakeFrame:
        _require_unicast(target_id)
        return WakeFrame(
            sender_id=self.sender_id,
            target_id=target_id,
            sequence=self._take_sequence(target_id),
            epoch=self.epoch,
        )

    def allocate_id(self, target_id: int) -> tuple[int, int]:
        """A fresh ``(sequence, epoch)`` for a frame sent below the controller.

        The gateway's probes and keepalives draw on the same per-node counter,
        so their ids never collide with a request in flight to that node.
        """

        return self._take_sequence(target_id), self.epoch

    def _take_sequence(self, target_id: int) -> int:
        _require_unicast(target_id)
        with self._lock:
            return self._take_sequence_locked(target_id)

    def _take_sequence_locked(self, target_id: int, epoch: Optional[int] = None) -> int:
        if epoch is None:
            used = self._protected_ids(target_id, self.epoch)
            if len(used) >= KEPT_IDS_LIMIT:
                # Too many ids the node may still keep in this EPOCH: move
                # on. The next REQUEST's first send writes the new EPOCH to the state
                # file before it goes; a restart then takes the one after it.
                self.epoch = (self.epoch + 1) & 0xFF
                self.epoch_moves += 1
                used = self._protected_ids(target_id, self.epoch)
            epoch = self.epoch
        else:
            used = self._protected_ids(target_id, epoch)
        return self._id_ledger.take_sequence(target_id, epoch, used)

    def _protected_ids(self, target_id: int, epoch: int, answers_only: bool = False) -> set[int]:
        """Message ids (EPOCH | SEQ, this ``epoch``) an answer from ``target_id`` may still settle something by (lock held).

        The REQUESTs the device may still keep (``IdLedger.request_ids``); every frame still waiting on
        the node, queued ones too (their id was taken when they were built)
        and the parked ones: its own id, the frame on the air, its last query
        ids; and the ACCEPTED requests whose RESULT is due. ``answers_only``
        (review B1): ids of REQUESTs and queries only, not of a WAKE (whose
        answer has no application layer).
        """

        mark = epoch << 8
        used = self._id_ledger.protected_ids(target_id, epoch)
        for item in [*self._queues.get(target_id, ()), *self._parked.get(target_id, ())]:
            for frame in (item.original, item.frame):
                if frame.epoch == epoch and (not answers_only or isinstance(frame, RequestFrame)):
                    used.add(mark | frame.sequence)
            if item.asked is not None and item.asked.epoch == epoch:
                used.update(mark | query for query in item.query_sequences)
        used.update(mid for node, mid in self._completions if node == target_id and mid >> 8 == epoch)
        # Commands ended UNCONFIRMED, see ``LateLedger.protected_ids``.
        used.update(self._late_ledger.protected_ids(target_id, epoch))
        return used

    # ------------------------------------------------------------- public API

    def request(
        self,
        target_id: int,
        action: int,
        value: int,
        *,
        timeout: Optional[float] = None,
        max_retries: Optional[int] = None,
        force: bool = False,
        important: bool = False,
    ) -> "Future[RequestOutcome]":
        """Queue a REQUEST. ``important`` (default no): confirm it on to the keep period, not only ``confirm_limit``."""

        self._check_window(
            self.timeout if timeout is None else timeout,
            self.max_retries if max_retries is None else max_retries,
        )
        if self._volatile:
            allowed = VOLATILE_EPOCH_ALLOWED if self.allow_volatile_epoch is None else self.allow_volatile_epoch
            if not allowed:
                raise NoStateFile(
                    "没有控制端状态文件，也没有指定 EPOCH：新命令可能撞上设备还记着的旧命令编号，"
                    "被当成重复帧、不执行却报已执行。请配置 state_file（演示、测试可传 allow_volatile_epoch=True）"
                )
        self._check_confirming(target_id, force)
        frame = self.build_request(action, value, target_id)
        return self.submit(frame, timeout=timeout, max_retries=max_retries, force=force, important=important)

    def wake(
        self,
        target_id: int,
        *,
        timeout: Optional[float] = None,
        max_retries: Optional[int] = None,
    ) -> "Future[RequestOutcome]":
        self._check_window(
            self.timeout if timeout is None else timeout,
            self.wake_max_retries if max_retries is None else max_retries,
        )
        frame = self.build_wake(target_id)
        return self.submit(frame, timeout=timeout, max_retries=max_retries)

    def submit(
        self,
        frame: OutgoingFrame,
        *,
        timeout: Optional[float] = None,
        max_retries: Optional[int] = None,
        force: bool = False,
        important: bool = False,
    ) -> "Future[RequestOutcome]":
        """Queue a frame for reliable delivery to ``frame.target_id``.

        ``timeout`` and ``max_retries`` override the controller defaults for
        this frame only (a WAKE defaults to ``wake_max_retries``). Raises
        ``QueueFull`` when the node already has ``max_queue`` frames waiting,
        and ``NodeConfirming`` for a REQUEST while one to the node is still
        being confirmed, unless ``force`` (then at most
        ``MAX_PAST_CONFIRMING`` past it).
        """

        _require_unicast(frame.target_id)
        if max_retries is None:
            max_retries = (
                self.wake_max_retries
                if isinstance(frame, WakeFrame)
                else self.max_retries
            )
        frame_timeout = self.timeout if timeout is None else timeout
        self._check_window(frame_timeout, max_retries)
        pending = _Pending(
            frame=frame,
            timeout=frame_timeout,
            max_retries=max_retries,
            important=bool(important),
        )
        effects = _SideEffects()
        target = frame.target_id
        with self._lock:
            queue = self._queues.get(target)
            if queue is not None and len(queue) >= self.max_queue:
                raise QueueFull(
                    f"node 0x{target:02X} already has {len(queue)} frames queued"
                )
            jump = False
            if isinstance(frame, RequestFrame):
                self._check_confirming_locked(target, self._confirming_locked(target), force)
                # ``passed`` counts in ``_transmit``: what goes on the air past
                # it, the requests that were queued behind it too (review A3).
                if queue and queue[0].confirming:
                    # Parked: asked about again once the queue is empty.
                    self._parked.setdefault(target, []).append(queue.popleft())
                    if queue:
                        queue[0].deadline = self.clock()  # its turn now
            elif queue and queue[0].confirming:
                # A WAKE goes ahead of a confirming request (it is not cached:
                # passes nothing) and hands the head back when it is over;
                # the requests queued behind stay where they are (review D3).
                pending.jumped = queue.popleft()
                self._parked.setdefault(target, []).append(pending.jumped)
                jump = True
            queue = self._queues.setdefault(target, deque())
            if jump:
                queue.appendleft(pending)
            else:
                queue.append(pending)
            is_first_in_line = queue[0] is pending
            if is_first_in_line:
                self._transmit(pending, self.clock(), effects)
            if isinstance(frame, RequestFrame) and self._parked.get(target):
                effects.state = self._snapshot()
        self._apply(effects)
        return pending.future

    def _confirming_locked(self, target: int) -> list[_Pending]:
        queue = self._queues.get(target)
        # Only past ``confirm_window``: during the first queries a new
        # request just waits its turn, as it always did.
        head = [queue[0]] if queue and queue[0].confirming else []
        return head + self._parked.get(target, [])

    def _check_confirming(self, target: int, force: bool) -> None:
        # Before an id is taken: a refused request must not burn one.
        with self._lock:
            self._check_confirming_locked(target, self._confirming_locked(target), force)

    def _check_confirming_locked(self, target: int, waiting: list[_Pending], force: bool) -> None:
        if not waiting:
            return
        sequences = "、".join(str(item.original.sequence) for item in waiting)
        if not force:
            raise NodeConfirming(
                f"节点 0x{target:02X} 的命令 SEQ {sequences} 还在确认中；force 可越过（旧命令继续确认）"
            )
        # Requests queued behind it, not sent yet, go past it too once it is parked (review A3).
        ahead = self._unsent_requests(target)
        if any(item.passed + ahead + 1 > MAX_PAST_CONFIRMING for item in waiting):
            raise NodeConfirming(
                f"节点 0x{target:02X}：已越过 {max(item.passed for item in waiting)} 条、排队待发 {ahead} 条，"
                f"再发会挤掉设备里 SEQ {sequences} 的记录；先等结论或放弃确认"
            )

    def _check_window(self, timeout: float, max_retries: int) -> None:
        if not timeout > 0:
            raise ValueError(f"timeout must be positive, got {timeout!r}")
        if max_retries < 0:
            raise ValueError(f"max_retries must not be negative, got {max_retries!r}")
        if self.max_retry_window is None:
            return
        window = (max_retries + 1) * timeout
        if window > self.max_retry_window:
            raise ValueError(
                f"retry window {window:g} s ((max_retries + 1) * timeout) exceeds "
                f"max_retry_window {self.max_retry_window:g} s: a device may have "
                "forgotten the request by the last retry and run it again"
            )

    def pending_count(self, target_id: int) -> int:
        """Frames queued or in flight for ``target_id``."""

        with self._lock:
            return len(self._queues.get(target_id, ()))

    def awaits_request_answer(self, node_id: int, message_id: int) -> bool:
        """Would a REPLY with ``message_id`` from ``node_id`` answer one of our REQUESTs or queries?

        Such a REPLY must carry the application layer (2026-10-21 review B1):
        the secure layer asks this for a plain one (``SecureTransport``).
        """

        with self._lock:
            return message_id in self._protected_ids(node_id, message_id >> 8, answers_only=True)

    def awaiting(self, target_id: int, message_id: int) -> bool:
        """Whether the frame ``message_id`` to ``target_id`` is still on the air.

        A gateway asks this before sending a copy it held back (a sleeping
        network, a queued flood): a command already answered or given up
        on must never go out again, or a device that has forgotten it runs
        it a second time.
        """

        with self._lock:
            queue = self._queues.get(target_id)
            return bool(queue) and queue[0].attempts > 0 and queue[0].frame.message_id == message_id

    # ------------------------------------------------------------- layer 3

    def nudge(self, node_id: int) -> None:
        """The gateway heard ``node_id`` (any frame): a confirming request to it is asked about now."""

        with self._lock:
            now = self.clock()
            self._heard_book.note(node_id, now)
            queue = self._queues.get(node_id)
            if not queue:
                return
            active = queue[0]
            if active.confirming and active.round_sent == 0:
                active.deadline = self.confirm.nudged(active.deadline, now, active.last_round)

    def nudge_all(self, fresh: bool = True) -> None:
        """Power came back somewhere (a JOIN heard) or a scan just mapped the network: ask about every confirming request.

        ``fresh``: for ``CONFIRM_HEARD_FRESH`` a round that gets no answer
        is followed after the fast interval (the node is likely up again).
        """

        with self._lock:
            now = self.clock()
            if fresh:
                self._heard_book.note_area(now)
            for queue in self._queues.values():
                active = queue[0]
                if active.confirming and active.round_sent == 0:
                    active.deadline = self.confirm.nudged(active.deadline, now, active.last_round)

    def confirming(self) -> list[Confirming]:
        """Requests whose outcome is still being confirmed, in node order."""

        with self._lock:
            now = self.clock()
            found = []
            for node_id in sorted(set(self._queues) | set(self._parked)):
                queue = self._queues.get(node_id)
                head = [queue[0]] if queue and queue[0].confirming else []
                for item in head + self._parked.get(node_id, []):
                    original = item.original
                    found.append(
                        Confirming(
                            node_id=node_id,
                            sequence=original.sequence,
                            epoch=original.epoch,
                            action=original.action,
                            value=original.value,
                            first_sent=item.first_sent,
                            last_heard=self._last_heard(node_id, item),
                            rounds=item.rounds,
                            next_query=item.deadline if item in head and item.round_sent == 0 else None,
                            important=item.important,
                            limit_in=self._limit_in(item, now),
                        )
                    )
            return found

    def abandon(self, node_id: int, sequence: Optional[int] = None) -> int:
        """The operator gives up confirming ``node_id``'s request(s): resolved ``UNCONFIRMED``.

        An explicit operator action. (The controller itself gives up only on an
        ordinary command at its confirm limit, or at the devices' keep period.)
        ``sequence`` picks one request, else all of the node's. Returns how many
        were abandoned. Their ids stay protected like those of a command that
        reached its limit, and a late positive result is published (``LateConfirmed``).
        """

        effects = _SideEffects()
        with self._lock:
            now = self.clock()
            reason = "操作员放弃（结论不确定）"
            parked = self._parked.get(node_id, [])
            for item in [item for item in parked if sequence in (None, item.original.sequence)]:
                parked.remove(item)
                self._watch_late(item)
                self._fail(item, reason, now, effects, abandoned=True)
                effects.resolved.append((item.future, self._outcome(item, None, now, reason)))
            if not parked:
                self._parked.pop(node_id, None)
            queue = self._queues.get(node_id)
            head = queue[0] if queue else None
            if head is not None and head.asked is not None and not head.completion_check and sequence in (None, head.original.sequence):
                self._watch_late(queue[0])
                self._fail(queue[0], reason, now, effects, abandoned=True)
                self._finish(queue[0], None, now, effects, reason)
            count = len(effects.resolved)
            if count:
                effects.state = self._snapshot()
        self._apply(effects)
        return count

    # ---------------------------------------------------------------- driving

    def on_frame(self, data: bytes) -> None:
        """Transport callback: handle one incoming frame."""

        try:
            frame = decode_frame(data)
        except ValueError as exc:
            self.events.publish(FrameRejected(raw=bytes(data), reason=str(exc)))
            return

        if isinstance(frame, HeartbeatFrame):
            self._on_heartbeat(frame)
        elif isinstance(frame, ReplyFrame):
            self._on_reply(frame)
        elif isinstance(frame, ReportFrame):
            self._on_report(frame)
        elif isinstance(frame, ResultFrame):
            self._on_result(frame)
        elif isinstance(frame, EventFrame):
            self._on_event(frame)
        # WAKE / REQUEST frames are controller-to-device only; ignore them.

    def tick(self, now: Optional[float] = None) -> None:
        """Retransmit or give up on frames whose timeout expired."""

        now = self.clock() if now is None else now
        effects = _SideEffects()
        with self._lock:
            if self._store is not None and self._store.failed:
                effects.state = self._snapshot()  # try again; REQUESTs wait for it
            effects.events.extend(self._late_ledger.replay())
            keep_horizon = self.confirm.keep_horizon()
            if keep_horizon is not None:
                forgotten = self._late_ledger.expire(now, keep_horizon)
                if forgotten and self._store is not None:
                    effects.state = self._snapshot()
            # Ordinary commands put aside by ``force`` have their limit too (a hard point, whether or not they are on the air).
            for node in list(self._parked):
                for item in list(self._parked.get(node, ())):
                    limit = self._limit_at(item)
                    if limit is not None and now >= limit and item.asked_since_start:
                        self._expire(item, now, effects)
            # Copy: _finish() may delete an emptied queue while we iterate.
            for queue in list(self._queues.values()):
                active = queue[0]
                if self._check_limit(active, now, effects):
                    continue
                if now < active.deadline:
                    continue
                if active.completion_check:
                    if active.attempts <= active.max_retries:
                        self._transmit(active, now, effects)
                    else:
                        self._finish(active, None, now, effects)  # quietly: the next check is due later
                    continue
                if active.asked is None and active.attempts <= active.max_retries:
                    self._transmit(active, now, effects)
                    continue
                if active.confirming:
                    self._confirm_round(active, now, effects)
                    continue
                confirming = self.confirm.in_query_window(active.first_sent, now)
                if confirming and active.asked is not None:
                    self._transmit(active, now, effects)  # the query again
                    continue
                if confirming and isinstance(active.frame, RequestFrame):
                    self._start_query(active, now, effects)
                    continue
                if self.confirm.enters_confirming(isinstance(active.original, RequestFrame)):
                    self._enter_confirming(active, now, effects)
                    continue
                self._fail(
                    active,
                    "未确认:请求与查询都没有回答" if active.asked is not None else "重试次数耗尽",
                    now,
                    effects,
                )
                self._finish(active, None, now, effects)
            self._check_completions(now, effects)
        self._apply(effects)

    # ------------------------------------------------------- incoming frames

    def _on_heartbeat(self, frame: HeartbeatFrame) -> None:
        if frame.target_id not in (self.sender_id, BROADCAST_ID):
            return
        self.events.publish(
            HeartbeatReceived(
                node_id=frame.sender_id,
                sequence=frame.sequence,
                state=frame.state,
            )
        )

    def _on_report(self, frame: ReportFrame) -> None:
        self._acknowledge(
            frame, lambda: NodeReported(node_id=frame.subject, reporter=frame.sender_id, event=frame.event)
        )

    def _on_event(self, frame: EventFrame) -> None:
        self._acknowledge(frame, lambda: NodeEvent(node_id=frame.sender_id, event_id=frame.event_id))

    def _acknowledge(self, frame: Union[ReportFrame, EventFrame], event: Callable[[], LinkEvent]) -> None:
        """A REPORT or EVENT: acknowledge every copy, publish the first (the device's message id names it)."""

        if frame.target_id != self.sender_id:
            return
        with self._lock:
            # Per reporter, refreshed by every copy: a message still being
            # retried stays known however many others the network sends
            # (measured: one table of 32 for everyone published 31-44 events
            # twice when 40 nodes flushed two each on a wake).
            seen = self._reports.setdefault(frame.sender_id, OrderedDict())
            fresh = frame.message_id not in seen
            seen[frame.message_id] = None
            seen.move_to_end(frame.message_id)
            if len(seen) > PUBLISHED_KEPT:
                seen.popitem(last=False)
        # Every copy is acknowledged: the reporter retries until one ack
        # gets through, and a retry means the previous ack was lost.
        ack = ReplyFrame(
            sender_id=self.sender_id,
            target_id=frame.sender_id,
            sequence=frame.sequence,
            epoch=frame.epoch,
            result=ReplyResult.OK,
        )
        effects = _SideEffects(sends=[ack.encode()])
        if fresh:
            effects.events.append(event())
        self._apply(effects)

    def _on_reply(self, frame: ReplyFrame) -> None:
        if frame.target_id != self.sender_id:
            return

        effects = _SideEffects()
        with self._lock:
            now = self.clock()
            node = frame.sender_id
            queue = self._queues.get(node)
            active = queue[0] if queue else None
            parked = next((item for item in self._parked.get(node, ()) if self._answers(item, frame)), None)
            if active is not None and self._answers(active, frame):
                self._resolve(active, frame.result, now, effects)
                self._finish(active, frame.result, now, effects)
            elif parked is not None:
                # A parked (confirming) request's late answer: as good as its next round's (A5).
                self._resolve(parked, frame.result, now, effects)
                self._settle_parked(parked, frame.result, now, effects)
            elif frame.result != ReplyResult.ACCEPTED and (node, frame.message_id) in self._completions:
                # The final answer of an ACCEPTED request (a query met its short action mid-run, A6).
                if _is_unknown_reply_result(frame.result):
                    self._id_ledger.settle(node, frame.message_id)
                result = None if frame.result == ReplyResult.NOT_RUN else frame.result
                self._complete((node, frame.message_id), result, now, effects, queried=False)
            elif (late := self._late_ledger.match(node, frame.epoch, frame.sequence)) is not None:
                self._late_answer(late, frame.result, "reply", frame.sequence, now, effects)
            elif active is None:
                effects.events.append(self._ignored(frame, "没有在途请求"))
            elif active.frame.epoch != frame.epoch:
                effects.events.append(self._ignored(frame, "会话代不匹配(重启前的回复)"))
            else:
                effects.events.append(self._ignored(frame, "序号不匹配(过期或重复回复)"))
        self._apply(effects)

    @staticmethod
    def _answers(pending: _Pending, frame: ReplyFrame) -> bool:
        """Does ``frame`` answer ``pending``: its own id, or (during a query) the request's or one of its last queries'?"""

        if pending.frame.epoch != frame.epoch:
            return False
        if frame.sequence in (pending.frame.sequence, pending.original.sequence):
            return True
        return pending.asked is not None and frame.sequence in pending.query_sequences

    def _resolve(self, pending: _Pending, result: int, now: float, effects: _SideEffects) -> None:
        """The events for a waiting frame's answer (lock held); the caller retires it."""

        original = pending.original
        if result == ReplyResult.ACCEPTED and isinstance(original, RequestFrame):
            pending.completion = self._track_completion(original, now)
        elif isinstance(original, RequestFrame):
            self._id_ledger.settle(original.target_id, original.message_id)
        if pending.completion_check:
            self._completion_answer(pending, result, now, effects)
        elif result == ReplyResult.NOT_RUN and pending.asked is not None:
            effects.events.append(
                RequestFailed(
                    node_id=original.target_id,
                    sequence=original.sequence,
                    kind=original.kind,
                    attempts=pending.attempts,
                    reason="确认未执行(查询)",
                    status=NOT_RUN,
                    epoch=original.epoch,
                )
            )
        else:
            effects.events.append(
                ReplyReceived(
                    node_id=original.target_id,
                    sequence=original.sequence,
                    kind=original.kind,
                    result=result,
                    attempts=pending.attempts,
                    rtt=now - pending.first_sent,
                    queried=pending.asked is not None,
                )
            )
            if _is_unknown_reply_result(result):
                effects.events.append(
                    ReplyIgnored(
                        node_id=original.target_id,
                        sequence=original.sequence,
                        reason=_unknown_reply_result_reason(result),
                    )
                )

    def _settle_parked(self, pending: _Pending, result: Optional[int], now: float, effects: _SideEffects) -> None:
        """A parked (confirming) request got its outcome while others went ahead of it (review A5)."""

        target = pending.original.target_id
        parked = self._parked[target]
        parked.remove(pending)
        if not parked:
            del self._parked[target]
        effects.resolved.append((pending.future, self._outcome(pending, result, now, "")))
        if self._store is not None:
            effects.state = self._snapshot()

    @staticmethod
    def _ignored(frame: ReplyFrame, reason: str) -> ReplyIgnored:
        return ReplyIgnored(node_id=frame.sender_id, sequence=frame.sequence, reason=reason)

    # ------------------------------------------- queue handling (lock held)

    # --------------------------------------------- ACCEPTED, RESULT (lock held)

    def _track_completion(self, frame: RequestFrame, now: float) -> Future:
        key = (frame.target_id, frame.message_id)
        known = self._completions.get(key)
        if known is not None:  # a late ACCEPTED again (a query, a repeat)
            return known.future
        future = _uncancellable_future()
        self._completions[key] = _Completion(frame, future, now, now + COMPLETION_CHECK, COMPLETION_CHECK)
        return future

    def _complete(self, key: tuple[int, int], result: Optional[int], now: float, effects: _SideEffects, queried: bool) -> None:
        completion = self._completions.pop(key, None)
        if completion is None:
            return
        frame = completion.frame
        unknown = _is_unknown_reply_result(result)
        if unknown:
            effects.events.append(
                ReplyIgnored(
                    node_id=frame.target_id,
                    sequence=frame.sequence,
                    reason=_unknown_reply_result_reason(result),
                )
            )
            result = None
        effects.events.append(
            RequestCompleted(
                node_id=frame.target_id,
                sequence=frame.sequence,
                epoch=frame.epoch,
                result=result,
                elapsed=now - completion.accepted_at,
                queried=queried,
            )
        )
        effects.resolved.append((completion.future, result))

    def _completion_answer(self, pending: _Pending, result: int, now: float, effects: _SideEffects) -> None:
        if result == ReplyResult.ACCEPTED:
            return  # still running: asked again later
        key = (pending.original.target_id, pending.original.message_id)
        # NOT_RUN here means the record is gone (keep period): the result can no longer be learnt.
        self._complete(key, None if result == ReplyResult.NOT_RUN else result, now, effects, queried=True)

    def _check_completions(self, now: float, effects: _SideEffects) -> None:
        """Ask about ACCEPTED requests whose RESULT is late, each once its node's queue is free."""

        for key, completion in list(self._completions.items()):
            node = key[0]
            if now < completion.next_check or self._queues.get(node) or self._parked.get(node):
                continue
            if self.confirm.past_keep(now - completion.accepted_at):
                self._id_ledger.settle(*key)  # its record is past the keep period
                self._complete(key, None, now, effects, queried=True)
                continue
            completion.next_check = now + completion.interval
            completion.interval = min(2 * completion.interval, self.confirm.slow_interval())
            timeout, retries = (
                self.timeouts_for(node) if self.timeouts_for is not None else (self.timeout, self.max_retries)
            )
            pending = _Pending(
                frame=completion.frame, timeout=timeout, max_retries=retries, asked=completion.frame, completion_check=True
            )
            self._new_query(pending)
            self._queues[node] = deque([pending])
            self._transmit(pending, now, effects)

    def _on_result(self, frame: ResultFrame) -> None:
        """A RESULT: acknowledge every copy (a repeat means the last acknowledgement was lost); the first settles."""

        if frame.target_id != self.sender_id:
            return
        ack = ReplyFrame(
            sender_id=self.sender_id,
            target_id=frame.sender_id,
            sequence=frame.sequence,
            epoch=frame.epoch,
            result=ReplyResult.OK,
        )
        effects = _SideEffects(sends=[ack.encode()])
        with self._lock:
            now = self.clock()
            self._id_ledger.settle(frame.sender_id, frame.message_id)  # acknowledged: the record closes
            queue = self._queues.get(frame.sender_id)
            active = queue[0] if queue else None
            if (
                active is not None
                and not active.completion_check
                and active.original.epoch == frame.epoch
                and active.original.sequence == frame.sequence
                and isinstance(active.original, RequestFrame)
            ):
                # Its ACCEPTED was lost and the action is already over: as good as the answer.
                original = active.original
                effects.events.append(
                    ReplyReceived(
                        node_id=frame.sender_id,
                        sequence=original.sequence,
                        kind=original.kind,
                        result=frame.result,
                        attempts=active.attempts,
                        rtt=now - active.first_sent,
                        queried=active.asked is not None,
                    )
                )
                if _is_unknown_reply_result(frame.result):
                    effects.events.append(
                        ReplyIgnored(
                            node_id=original.target_id,
                            sequence=original.sequence,
                            reason=_unknown_reply_result_reason(frame.result),
                        )
                    )
                self._finish(active, frame.result, now, effects)
            else:
                parked = next(
                    (
                        item
                        for item in self._parked.get(frame.sender_id, ())
                        if item.original.epoch == frame.epoch and item.original.sequence == frame.sequence
                    ),
                    None,
                )
                late = self._late_ledger.get(frame.sender_id, frame.message_id)
                if parked is not None:  # a parked request's RESULT settles it (review A5)
                    self._resolve(parked, frame.result, now, effects)
                    self._settle_parked(parked, frame.result, now, effects)
                elif late is not None:  # its command ended UNCONFIRMED: published, the Future stays as it was
                    self._late_answer(late, frame.result, "result", frame.sequence, now, effects)
                else:
                    self._complete((frame.sender_id, frame.message_id), frame.result, now, effects, queried=False)
        self._apply(effects)

    def _start_query(self, pending: _Pending, now: float, effects: _SideEffects) -> None:
        asked = pending.frame
        pending.asked = asked
        self._new_query(pending)
        effects.events.append(
            RequestQuerying(
                node_id=asked.target_id,
                sequence=asked.sequence,
                query_sequence=pending.frame.sequence,
                attempts=pending.attempts,
                epoch=asked.epoch,
            )
        )
        self._transmit(pending, now, effects)

    def _new_query(self, pending: _Pending) -> None:
        asked = pending.asked
        pending.frame = RequestFrame(
            sender_id=self.sender_id,
            target_id=asked.target_id,
            sequence=self._take_sequence_locked(asked.target_id, epoch=asked.epoch),
            epoch=asked.epoch,
            action=QUERY_ACTION,
            value=asked.sequence,
        )
        pending.query_sequences.append(pending.frame.sequence)

    def _enter_confirming(self, pending: _Pending, now: float, effects: _SideEffects) -> None:
        if pending.asked is None:  # retries ran past the window: ask from now on
            pending.asked = pending.frame
            self._new_query(pending)
            pending.query_unsent = True  # the first round sends this one
        pending.confirming = True
        pending.round_sent = 0
        pending.last_round = now
        pending.deadline = self._next_round(pending, now)
        effects.events.append(self._confirming_event(pending, now))
        self._drop_unsent(pending.original.target_id, now, effects)
        effects.state = self._snapshot()

    def _drop_unsent(self, target: int, now: float, effects: _SideEffects) -> None:
        """Frames queued behind a request that went confirming end ``NOT_SENT`` (review A3b, user's decision).

        They would wait as long as it confirms (days, maybe), with no outcome
        meanwhile. Never sent, they certainly did not run: the operator sends
        them again (``force`` past the confirming one, or once it has its outcome).
        """

        queue = self._queues[target]
        for item in [item for item in list(queue)[1:] if item.attempts == 0]:
            queue.remove(item)
            frame = item.frame
            event, outcome = not_sent_pair(frame)
            effects.events.append(event)
            effects.resolved.append((item.future, outcome))

    def past_allowance(self, target_id: int) -> Optional[tuple[list[int], int]]:
        """``(SEQs confirming on the node, requests that may still go past them)``, or ``None`` if none is confirming."""

        with self._lock:
            waiting = self._confirming_locked(target_id)
            if not waiting:
                return None
            ahead = self._unsent_requests(target_id)
            room = MAX_PAST_CONFIRMING - max(item.passed for item in waiting) - ahead
            return [item.original.sequence for item in waiting], max(0, room)

    def _unsent_requests(self, target: int) -> int:
        return sum(
            1
            for item in self._queues.get(target, ())
            if item.attempts == 0 and isinstance(item.frame, RequestFrame) and item.frame.action != QUERY_ACTION
        )

    def _confirm_round(self, pending: _Pending, now: float, effects: _SideEffects) -> None:
        """One step of CONFIRMING: start a round of queries, retry it, or schedule the next."""

        age = now - pending.first_sent
        if self.confirm.past_keep(age):
            reason = "超过设备记忆期限，需现场核查"
            self._fail(pending, reason, now, effects)
            self._finish(pending, None, now, effects, reason)
            effects.state = self._snapshot()
            return
        if pending.round_sent > pending.max_retries:  # the round got no answer
            if pending.final:  # the last round, asked past the confirm limit: that is all
                self._expire(pending, now, effects)
                return
            pending.round_sent = 0
            pending.deadline = self._next_round(pending, now)
            effects.events.append(self._confirming_event(pending, now))
            return
        if pending.round_sent == 0:
            if not pending.query_unsent:
                self._new_query(pending)  # a fresh id: copies of the last round may still be queued
            pending.query_unsent = False
            pending.rounds += 1
            pending.last_round = now
            if self.timeouts_for is not None:
                pending.timeout, pending.max_retries = self.timeouts_for(pending.original.target_id)
        pending.round_sent += 1
        self._transmit(pending, now, effects)

    def _next_round(self, pending: _Pending, now: float) -> float:
        age = now - pending.first_sent
        quiet = (
            self.confirm.quiet_after is not None
            and age >= self.confirm.quiet_after
            and self.round_wakes is not None
            and self.round_wakes(pending.original.target_id)
        )
        return self.confirm.next_round_due(
            first_sent=pending.first_sent,
            last_round=pending.last_round,
            now=now,
            fresh=self._heard_book.fresh(pending.original.target_id, pending.first_sent, now),
            quiet=quiet,
            limit=self._limit_at(pending),
        )

    # ------------------------------------- confirm limit, late results

    def _limit_at(self, pending: _Pending) -> Optional[float]:
        """When an ordinary confirming command ends (a hard point on the clock), else ``None``."""

        return self.confirm.limit_at(
            pending.first_sent,
            important=pending.important,
            confirming=pending.confirming,
        )

    def _limit_in(self, pending: _Pending, now: float) -> Optional[float]:
        return self.confirm.limit_in(self._limit_at(pending), now)

    def _fresh(self, pending: _Pending, now: float) -> bool:
        """Was the node (or the area it is in) heard in the last ``CONFIRM_HEARD_FRESH``?"""

        return self._heard_book.fresh(pending.original.target_id, pending.first_sent, now)

    def _check_limit(self, pending: _Pending, now: float, effects: _SideEffects) -> bool:
        """The head of a node's queue at its ordinary confirm limit (``True``: dealt with, leave it for this tick).

        At the limit: a command whose node was heard in the last ``CONFIRM_HEARD_FRESH`` (or never asked since this
        run started) gets one last round of queries, which ends it when it fails; any other ends at once, a round
        in progress cut off. The point does not move with the quiet rounds (``_next_round`` only schedules up to it).
        """

        limit = self._limit_at(pending)
        if limit is None or pending.final or now < limit:
            return False
        if not pending.asked_since_start or self._fresh(pending, now):
            pending.final = True
            if pending.round_sent == 0:
                self._confirm_round(pending, now, effects)
                return True
            return False  # a round is on the air: it is the last one
        self._expire(pending, now, effects)
        return True

    def _expire(self, pending: _Pending, now: float, effects: _SideEffects) -> None:
        """An ordinary command reached its confirm limit: ``UNCONFIRMED``, queue released, nothing more asked (lock held).

        Like the operator's ``abandon`` this calls ``_fail`` and never ``IdLedger.settle``: its REQUEST id stays "not
        settled" in the state file, and its last query ids stay protected (``_watch_late``).
        """

        reason = f"{UNCONFIRMED_NOTE}（普通指令确认 {self.confirm.limit / 60:g} min 到点）"
        self._watch_late(pending)
        self._fail(pending, reason, now, effects, expired=True)
        target = pending.original.target_id
        queue = self._queues.get(target)
        if queue and queue[0] is pending:
            self._finish(pending, None, now, effects, reason)
        else:  # put aside by ``force``
            parked = self._parked.get(target, [])
            if pending in parked:
                parked.remove(pending)
            if not parked:
                self._parked.pop(target, None)
            effects.resolved.append((pending.future, self._outcome(pending, None, now, reason)))
        effects.state = self._snapshot()

    def _watch_late(self, pending: _Pending) -> None:
        """A command ends ``UNCONFIRMED``: keep its ids protected and take a late positive result (lock held)."""

        if not pending.watchable:
            return
        original = pending.original
        self._late_ledger.watch(original, pending.first_sent, pending.query_sequences, pending.important)

    def _late_answer(
        self, late: LateRecord, result: int, via: str, sequence: int, now: float, effects: _SideEffects
    ) -> None:
        """A REPLY / RESULT for a command that ended ``UNCONFIRMED`` (lock held): the ledger decides, this closes the record."""

        why = self._late_ledger.take_answer(late, result, via, now)
        if why is not None:
            effects.events.append(ReplyIgnored(node_id=late.node, sequence=sequence, reason=why))
            return
        self._id_ledger.settle(late.node, late.epoch << 8 | late.sequence)  # a final answer: the device's record is closed
        effects.events.extend(self._late_ledger.unacknowledged(only=late))
        if self._store is not None:
            effects.state = self._snapshot()

    def late_confirmations(self) -> list[LateConfirmation]:
        """Late positive results nobody has acknowledged yet (they are published again after a restart)."""

        with self._lock:
            return [
                LateConfirmation(e.node_id, e.sequence, e.epoch, e.result, e.via, e.elapsed)
                for e in self._late_ledger.unacknowledged()
            ]

    def acknowledge_late(self, node_id: int, sequence: int, epoch: Optional[int] = None) -> bool:
        """The consumer took note of a late result: not published again after a restart. The ids stay protected."""

        effects = _SideEffects()
        with self._lock:
            found = self._late_ledger.acknowledge(node_id, sequence, epoch)
            if found:
                effects.state = self._snapshot()
        self._apply(effects)
        return found

    def _confirming_event(self, pending: _Pending, now: float) -> RequestConfirming:
        original = pending.original
        heard = self._last_heard(original.target_id, pending)
        return confirming_event(pending, now, heard, self._limit_in(pending, now))

    def _last_heard(self, node_id: int, pending: _Pending) -> Optional[float]:
        return self._heard_book.last_heard(node_id, pending.first_sent)

    def _fail(
        self,
        pending: _Pending,
        reason: str,
        now: float,
        effects: _SideEffects,
        abandoned: bool = False,
        expired: bool = False,
    ) -> None:
        effects.events.append(failed_event(pending, reason, abandoned=abandoned, expired=expired))

    def _transmit(self, pending: _Pending, now: float, effects: _SideEffects) -> None:
        pending.attempts += 1
        if pending.attempts == 1:
            pending.first_sent = now
        pending.deadline = now + pending.timeout

        frame = pending.frame
        is_request = isinstance(frame, RequestFrame)
        if pending.asked is not None and is_request and frame.action == QUERY_ACTION and not pending.completion_check:
            pending.asked_since_start = True
        if pending.attempts == 1 and is_request and frame.action != QUERY_ACTION:
            self._id_ledger.record_request(frame.target_id, frame.message_id)
            # Only the head is sent: whatever is confirming on the node is parked.
            for item in self._parked.get(frame.target_id, ()):
                item.passed += 1
            if self._store is not None:
                effects.state = self._snapshot()
        effects.sends.append(frame.encode())
        effects.events.append(
            RequestSent(
                node_id=frame.target_id,
                sequence=frame.sequence,
                kind=frame.kind,
                attempt=pending.attempts,
                max_attempts=pending.max_retries + 1,
                action=frame.action if is_request else None,
                value=frame.value if is_request else None,
            )
        )

    def _finish(
        self,
        pending: _Pending,
        result: Optional[int],
        now: float,
        effects: _SideEffects,
        reason: str = "",
    ) -> None:
        """Retire the active frame of a node and start the next one, if any."""

        target = pending.original.target_id
        queue = self._queues[target]
        queue.popleft()
        effects.resolved.append((pending.future, self._outcome(pending, result, now, reason)))
        if self._store is not None and isinstance(pending.original, RequestFrame):
            effects.state = True  # snapshot once the queue below is settled

        if pending.jumped is not None and pending.jumped in self._parked.get(target, ()):
            # A WAKE that went ahead of it: the confirming one has the head again.
            self._unpark(target, pending.jumped, now)
            queue.appendleft(pending.jumped)
        elif queue:
            self._transmit(queue[0], now, effects)
        elif self._parked.get(target):
            # The requests sent past it are done: ask about the parked one again.
            parked = self._parked[target][0]
            self._unpark(target, parked, now)
            queue.append(parked)
        else:
            del self._queues[target]
        if effects.state is True:
            effects.state = self._snapshot()

    def _unpark(self, target: int, parked: _Pending, now: float) -> None:
        """Take ``parked`` off the parked list: its next round of queries is due (it goes back on the queue)."""

        self._parked[target].remove(parked)
        if not self._parked[target]:
            del self._parked[target]
        parked.round_sent = 0
        parked.deadline = self.confirm.resume_due(now, parked.last_round)

    def _outcome(self, pending: _Pending, result: Optional[int], now: float, reason: str) -> RequestOutcome:
        return make_outcome(pending, result, now, reason)

    # ------------------------------------------------- state file (layer 3)

    def _snapshot(self) -> Optional[dict]:
        """The persistent state, taken under the lock (``None`` without ``state_file``)."""

        if self._store is None:
            return None
        confirming = []
        for node_id in sorted(set(self._queues) | set(self._parked)):
            queue = self._queues.get(node_id)
            # Every REQUEST on the air, not only confirming ones: after a crash
            # its outcome is asked about too (it may have run).
            head = [queue[0]] if queue and self._unresolved(queue[0]) else []
            for item in head + self._parked.get(node_id, []):
                original = item.original
                confirming.append(
                    PersistedRequest(
                        node=node_id,
                        epoch=original.epoch,
                        sequence=original.sequence,
                        action=original.action,
                        value=original.value,
                        first_sent=item.first_sent,
                        rounds=item.rounds,
                        passed=item.passed,
                        important=item.important,
                        queries=list(item.query_sequences),
                    )
                )
        return self._store.snapshot(
            PersistedState(
                epoch=self.epoch,
                request_ids=self._id_ledger.request_ids,
                closed_ids=self._id_ledger.closed_ids,
                next_sequence=self._id_ledger.next_sequence,
                confirming=confirming,
                late=self._late_ledger.records(),
            )
        )

    @staticmethod
    def _unresolved(pending: _Pending) -> bool:
        """A REQUEST that went out and has no outcome yet."""

        return pending.attempts > 0 and pending.watchable

    def _restore(self, saved: PersistedState) -> None:
        self._id_ledger.restore(saved)
        self._late_ledger.restore(saved.late)
        now = saved.restored_at
        for entry in saved.confirming:
            asked = RequestFrame(
                sender_id=self.sender_id,
                target_id=entry.node,
                sequence=entry.sequence,
                epoch=entry.epoch,
                action=entry.action,
                value=entry.value,
            )
            pending = _Pending(
                frame=asked,
                timeout=self.timeout,
                max_retries=self.max_retries,
                attempts=1,
                first_sent=entry.first_sent,
                deadline=now,
                asked=asked,
                confirming=True,
                rounds=entry.rounds,
                passed=entry.passed,
                important=entry.important,
            )
            pending.query_sequences.extend(entry.queries)
            queue = self._queues.setdefault(entry.node, deque())
            if queue:
                self._parked.setdefault(entry.node, []).append(pending)
            else:
                queue.append(pending)

    # ------------------------------------------------ side effects (unlocked)

    def _apply(self, effects: _SideEffects) -> None:
        # Events go out before the frames, so a transport that delivers the
        # reply synchronously cannot publish ReplyReceived before RequestSent.
        # A failing transport costs a frame, which the retry logic already
        # covers; it must never keep a future from resolving.
        try:
            held = False
            if isinstance(effects.state, dict):
                try:
                    self._store.write(effects.state)
                except OSError as exc:
                    # A REQUEST id not on disk could be handed out again after a
                    # restart: REQUESTs wait (their retry sends them) until a write
                    # succeeds; acknowledgements still go (review D2).
                    held = True
                    self.events.publish(LinkError(message=f"状态文件写入失败，命令暂缓发出: {exc}"))
            for event in effects.events:
                self.events.publish(event)
            for data in effects.sends:
                if held and data[0] == FrameKind.REQUEST:
                    continue
                try:
                    self.transport.send(data)
                except Exception as exc:  # noqa: BLE001 - report, retry will recover
                    self.events.publish(
                        LinkError(message=f"send failed: {type(exc).__name__}: {exc}")
                    )
        finally:
            for future, outcome in effects.resolved:
                future.set_result(outcome)


def _require_unicast(target_id: int) -> None:
    # A reliable frame waits for one reply from target_id; replies carry the
    # replying device's own id, so a broadcast frame could never be matched.
    if target_id == BROADCAST_ID:
        raise ValueError("cannot send a reliable frame to BROADCAST_ID")
