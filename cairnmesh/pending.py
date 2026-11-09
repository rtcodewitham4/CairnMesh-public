"""Leaf data types shared by the controller's queue and outcome builders.

This module deliberately has no dependency on ``controller``, transport,
locking, or the event bus. It only describes work queued by the controller.
"""

from __future__ import annotations

from collections import deque
from concurrent.futures import Future
from dataclasses import dataclass, field
from typing import Optional, Union

from .events import LinkEvent
from .frames import QUERY_ACTION, RequestFrame, WakeFrame

OutgoingFrame = Union[RequestFrame, WakeFrame]

#: Query ids per confirming request that a late answer is still taken from,
#: and that no other REQUEST to the node may take meanwhile (2026-10-21: a
#: request sent with ``force`` drew an old query id of the parked one off the
#: shared counter; a late copy of its answer then resolved the parked one,
#: never run, as DONE).
QUERY_IDS_KEPT = 4


def _uncancellable_future() -> Future:
    # The link cannot take a frame back once it is queued, so the caller must
    # not be able to cancel it either; a cancelled future would also make
    # set_result() raise in the middle of a batch.
    future: Future = Future()
    future.set_running_or_notify_cancel()
    return future


@dataclass
class Pending:
    """One frame waiting for (or in) its turn on a node's queue."""

    frame: OutgoingFrame
    timeout: float
    max_retries: int
    future: Future = field(default_factory=_uncancellable_future)
    attempts: int = 0
    first_sent: float = 0.0
    deadline: float = 0.0
    #: The request a status query is asking about (``frame`` is then the query).
    asked: Optional[RequestFrame] = None
    #: Layer 3: past ``confirm_window``, asked in rounds of queries.
    confirming: bool = False
    rounds: int = 0
    #: Sends of the current round (0: between rounds) and when it started.
    round_sent: int = 0
    last_round: float = 0.0
    #: The last query sequences sent about ``asked``: a late answer to any is
    #: as good. Only the last ``QUERY_IDS_KEPT``: no other REQUEST to the node
    #: may take these ids (``_take_sequence_locked``), and the per-node counter
    #: goes round in minutes; a late answer to an older one is ignored and the
    #: next round asks again.
    query_sequences: deque = field(default_factory=lambda: deque(maxlen=QUERY_IDS_KEPT))
    #: The current query was made (``_enter_confirming``) but not sent yet:
    #: the first round sends it instead of burning another id.
    query_unsent: bool = False
    #: A status query of ours about an ACCEPTED request whose RESULT is late:
    #: it publishes nothing of its own and gives up quietly.
    completion_check: bool = False
    #: Set on an ACCEPTED answer: ``RequestOutcome.completion``.
    completion: Optional[Future] = None
    #: Requests sent to the node while this one was parked (``force``).
    passed: int = 0
    #: A WAKE: the confirming request it went ahead of (review D3).
    jumped: Optional["Pending"] = None
    #: ``important`` (a REQUEST only): confirmed until the keep period or the operator; else until the confirm limit.
    important: bool = False
    #: Ordinary command past its confirm limit: the round in progress (or about to start) is the last one.
    final: bool = False
    #: A query of this run went out for it (a restored command must be asked once more before its limit is judged).
    asked_since_start: bool = False

    @property
    def original(self) -> OutgoingFrame:
        return self.frame if self.asked is None else self.asked

    @property
    def watchable(self) -> bool:
        """Can an unconfirmed outcome be watched for a late positive result?"""

        original = self.original
        return (
            not self.completion_check
            and isinstance(original, RequestFrame)
            and original.action != QUERY_ACTION
        )


@dataclass
class _Completion:
    """An ACCEPTED request whose RESULT has not come yet."""

    frame: RequestFrame
    future: Future
    accepted_at: float
    next_check: float
    interval: float


@dataclass
class _SideEffects:
    """Work decided under the lock and carried out after releasing it.

    Sending, publishing and resolving futures all call foreign code
    (transport, subscribers, future callbacks) that may call back into the
    controller, so none of it may run while the lock is held.
    """

    sends: list[bytes] = field(default_factory=list)
    events: list[LinkEvent] = field(default_factory=list)
    #: ``(future, value)``: a request's ``RequestOutcome``, or a completion's result.
    resolved: list[tuple[Future, object]] = field(default_factory=list)
    #: Snapshot of the persistent state to write (``state_file``).
    state: Optional[dict] = None
