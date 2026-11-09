"""Power-cut bench for the dedup records and replay marks (spec 4.1, 5).

Drives the bench firmware (cmd/node) like serial_bench.py, resets the chip
through the programmer (NRST over SWD, mid-traffic) and checks that what the
board must remember survives:

* marks: a frame the board took before the reset, played into the port
  again after it, is dropped by the secure layer (no answer, no action);
  before the reset a never-delivered copy of a frame is answered, so the
  injection path itself works;
* records: a command whose answers were all lost before the reset is asked
  about afterwards (status query, Q18) and answered from the record: done
  before the reset -> its result; cut mid-run -> ERROR; never run again;
* a storm of resets at random moments under a burst: every command ends
  certain, the board always boots (no NMI loop on a torn flash slot) and its
  frame counter never falls back.

The LED (PA5) is read through SWD (GPIOA ODR) without halting the core.
Run from the repository root after ``make flash CMD=node BENCH=1``:

    .venv/bin/python firmware/tools/persist_bench.py /dev/ttyACM0 [--storm 20]
"""

from __future__ import annotations

import argparse
import os
import random
import re
import select
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.dirname(__file__))

from serial_bench import NODE, Port  # noqa: E402

from cairnmesh.events import FrameRejected, HeartbeatReceived, LinkError, ReplyIgnored  # noqa: E402
from cairnmesh.frames import ReplyResult  # noqa: E402
from cairnmesh.secure import AppKeys  # noqa: E402
from cairnmesh.session import LinkSession  # noqa: E402
from cairnmesh.vectors import APP_KEY, NETWORK_KEY  # noqa: E402

PROGRAMMER = Path(
    os.environ.get(
        "CAIRNMESH_PROGRAMMER", str(Path.home() / "apps/STM32CubeProrammer/bin/STM32_Programmer_CLI")
    )
)
GPIOA_ODR = 0x50000014
LED_BIT = 1 << 5


class TapPort(Port):
    """A port that records what is written and can drop either direction.

    It also outlives the device: with the USB cable pulled (the ST-LINK and
    its serial port go with the board's power) reads return nothing and
    writes are lost, like the air, until the port comes back.
    """

    def __init__(self, path: str) -> None:
        super().__init__(path)
        self.path = path
        self.writes: list[bytes] = []
        self.deaf = False  # drop what the board sends
        self.mute = False  # drop what the controller sends
        self.lost: list[float] = []  # when the port went away
        self.back: list[float] = []  # when it came back

    def _gone(self) -> None:
        try:
            os.close(self.fd)
        except OSError:
            pass
        self.fd = None
        self.lost.append(time.monotonic())

    def _reopen(self) -> bool:
        try:
            Port.__init__(self, self.path)
        except OSError:
            time.sleep(self.timeout)
            return False
        self.back.append(time.monotonic())
        return True

    def read(self, n: int) -> bytes:
        if self.fd is None and not self._reopen():
            return b""
        try:
            ready, _, _ = select.select([self.fd], [], [], self.timeout)
            if not ready:
                return b""
            data = os.read(self.fd, max(1, n))
        except BlockingIOError:
            return b""
        except OSError:
            self._gone()
            return b""
        if not data:  # readable but empty: the tty hung up
            self._gone()
            return b""
        return b"" if self.deaf else data

    def close(self) -> None:
        if self.fd is not None:
            super().close()

    def write(self, data: bytes) -> None:
        self.writes.append(bytes(data))
        if self.mute or self.fd is None:
            return
        try:
            super().write(data)
        except OSError:
            self._gone()

    def inject(self, data: bytes) -> None:
        if self.fd is None:
            return
        try:
            Port.write(self, data)
        except OSError:
            self._gone()


def programmer(*args: str) -> str:
    done = subprocess.run([str(PROGRAMMER), *args], capture_output=True, text=True, timeout=60)
    return done.stdout


def reset_board() -> None:
    programmer("-c", "port=SWD", "mode=HOTPLUG", "-hardRst")


def led_on() -> bool:
    out = programmer("-c", "port=SWD", "mode=HOTPLUG", "-r32", hex(GPIOA_ODR), "4")
    found = re.search(r"0x%08X\s*:\s*([0-9A-Fa-f]{8})" % GPIOA_ODR, out)
    if not found:
        raise RuntimeError("cannot read GPIOA ODR:\n" + out)
    return bool(int(found.group(1), 16) & LED_BIT)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("port")
    parser.add_argument("--storm", type=int, default=20, help="resets under a burst")
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument(
        "--pulls", type=int, default=0,
        help="instead of the SWD tests: N rounds of a real power cut, the USB cable pulled by hand under a burst",
    )
    parser.add_argument(
        "--state", default=os.path.join(os.path.dirname(__file__), "..", "results", "bench-state")
    )
    args = parser.parse_args()
    rng = random.Random(args.seed)

    heartbeats, rejected, errors, ignored = [], [], [], []

    def on_event(event) -> None:
        if isinstance(event, HeartbeatReceived):
            heartbeats.append(time.monotonic())
        elif isinstance(event, FrameRejected):
            rejected.append(event.reason)
        elif isinstance(event, LinkError):
            errors.append(event.message)
        elif isinstance(event, ReplyIgnored):
            ignored.append((time.monotonic(), event.sequence, event.reason))

    os.makedirs(args.state, exist_ok=True)
    port = TapPort(args.port)
    session = LinkSession.serial(
        port,
        nodes=(NODE,),
        key=NETWORK_KEY,
        app_keys=AppKeys({NODE: APP_KEY}),
        state_file=os.path.join(args.state, "secure.json"),
    )
    session.events.subscribe(on_event)
    session.start()
    failures: list[str] = []

    def report(name: str, good: bool, line: str) -> None:
        print(("ok   " if good else "FAIL ") + f"{name:<28} {line}")
        if not good:
            failures.append(name)

    def ask(action: int, value: int, timeout: float = 120):
        return session.request(NODE, action, value).result(timeout=timeout)

    def booted(after: float, within: float = 8.0) -> bool:
        deadline = time.monotonic() + within
        while time.monotonic() < deadline:
            if any(t > after for t in heartbeats):
                return True
            time.sleep(0.05)
        return False

    def quiet_since(since: float) -> bool:
        return not any(t > since for t, _, _ in ignored)

    def wait_for(events: list, count: int, within: float = 600.0) -> bool:
        deadline = time.monotonic() + within
        while len(events) <= count and time.monotonic() < deadline:
            time.sleep(0.05)
        return len(events) > count

    def pulls() -> None:
        """Real power cuts: the flash write under way when the cable goes is torn."""

        passed = round_ = 0
        while passed < args.pulls:
            round_ += 1
            port.writes.clear()
            o = ask(1, 0)
            seen = port.writes[-1]  # answered at the first try: the board took it
            if o.attempts != 1:
                report(f"pull{round_}_setup", False, f"attempts={o.attempts}")
                continue
            time.sleep(1.5)  # its mark is written

            stop = threading.Event()
            results: list = []

            def burst() -> None:
                n = 0
                while not stop.is_set():
                    n += 1
                    # Mostly instant commands (two log writes each), now and then
                    # a 0.8 s one (ACCEPTED, then RESULT).
                    o = ask(2, 8) if n % 10 == 0 else ask(4, 0)
                    final = o.completion.result(timeout=120) if o.completion is not None else o.result
                    results.append((o, final))

            ask(1, 1)  # the green LED lit: pull now (it is dark again after the power cut)
            lost, back = len(port.lost), len(port.back)
            worker = threading.Thread(target=burst, daemon=True)
            worker.start()
            print(f">>> 现在可以拔（已通过 {passed}/{args.pulls}）：拔掉 USB，约 2 s 后插回（60 s 内），然后等下一个 >>> 提示", flush=True)
            if not wait_for(port.lost, lost):
                report(f"pull{round_}", False, "the port never went away")
                stop.set()
                return
            stop.set()
            wait_for(port.back, back)
            checking = len(port.lost)
            up = booted(port.back[-1], within=15)
            worker.join(timeout=200)
            time.sleep(2.0)  # late duplicate answers settle
            mark = time.monotonic()
            port.inject(seen)
            time.sleep(1.0)
            if len(port.lost) > checking or port.fd is None:
                print(f"pull{round_}: 检查时又断电了，这一轮不算；等提示再拔", flush=True)
                while port.fd is None:
                    time.sleep(0.1)
                booted(port.back[-1], within=15)
                continue

            certain = all(
                o.status == "not_run"
                or (o.status in ("done", "accepted") and final in (ReplyResult.OK, ReplyResult.ERROR))
                for o, final in results
            )
            tally: dict[tuple, int] = {}
            for o, final in results:
                key = (o.status, final, "queried" if o.queried else "answered")
                tally[key] = tally.get(key, 0) + 1
            report(
                f"pull{round_}",
                up and certain and quiet_since(mark) and not worker.is_alive(),
                f"off {port.back[-1] - port.lost[-1]:.1f} s, boot {up}, commands {len(results)}, "
                f"replayed frame dropped {quiet_since(mark)}; {tally}",
            )
            passed += 1

    try:
        time.sleep(1.0)
        if args.pulls:
            pulls()
        else:
            # ---- marks ----
            ask(1, 0)
            port.writes.clear()
            port.mute = True  # the first copy of "LED on" never reaches the board
            outcome_fut = session.request(NODE, 1, 1)
            time.sleep(0.1)
            port.mute = False
            outcome = outcome_fut.result(timeout=30)
            undelivered, delivered = port.writes[0], port.writes[-1]
            report("led_on_after_lost_copy", outcome.ok and outcome.attempts >= 2 and led_on(),
                   f"attempts={outcome.attempts} copies={len(port.writes)}")
            ask(1, 0)
            report("led_off", not led_on(), "")

            # The never-delivered copy is new to the board (within its 32-frame
            # window): it passes the secure layer, the record answers it (the
            # controller ignores the late reply) and nothing runs again.
            mark = time.monotonic()
            port.inject(undelivered)
            time.sleep(0.8)
            report("inject_unseen_answered", not quiet_since(mark) and not led_on(),
                   f"ignored replies {sum(t > mark for t, _, _ in ignored)}, led {led_on()}")

            mark = time.monotonic()
            port.inject(delivered)
            time.sleep(0.8)
            report("inject_seen_dropped", quiet_since(mark) and not led_on(), "before the reset")

            time.sleep(1.5)  # marks are written at most once a second
            mark = time.monotonic()
            reset_board()
            report("boot_after_reset", booted(mark), "")
            mark = time.monotonic()
            port.inject(delivered)
            port.inject(undelivered)
            time.sleep(0.8)
            report("inject_after_reset_dropped", quiet_since(mark) and not led_on(),
                   f"ignored replies {sum(t > mark for t, _, _ in ignored)}, led {led_on()}")

            # ---- records ----
            def lost_answers(name: str, cut_after: float, want: int) -> None:
                port.deaf = True
                started = time.monotonic()
                future = session.request(NODE, 2, 30)  # 3 s: ACCEPTED, then RESULT
                time.sleep(0.2)
                port.mute = True
                time.sleep(cut_after)
                reset_board()
                time.sleep(0.5)
                port.deaf = port.mute = False
                outcome = future.result(timeout=120)
                final = outcome.result
                if outcome.completion is not None:
                    final = outcome.completion.result(timeout=60)
                took = time.monotonic() - started
                report(name, final == want and outcome.status in ("done", "accepted"),
                       f"status={outcome.status} result={outcome.result} final={final} "
                       f"queried={outcome.queried} attempts={outcome.attempts} {took:.1f} s")

            lost_answers("record_done_before_reset", 4.0, ReplyResult.OK)
            lost_answers("record_cut_mid_action", 1.0, ReplyResult.ERROR)
            o = ask(2, 4)
            report("next_command_runs", o.ok and o.attempts == 1, f"attempts={o.attempts}")

            # ---- reset storm ----
            stop = threading.Event()
            results: list = []

            def burst() -> None:
                while not stop.is_set():
                    results.append(ask(4, 0))

            worker = threading.Thread(target=burst, daemon=True)
            worker.start()
            boots = 0
            for _ in range(args.storm):
                time.sleep(rng.uniform(0.0, 1.5))
                mark = time.monotonic()
                reset_board()
                boots += booted(mark)
            stop.set()
            worker.join(timeout=200)
            # A reset between running a command and logging its result reads back
            # ERROR (spec 4.1): at most one such command per reset.
            outcomes: dict[tuple, int] = {}
            for o in results:
                key = (o.status, o.result, "queried" if o.queried else "answered")
                outcomes[key] = outcomes.get(key, 0) + 1
            cut = sum(o.result == ReplyResult.ERROR for o in results)
            if cut:
                print("     ERROR answers (command #, sequence):",
                      [(n, o.sequence) for n, o in enumerate(results) if o.result == ReplyResult.ERROR])
            good = (boots == args.storm and not worker.is_alive()
                    and all(o.status == "done" and o.result in (ReplyResult.OK, ReplyResult.ERROR) for o in results)
                    and cut <= args.storm)
            report("reset_storm", good, f"resets {args.storm}, boots {boots}, commands {len(results)}, "
                   f"cut {cut}; {outcomes}")
            o = ask(4, 0)
            report("alive_after_storm", o.ok, f"attempts={o.attempts}")
    finally:
        session.stop()

    print(f"heartbeats {len(heartbeats)}; rejected frames {len(rejected)} {sorted(set(rejected))}; "
          f"link errors {errors[:3]}")
    if rejected or errors:
        failures.append("rejected frames or link errors")
    print("RESULT", "FAIL " + ", ".join(failures) if failures else "PASS")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
