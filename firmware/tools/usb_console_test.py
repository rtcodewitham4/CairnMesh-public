"""Check the USB debug console (cmd/usbconsole) from the PC.

    python3 firmware/tools/usb_console_test.py /dev/ttyACM1

Runs the commands, then echoes 3 kB, 30 kB and 100 kB in lines of 60
characters while reading at the same time (a test that writes everything
before reading deadlocks: the tty's input buffer fills, cdc_acm stops
taking the device's echo, the console's backpressure stops reading, and
the host can no longer write). Every line must come back whole and in
order. The port needs access: a udev rule for 1209:0001.
"""

import os
import select
import sys
import termios
import time


def open_port(path):
    fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    a = termios.tcgetattr(fd)
    a[0] = a[1] = a[3] = 0
    a[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
    termios.tcsetattr(fd, termios.TCSANOW, a)
    return fd


def pump(fd, payload, quiet=1.0):
    """Write payload while reading; return what came back until quiet seconds of silence."""
    view, back, last = memoryview(payload), b"", time.time()
    while view or time.time() - last < quiet:
        r, w, _ = select.select([fd], [fd] if view else [], [], 0.05)
        if r:
            try:
                back += os.read(fd, 65536)
                last = time.time()
            except BlockingIOError:
                pass
        if w and view:
            try:
                view = view[os.write(fd, view[:4096]):]
            except BlockingIOError:
                pass
    return back


def main():
    fd = open_port(sys.argv[1])
    failures = []
    pump(fd, b"", 0.3)
    for cmd, want in ((b"help", b"help uid uptime stats"), (b"uid", None), (b"uptime", b" s"), (b"bogus", b"unknown")):
        reply = pump(fd, cmd + b"\r", 0.3)
        good = want is None or want in reply
        print(("ok   " if good else "FAIL ") + reply.decode(errors="replace").strip().replace("\r\n", " | "))
        if not good:
            failures.append(cmd.decode())
    for total in (3000, 30000, 100020):
        lines = [bytes([65 + i % 26]) * 60 for i in range(total // 60)]
        payload = b"".join(line + b"\r" for line in lines)
        started = time.time()
        back = pump(fd, payload)
        took = time.time() - started - 1.0
        got = [line.removeprefix(b"> ") for line in back.split(b"\r\n")]
        got = [line for line in got if line and 65 <= line[0] <= 90]
        good = got == lines
        print(("ok   " if good else "FAIL ") + f"echo {total} B: {len(got)}/{len(lines)} lines in order, "
              f"{len(payload) / took / 1000:.0f} kB/s each way")
        if not good:
            failures.append(f"echo {total}")
    stats = pump(fd, b"stats\r", 0.3).decode().strip()
    print(stats.replace("\r\n", " | "))
    if "rxdropped 0" not in stats:
        failures.append("rxdropped")
    print("RESULT", "FAIL " + ", ".join(failures) if failures else "PASS")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
