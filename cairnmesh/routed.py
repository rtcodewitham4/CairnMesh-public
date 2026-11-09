"""Device side of routed sessions: a ``Transport`` between ``Device`` and the secure layer.

The device itself never sees a route. ``RouteEndpoint`` strips the route
trailer from a routed REQUEST or WAKE, hands the plain frame up and sends
the device's REPLY back along the same route walked backwards. It keeps
that route from the request until the reply, and for an ACCEPTED request
(``MAX_RESULT_ROUTES``) until its RESULT has gone back along it once. It answers a
KEEPALIVE itself (REPLY OK; it never reaches the duplicate cache). A request
that arrives by flood (the gateway's fallback) is answered by flood, even if
an earlier copy came routed: that route may be what just broke.

``protocol_spec.md`` section 11 has the frame formats.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from typing import Callable, Optional

from .frames import (
    FrameKind,
    KeepaliveFrame,
    ReplyFrame,
    ReplyResult,
    RequestFrame,
    WakeFrame,
    add_route,
    decode_routed,
    is_routed,
    reply_route,
)
from .transport import Receiver, Transport

#: ACCEPTED requests whose RESULT may still take their route back.
MAX_RESULT_ROUTES = 2

class RouteEndpoint:
    """Route trailers off on the way up, back on for the answer on the way down."""

    def __init__(
        self,
        transport: Transport,
        node_id: int,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._transport = transport
        self.node_id = node_id
        self.clock = clock
        self._receiver: Optional[Receiver] = None
        # [MEM] route of the request being answered | 端: device | 上限: 1 | 每项: 提问方 1 B + 消息 ID 2 B + ≤16 B 路由
        self._answer: Optional[tuple[int, int, tuple[int, ...]]] = None
        # [MEM] routes for RESULT frames still to come | 端: device | 上限: MAX_RESULT_ROUTES(2) | 每项: 提问方 1 B + 消息 ID 2 B + ≤16 B 路由
        #: An ACCEPTED request's route back: its RESULT goes that way first,
        #: once; a repeat floods (that route may be what lost it).
        self._result_routes: OrderedDict[tuple[int, int], tuple[int, ...]] = OrderedDict()
        #: When the gateway's last KEEPALIVE says the session ends (``None``: no session).
        self.lease_until: Optional[float] = None
        self.stats = {"routed_in": 0, "routed_out": 0, "keepalives": 0, "bad": 0}
        transport.set_receiver(self._on_frame)

    # ---------------------------------------------------------------- Transport

    def set_receiver(self, callback: Receiver) -> None:
        self._receiver = callback

    def send(self, data: bytes) -> None:
        data = bytes(data)
        answer = self._answer
        if answer is not None and len(data) == 6 and data[0] == FrameKind.REPLY:
            asker, message_id, route = answer
            if data[2] == asker and (data[4] << 8 | data[3]) == message_id:
                if data[5] == ReplyResult.ACCEPTED:
                    self._result_routes[(asker, message_id)] = route
                    while len(self._result_routes) > MAX_RESULT_ROUTES:
                        self._result_routes.popitem(last=False)
                data = add_route(data, route)
                self.stats["routed_out"] += 1
        elif len(data) == 6 and data[0] == FrameKind.RESULT:
            route = self._result_routes.pop((data[2], data[4] << 8 | data[3]), None)
            if route is not None:
                data = add_route(data, route)
                self.stats["routed_out"] += 1
        self._transport.send(data)

    def start(self) -> None:
        """No-op; lets a session start and stop this layer like a port."""

    def close(self) -> None:
        closer = getattr(self._transport, "close", None)
        if closer is not None:
            closer()

    # ---------------------------------------------------------------- receiving

    def _on_frame(self, plain: bytes) -> None:
        if not is_routed(plain):
            if len(plain) >= 5 and plain[0] in (FrameKind.REQUEST, FrameKind.WAKE):
                if plain[2] == self.node_id:
                    self._answer = None  # flooded: answer by flood
            self._deliver(plain)
            return
        try:
            frame, relays = decode_routed(plain)
        except ValueError:
            self.stats["bad"] += 1
            return
        if frame.target_id != self.node_id:
            return
        self.stats["routed_in"] += 1
        back = reply_route(relays)
        if isinstance(frame, KeepaliveFrame):
            self.stats["keepalives"] += 1
            self.lease_until = None if frame.lease == 0 else self.clock() + frame.lease
            reply = ReplyFrame(
                sender_id=self.node_id,
                target_id=frame.sender_id,
                sequence=frame.sequence,
                result=ReplyResult.OK,
                epoch=frame.epoch,
            )
            self._transport.send(add_route(reply.encode(), back))
            return
        if isinstance(frame, (RequestFrame, WakeFrame)):
            self._answer = (frame.sender_id, frame.message_id, back)
            self._deliver(frame.encode())
        elif isinstance(frame, ReplyFrame):
            self._deliver(frame.encode())  # the controller's acknowledgement of our RESULT

    def _deliver(self, plain: bytes) -> None:
        if self._receiver is not None:
            self._receiver(plain)
