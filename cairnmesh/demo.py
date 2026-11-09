"""End-to-end demonstration of request/reply flow and duplicate suppression.

Included:
    - request, reply, wake and heartbeat frames
    - sequence numbers
    - retransmission after a timeout
    - duplicate-request detection

Not included:
    - radio hardware or a serial port
    - encryption or authentication
    - persistence
    - multi-hop forwarding
"""

from __future__ import annotations

import time

from .events import (
    LinkEvent,
    ReplyIgnored,
    ReplyReceived,
    RequestFailed,
    RequestSent,
)
from .session import LinkSession


def _print_event(event: LinkEvent) -> None:
    if isinstance(event, RequestSent):
        print(
            f"[CTRL] 第 {event.attempt} 次发送 {event.kind.name} "
            f"(SEQUENCE={event.sequence})"
        )
    elif isinstance(event, ReplyReceived):
        mark = "✓" if event.ok else "✗"
        print(
            f"[CTRL] {mark} 收到回复 SEQUENCE={event.sequence} "
            f"RESULT={event.result} 尝试 {event.attempts} 次 "
            f"RTT={event.rtt * 1000:.0f} ms"
        )
    elif isinstance(event, RequestFailed):
        print(f"[CTRL] ✗ {event.reason} SEQUENCE={event.sequence}")
    elif isinstance(event, ReplyIgnored):
        print(f"[CTRL] 忽略回复 SEQUENCE={event.sequence}: {event.reason}")


def main() -> None:
    # A 50% reply-drop rate makes the retry path easy to observe.
    session = LinkSession.simulated(
        node_ids=[1],
        downlink_drop=0.0,
        uplink_drop=0.5,
        heartbeat_interval=60.0,
        trace=lambda message: print("    " + message),
    )
    session.events.subscribe(_print_event)
    device = session.simulation.device(1)

    started = time.perf_counter()
    with session:
        print("=" * 60)
        print("[CTRL] 唤醒设备 0x01")
        wake = session.wake(1).result(timeout=30)

        print()
        print("=" * 60)
        print("[CTRL] 发送请求 ACTION=3 VALUE=100")
        outcome = session.request(1, action=0x03, value=100).result(timeout=30)
        # Let late replies to earlier retries arrive and be ignored.
        time.sleep(0.3)
    elapsed_ms = (time.perf_counter() - started) * 1000.0

    print()
    print("=" * 60)
    print("测试结束")
    print(f"唤醒结果: {'成功' if wake.ok else '失败'}")
    print(f"请求结果: {'成功' if outcome.ok else '失败'}，尝试 {outcome.attempts} 次")
    print(f"设备实际处理次数: {device.handled_count}")
    print(f"全程耗时: {elapsed_ms:.3f} ms")
    print("=" * 60)


if __name__ == "__main__":
    main()
