"""Drive the bench firmware (cmd/node) from the Python controller.

The board's serial port stands in for the air: ``LinkSession.serial`` with
the secure envelope and the application layer (wire format v3), exactly as a
controller would talk to a gateway. Uses only the standard library for the
port (no pyserial). Run from the repository root after ``make flash CMD=node BENCH=1``
on a board provisioned with the test keys (tools/provision.py --test-keys):

    .venv/bin/python firmware/tools/serial_bench.py /dev/ttyACM0 [--count 50]

Checks the demo actions of cmd/node (LED, ERROR, a short and a long slow
action: ACCEPTED then RESULT; an action whose result is known later: the
RESULT on completion, ERROR at its deadline), a run of back-to-back requests, the
extension layer (the B1 alarm module: raised by software through the real
interrupt path, its 500 ms signal outside the protocol holds the radio, so
the next answer needs a retry; disarmed it does nothing), heartbeats and
rejected frames; prints a summary and exits non-zero on a mismatch.
"""

from __future__ import annotations

import argparse
import os
import select
import sys
import termios
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from cairnmesh.events import FrameRejected, HeartbeatReceived, LinkError  # noqa: E402
from cairnmesh.frames import ReplyResult  # noqa: E402
from cairnmesh.secure import AppKeys  # noqa: E402
from cairnmesh.session import LinkSession  # noqa: E402
from cairnmesh.vectors import APP_KEY, NETWORK_KEY  # noqa: E402

NODE = 0x01


class Port:
    """A raw termios port with ``read(n)`` / ``write`` / ``close`` (what ``SerialTransport`` needs)."""

    def __init__(self, path: str, baud: int = termios.B115200, timeout: float = 0.05) -> None:
        self.fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        cc = list(termios.tcgetattr(self.fd)[6])
        cc[termios.VMIN] = 0
        cc[termios.VTIME] = 0
        cflag = termios.CS8 | termios.CREAD | termios.CLOCAL
        termios.tcsetattr(self.fd, termios.TCSANOW, [0, 0, cflag, 0, baud, baud, cc])
        termios.tcflush(self.fd, termios.TCIOFLUSH)
        self.timeout = timeout

    def read(self, n: int) -> bytes:
        ready, _, _ = select.select([self.fd], [], [], self.timeout)
        if not ready:
            return b""
        try:
            return os.read(self.fd, max(1, n))
        except BlockingIOError:
            return b""

    def write(self, data: bytes) -> None:
        view = memoryview(data)
        while view:
            try:
                view = view[os.write(self.fd, view):]
            except BlockingIOError:
                select.select([], [self.fd], [], 0.1)

    def close(self) -> None:
        os.close(self.fd)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("port")
    parser.add_argument("--count", type=int, default=50, help="back-to-back requests")
    parser.add_argument(
        "--state",
        default=os.path.join(os.path.dirname(__file__), "..", "results", "bench-state"),
        help="directory of the controller's state files. Keep it: a controller that loses them restarts its frame "
        "counter at the Unix second, below what a busy earlier run used, and the board rightly drops its frames as "
        "replayed. Reused across a board reset it also checks that the board's counter never falls back.",
    )
    args = parser.parse_args()

    heartbeats, rejected, errors = [], [], []

    def on_event(event) -> None:
        if isinstance(event, HeartbeatReceived):
            heartbeats.append(time.monotonic())
        elif isinstance(event, FrameRejected):
            rejected.append(event.reason)
        elif isinstance(event, LinkError):
            errors.append(event.message)

    state = args.state
    os.makedirs(state, exist_ok=True)
    session = LinkSession.serial(
        Port(args.port),
        nodes=(NODE,),
        key=NETWORK_KEY,
        app_keys=AppKeys({NODE: APP_KEY}),
        state_file=os.path.join(state, "secure.json"),
    )
    session.events.subscribe(on_event)
    session.start()
    failures = []

    def check(name: str, action: int, value: int, want: int, completion: int | None = None) -> None:
        started = time.monotonic()
        outcome = session.request(NODE, action, value).result(timeout=30)
        took = time.monotonic() - started
        line = f"{name:<24} result={outcome.result} attempts={outcome.attempts} {took * 1000:7.1f} ms"
        good = outcome.result == want
        if completion is not None and outcome.completion is not None:
            final = outcome.completion.result(timeout=60)
            line += f" completion={final} after {(time.monotonic() - started):.2f} s"
            good = good and final == completion
        print(("ok   " if good else "FAIL ") + line)
        if not good:
            failures.append(name)

    try:
        time.sleep(1.0)
        check("led_on", 1, 1, ReplyResult.OK)
        check("action_error", 3, 0, ReplyResult.ERROR)
        check("short_action_0.4s", 2, 4, ReplyResult.OK)
        check("long_action_2s", 2, 20, ReplyResult.ACCEPTED, completion=ReplyResult.OK)
        # Result known later (ACTION 5: completes after VALUE x 0.1 s, deadline 5 s).
        check("later_completes_2s", 5, 20, ReplyResult.ACCEPTED, completion=ReplyResult.OK)
        check("later_misses_deadline", 5, 80, ReplyResult.ACCEPTED, completion=ReplyResult.ERROR)

        latencies = []
        for _ in range(args.count):
            started = time.monotonic()
            outcome = session.request(NODE, 4, 0).result(timeout=30)
            latencies.append(time.monotonic() - started)
            if outcome.result != ReplyResult.OK or outcome.attempts != 1:
                failures.append(f"burst result={outcome.result} attempts={outcome.attempts}")
        latencies.sort()
        print(
            f"burst {args.count}: median {latencies[len(latencies) // 2] * 1000:.1f} ms, "
            f"max {latencies[-1] * 1000:.1f} ms"
        )
        check("led_off", 1, 0, ReplyResult.OK)

        # Extension layer (cmd/node's B1 alarm: ACTION 0x10 arms, 0x11 raises the interrupt).
        def ping(name: str, want_retry: bool) -> None:
            started = time.monotonic()
            outcome = session.request(NODE, 4, 0).result(timeout=30)
            took = (time.monotonic() - started) * 1000
            good = outcome.result == ReplyResult.OK and (outcome.attempts >= 2) == want_retry
            print(("ok   " if good else "FAIL ") + f"{name:<24} attempts={outcome.attempts} {took:7.1f} ms")
            if not good:
                failures.append(name)

        check("alarm_disarm", 0x10, 0, ReplyResult.OK)
        check("b1_raise_disarmed", 0x11, 0, ReplyResult.OK)
        ping("answer_not_held", want_retry=False)
        check("alarm_arm", 0x10, 1, ReplyResult.OK)
        check("b1_raise", 0x11, 0, ReplyResult.OK)
        ping("answer_held_by_signal", want_retry=True)
        time.sleep(2.5)  # past the alarm's cooldown
        ping("answer_after_signal", want_retry=False)
        time.sleep(4.0)
    finally:
        session.stop()

    gaps = [b - a for a, b in zip(heartbeats, heartbeats[1:])]
    print(f"heartbeats {len(heartbeats)}" + (f", gaps {min(gaps):.2f}-{max(gaps):.2f} s" if gaps else ""))
    print(f"rejected frames {len(rejected)} {sorted(set(rejected))}; link errors {errors}")
    if not heartbeats:
        failures.append("no heartbeat")
    if rejected or errors:
        failures.append("rejected frames or link errors")
    print("RESULT", "FAIL " + ", ".join(failures) if failures else "PASS")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
