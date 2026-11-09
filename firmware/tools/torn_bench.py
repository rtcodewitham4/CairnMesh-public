"""PC side of cmd/flashtorn: power cuts that tear flash writes.

Flash cmd/flashtorn (``make flash CMD=flashtorn``), run this, then pull the
USB cable while the green LED is lit (the board hammers its scratch journal)
and plug it back after about 2 s; repeat. Each boot is checked:

* the board boots and prints (a double ECC error read without the NMI
  handler would stop it at every boot: no line at all);
* no entry is lost: the boot's newest entry is not below the last one the
  board reported written before the cut;
* the entries replay in order.

It counts the boots that met a torn slot (bad CRC) or a double ECC error:
the point is to see those, and the node survive them.

    .venv/bin/python firmware/tools/torn_bench.py /dev/serial/by-id/usb-STMicroelectronics_*-if01 --pulls 10
"""

from __future__ import annotations

import argparse
import os
import re
import select
import sys
import termios
import time

LINE = re.compile(
    rb"H n= (\d+) boot: entries= (\d+) last= (\d+) torn= (\d+) ecc= (\d+) order= (\w+) hammer= (\w+)"
)


def open_port(path: str) -> int | None:
    try:
        fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    except OSError:
        return None
    cc = list(termios.tcgetattr(fd)[6])
    cc[termios.VMIN] = 0
    cc[termios.VTIME] = 0
    cflag = termios.CS8 | termios.CREAD | termios.CLOCAL
    termios.tcsetattr(fd, termios.TCSANOW, [0, 0, cflag, 0, termios.B115200, termios.B115200, cc])
    termios.tcflush(fd, termios.TCIFLUSH)
    return fd


def swd_resets(path: str, count: int) -> int:
    """NRST at random moments of the hammering: a reset aborts a program or an erase as a cut does."""

    import random
    import subprocess
    from pathlib import Path

    programmer = Path(
        os.environ.get(
            "CAIRNMESH_PROGRAMMER", str(Path.home() / "apps/STM32CubeProrammer/bin/STM32_Programmer_CLI")
        )
    )
    fd = open_port(path)
    if fd is None:
        print("cannot open", path)
        return 1
    buf = b""

    def lines(until: float):
        nonlocal buf
        while time.monotonic() < until:
            ready, _, _ = select.select([fd], [], [], 0.05)
            if ready:
                buf += os.read(fd, 4096)
            *done, buf = buf.split(b"\n")
            for line in done:
                m = LINE.search(line)
                if m:
                    yield m

    failures, torn_boots, ecc_boots, torn_slots, ecc_reads, unreset = [], 0, 0, 0, 0, 0
    for i in range(1, count + 1):
        written = None
        for m in lines(time.monotonic() + 70):  # until it hammers for a random while
            if m.group(7) == b"on":
                written = int(m.group(1))
                if random.random() < 0.4:
                    break
        else:
            failures.append(f"reset {i}: never hammered")
            break
        time.sleep(random.uniform(0, 0.3))
        for _ in range(5):  # the debugger cannot connect while an erase holds the bus
            done = subprocess.run([str(programmer), "-c", "port=SWD", "mode=HOTPLUG", "-hardRst"],
                                  capture_output=True, text=True, timeout=60)
            if "Hard reset is performed" in done.stdout:
                break
            unreset += 1
            time.sleep(0.05)
        else:
            failures.append(f"reset {i}: the programmer could not reset the board")
            continue
        termios.tcflush(fd, termios.TCIFLUSH)
        buf = b""
        # Lines still in the ST-LINK's buffer from before the reset carry the
        # old boot's numbers; the new boot's first lines are the settle ones.
        boot = next((m for m in lines(time.monotonic() + 10) if m.group(7) == b"off"), None)
        if boot is None:
            failures.append(f"reset {i}: no output 10 s after the reset (stuck: NMI?)")
            print(f"FAIL reset {i}: no output; raw after the reset: {buf[-300:]!r}", flush=True)
            continue
        entries, last, torn, ecc = (int(boot.group(k)) for k in range(2, 6))
        order = boot.group(6).decode()
        good = last >= written and order == "ok"
        torn_boots += torn > 0
        ecc_boots += ecc > 0
        torn_slots += torn
        ecc_reads += ecc
        if not good or torn or ecc or i % 10 == 0:
            print(
                ("ok   " if good else "FAIL ")
                + f"reset {i}/{count}: before {written}, after boot last {last} entries {entries}, "
                f"torn {torn}, ecc {ecc}, order {order}",
                flush=True,
            )
        if not good:
            failures.append(f"reset {i}")
    os.close(fd)
    print(f"resets {count}: boots with a torn slot {torn_boots} ({torn_slots} slots), "
          f"with a double ECC error {ecc_boots} ({ecc_reads} reads); reset attempts refused {unreset}")
    print("RESULT", "FAIL " + ", ".join(failures) if failures else "PASS")
    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("port")
    parser.add_argument("--pulls", type=int, default=10, help="power cuts to check")
    parser.add_argument(
        "--swd", type=int, default=0,
        help="instead of pulls by hand: N resets over SWD (NRST) at random moments of the hammering",
    )
    args = parser.parse_args()
    if args.swd:
        return swd_resets(args.port, args.swd)

    fd = None
    buf = b""
    written = None  # newest entry reported written in the current boot
    boot_seen = False  # this connection has shown its boot line
    back_at = None
    boots = []  # (lost, torn, ecc, order, entries)
    failures = []

    def gone() -> None:
        nonlocal fd, buf, boot_seen, back_at
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        fd, buf, boot_seen, back_at = None, b"", False, None

    while len(boots) < args.pulls + 1:
        if fd is None:
            fd = open_port(args.port)
            if fd is None:
                time.sleep(0.1)
                continue
            back_at = time.monotonic()
        try:
            ready, _, _ = select.select([fd], [], [], 0.2)
            data = os.read(fd, 4096) if ready else b""
            if ready and not data:
                gone()
                continue
        except BlockingIOError:
            data = b""
        except OSError:
            gone()
            continue
        if not boot_seen and back_at is not None and time.monotonic() - back_at > 10:
            failures.append(f"boot {len(boots)}: no output 10 s after power-up (stuck: NMI?)")
            print("FAIL no output from the board 10 s after power-up", flush=True)
            back_at = None
        buf += data
        *lines, buf = buf.split(b"\n")
        for line in lines:
            m = LINE.search(line)
            if not m:
                if line.startswith(b"F "):
                    print("append failed:", line.decode(errors="replace").strip(), flush=True)
                continue
            n, entries, last, torn, ecc = (int(m.group(i)) for i in range(1, 6))
            order, hammer = m.group(6).decode(), m.group(7).decode()
            if not boot_seen:
                boot_seen = True
                lost = (written - last) if written is not None else 0
                boots.append((lost, torn, ecc, order, entries))
                good = lost <= 0 and order == "ok"
                label = "first boot" if len(boots) == 1 else f"cut {len(boots) - 1}/{args.pulls}"
                print(
                    ("ok   " if good else "FAIL ")
                    + f"{label:<12} before cut {written}, after boot last {last} entries {entries}, "
                    f"torn {torn}, ecc {ecc}, order {order}",
                    flush=True,
                )
                if not good:
                    failures.append(label)
            if hammer == "on" or written is None or n > written:
                written = n

    cuts = boots[1:]
    print(
        f"cuts {len(cuts)}: with a torn slot {sum(t > 0 for _, t, _, _, _ in cuts)}, "
        f"with a double ECC error {sum(e > 0 for _, _, e, _, _ in cuts)}"
    )
    print("RESULT", "FAIL " + ", ".join(failures) if failures else "PASS")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
