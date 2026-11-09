"""Capture the output of cmd/selftest and turn it into a Markdown table.

Uses only the Python standard library. It stops at "R done", at a fatal
error or after the timeout, so two captures never share the port.

Usage:
  loadreport.py capture <port> <log> [timeout_s]   read the port into <log>
  loadreport.py table <log>                        print the result table
"""

import os
import re
import sys
import termios
import time

LINE = re.compile(r"^B (\S+) mhz=(\d+) n=(\d+) min=(\d+) avg=(\d+) max=(\d+)")


def open_port(port, baud=termios.B115200):
    fd = os.open(port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    cc = list(termios.tcgetattr(fd)[6])
    cc[termios.VMIN] = 0
    cc[termios.VTIME] = 0
    cflag = termios.CS8 | termios.CREAD | termios.CLOCAL
    termios.tcsetattr(fd, termios.TCSANOW, [0, 0, cflag, 0, baud, baud, cc])
    termios.tcflush(fd, termios.TCIFLUSH)
    return fd


def capture(port, log, timeout):
    fd = open_port(port)
    data = b""
    start = time.time()
    while time.time() - start < timeout:
        try:
            chunk = os.read(fd, 4096)
        except BlockingIOError:
            chunk = b""
        if not chunk:
            time.sleep(0.02)
            continue
        data += chunk
        tail = data[data.find(b"R start"):] if b"R start" in data else b""
        if b"R done" in tail or b"fatal error" in tail or b"panic:" in tail:
            time.sleep(0.3)
            try:
                data += os.read(fd, 4096)
            except BlockingIOError:
                pass
            break
    os.close(fd)
    # Keep the last run only: the ST-LINK buffers bytes from before the reset.
    if b"R start" in data:
        data = data[data.rindex(b"R start"):]
    with open(log, "wb") as out:
        out.write(data)
    print(f"captured {len(data)} bytes in {time.time() - start:.1f} s -> {log}")


def table(log):
    rows = {}
    order = []
    with open(log, encoding="utf-8", errors="replace") as f:
        lines = f.read().splitlines()
    for line in lines:
        m = LINE.match(line.strip())
        if not m:
            continue
        name, mhz = m.group(1), int(m.group(2))
        n, lo, avg, hi = (int(m.group(i)) for i in range(3, 7))
        if name not in rows:
            rows[name] = {}
            order.append(name)
        rows[name][mhz] = (n, lo, avg, hi)

    print("| test | n | cycles min / avg / max @4 MHz | µs avg @4 MHz | cycles avg @48 MHz | µs avg @48 MHz |")
    print("|---|---|---|---|---|---|")
    for name in order:
        r = rows[name]
        c4, c48 = r.get(4), r.get(48)
        n = (c4 or c48)[0]
        cyc4 = f"{c4[1]} / {c4[2]} / {c4[3]}" if c4 else "-"
        us4 = f"{c4[2] / 4:.1f}" if c4 else "-"
        cy48 = str(c48[2]) if c48 else "-"
        us48 = f"{c48[2] / 48:.2f}" if c48 else "-"
        print(f"| {name} | {n} | {cyc4} | {us4} | {cy48} | {us48} |")
    print()
    for line in lines:
        if line.startswith("R ") or "fatal" in line or "panic" in line:
            print(line.rstrip())


def main():
    if len(sys.argv) >= 4 and sys.argv[1] == "capture":
        capture(sys.argv[2], sys.argv[3], float(sys.argv[4]) if len(sys.argv) > 4 else 180)
    elif len(sys.argv) == 3 and sys.argv[1] == "table":
        table(sys.argv[2])
    else:
        print(__doc__)
        sys.exit(2)


if __name__ == "__main__":
    main()
