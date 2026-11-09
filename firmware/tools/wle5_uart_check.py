#!/usr/bin/env python3
"""Read the E77 uarthello serial output and assert on it (standard library only).

  wle5_uart_check.py [-p /dev/ttyUSB0] [-b 115200] [-n 5] [--echo] [--cmd uarthello|wle5check|wle5cw] [--cw-dbm N]

--cmd uarthello (default): passes when at least N lines "cairnmesh uarthello <n>" arrive with consecutive
n (the first n is whatever the board is at). With --echo it then sends all 256
byte values, drops the hello lines from what comes back and compares the rest
with what was sent. Exit 0 = pass; otherwise one "FAIL: reason" line on stderr
and exit 1.

--cmd wle5check: waits for one complete pass of cmd/wle5check ("wle5check start"
... "wle5check PASS|FAIL pass=n fail=n") and passes when every check in
CHECK_ITEMS is PASS and the summary line is "wle5check PASS". The program
repeats the pass every 3 s, so a pass cut off at the start is skipped.

--cmd wle5cw: the board sends ONE carrier (antenna or 50 ohm load must be on; the flash
already started it). Passes when "wle5cw: CW on" is followed by "wle5cw: done" and no
"refused" / "radio:" line came; a "wle5cw: power <n> dBm" line, if the start was not missed,
must say N (--cw-dbm, default 10) and the "t=" lines must not exceed the 10 s run.
The whole run takes 5.2 s + 10 s, so this waits up to 30 s.
"""
import argparse
import os
import re
import select
import sys
import termios
import time

HELLO = re.compile(rb"cairnmesh uarthello (\d+)\r\n")

# The checks of cmd/wle5check, in its order; keep in step with checktext.Names
# (a Go test compares them).
CHECK_ITEMS = ("busy", "status", "tcxo", "sync", "errors", "calib", "rssi")
CHECK_BLOCK = re.compile(
    rb"wle5check start cycle=(\d+) cpu_hz=\d+\r\n(.*?)wle5check (PASS|FAIL) pass=(\d+) fail=(\d+)\r\n", re.S)
CHECK_ITEM = re.compile(rb"wle5check (\w+) (PASS|FAIL)([^\r\n]*)\r\n")


CW_LINE = re.compile(rb"wle5cw: ([^\r\n]*)\r\n")


def judge_cw(buf, dbm):
    """Return (None, summary) when the wle5cw run in buf is clean, else (reason, None);
    (None, None) while it is not finished."""
    lines = [m.group(1).decode("ascii", "replace") for m in CW_LINE.finditer(buf)]
    for ln in lines:
        if "refused" in ln or ln.startswith("radio:"):
            return "board says: wle5cw: " + ln, None
    for ln in lines:
        m = re.fullmatch(r"power (-?\d+) dBm", ln)
        if m and int(m.group(1)) != dbm:
            return "power line says %s dBm, expected %d" % (m.group(1), dbm), None
    if "CW on" not in lines:
        return None, None
    after = lines[lines.index("CW on"):]
    if "done" not in after:
        return None, None
    ts = [int(m.group(1)) for ln in after for m in [re.fullmatch(r"t= ?(\d+)", ln)] if m]
    if ts and max(ts) > 10:
        return "t=%d: the carrier ran past 10 s" % max(ts), None
    return None, "CW on ... done (%d t= lines)%s" % (
        len(ts), "" if any(l.startswith("power ") for l in lines) else ", power line not seen (start missed)")


def check_wle5cw(fd, dbm, timeout):
    buf, end = b"", time.monotonic() + timeout
    while time.monotonic() < end:
        buf += read_some(fd, 0.2)
        why, ok = judge_cw(buf, dbm)
        if why:
            fail(why)
        if ok:
            return ok
    fail("no 'wle5cw: CW on' ... 'wle5cw: done' in %ds (got %d bytes)" % (timeout, len(buf)))


def fail(msg):
    print("FAIL: " + msg, file=sys.stderr)
    sys.exit(1)


def open_port(path, baud):
    speed = getattr(termios, "B%d" % baud, None)
    if speed is None:
        fail("unsupported baud %d" % baud)
    try:
        fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    except OSError as e:
        fail("cannot open %s: %s" % (path, e))
    attr = termios.tcgetattr(fd)
    attr[0] = 0  # iflag: no parity check, no CR/NL mapping, no flow control
    attr[1] = 0  # oflag: raw
    attr[2] = termios.CS8 | termios.CREAD | termios.CLOCAL  # 8N1, no modem lines
    attr[3] = 0  # lflag: no echo, non-canonical
    attr[4] = speed
    attr[5] = speed
    attr[6][termios.VMIN] = 0
    attr[6][termios.VTIME] = 0
    termios.tcsetattr(fd, termios.TCSANOW, attr)
    termios.tcflush(fd, termios.TCIOFLUSH)
    return fd


def read_some(fd, timeout):
    r, _, _ = select.select([fd], [], [], timeout)
    if not r:
        return b""
    try:
        return os.read(fd, 4096)
    except BlockingIOError:
        return b""


def check_hello(fd, want, timeout):
    buf, seen, end = b"", [], time.monotonic() + timeout
    while time.monotonic() < end:
        buf += read_some(fd, 0.2)
        seen = [int(m.group(1)) for m in HELLO.finditer(buf)]
        if len(seen) >= want:
            break
    if not seen:
        fail("no 'cairnmesh uarthello <n>' line in %ds (got %d bytes)" % (timeout, len(buf)))
    if len(seen) < want:
        fail("only %d hello lines in %ds, need %d" % (len(seen), timeout, want))
    for a, b in zip(seen, seen[1:]):
        if b != a + 1:
            fail("hello counter not consecutive: %d then %d" % (a, b))
    return seen


def sync_to_line_end(fd, timeout=3.0):
    """Drop input up to the next LF, so a half-received hello line cannot leak into the echo check."""
    termios.tcflush(fd, termios.TCIFLUSH)
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if b"\n" in read_some(fd, 0.2):
            return
    fail("no hello line while waiting to start the echo check")


def check_echo(fd, timeout=10.0):
    sent = bytes(range(256))
    sync_to_line_end(fd)
    # 16-byte chunks: the board buffers 128 bytes and drains them once a millisecond.
    for i in range(0, len(sent), 16):
        os.write(fd, sent[i:i + 16])
        time.sleep(0.02)
    raw, end = b"", time.monotonic() + timeout
    while time.monotonic() < end:
        raw += read_some(fd, 0.2)
        got = HELLO.sub(b"", raw)
        if len(got) >= len(sent):
            break
    got = HELLO.sub(b"", raw)
    if len(got) < len(sent):
        fail("echo: got %d of %d bytes back in %ds" % (len(got), len(sent), timeout))
    got = got[:len(sent)]  # anything after is a hello line cut off at the end
    for i, (a, b) in enumerate(zip(sent, got)):
        if a != b:
            fail("echo: byte %d sent 0x%02x got 0x%02x" % (i, a, b))


def parse_check_block(buf):
    """First complete wle5check pass in buf: (cycle, {item: (verdict, rest)}, verdict, pass, fail), or None."""
    m = CHECK_BLOCK.search(buf)
    if not m:
        return None
    items = {}
    for it in CHECK_ITEM.finditer(m.group(2)):
        items[it.group(1).decode()] = (it.group(2).decode(), it.group(3).decode().strip())
    return int(m.group(1)), items, m.group(3).decode(), int(m.group(4)), int(m.group(5))


def judge_check_block(block):
    """Return None when the pass is clean, else the reason."""
    cycle, items, verdict, npass, nfail = block
    # every FAIL line, including "init" (which only appears when the radio Init failed)
    bad = ["%s %s" % (n, v[1]) for n, v in items.items() if v[0] != "PASS"]
    missing = [n for n in CHECK_ITEMS if n not in items]
    if missing:
        return "pass %d lacks the checks %s" % (cycle, ", ".join(missing))
    if bad:
        return "pass %d: FAIL %s" % (cycle, "; ".join(bad))
    if verdict != "PASS" or nfail != 0 or npass != len(CHECK_ITEMS):
        return "pass %d: summary %s pass=%d fail=%d" % (cycle, verdict, npass, nfail)
    return None


def check_wle5check(fd, timeout):
    buf, end = b"", time.monotonic() + timeout
    while time.monotonic() < end:
        buf += read_some(fd, 0.2)
        block = parse_check_block(buf)
        if block:
            why = judge_check_block(block)
            if why:
                fail(why)
            return block
    fail("no complete 'wle5check start' ... 'wle5check PASS|FAIL' pass in %ds (got %d bytes)" % (timeout, len(buf)))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("-p", "--port", default=os.environ.get("WLE5_TTY", "/dev/ttyUSB0"))
    ap.add_argument("-b", "--baud", type=int, default=int(os.environ.get("WLE5_BAUD", "115200")))
    ap.add_argument("-n", "--lines", type=int, default=5)
    ap.add_argument("--echo", action="store_true", help="also check the 256-byte echo")
    ap.add_argument("--cmd", choices=("uarthello", "wle5check", "wle5cw"), default="uarthello",
                    help="which program is on the board")
    ap.add_argument("--cw-dbm", type=int, default=int(os.environ.get("WLE5_CW_DBM") or 10),
                    help="wle5cw: the power the board must report (default WLE5_CW_DBM or 10)")
    a = ap.parse_args()
    if a.cmd == "wle5cw":
        if a.echo:
            fail("--echo is for uarthello; wle5cw does not echo")
        fd = open_port(a.port, a.baud)
        try:
            print("PASS: wle5cw %s on %s @ %d" % (check_wle5cw(fd, a.cw_dbm, 30), a.port, a.baud))
        finally:
            os.close(fd)
        return
    if a.cmd == "wle5check":
        if a.echo:
            fail("--echo is for uarthello; wle5check does not echo")
        fd = open_port(a.port, a.baud)
        try:
            cycle, _, _, npass, nfail = check_wle5check(fd, 15)
            print("PASS: wle5check PASS pass=%d fail=%d (cycle %d) on %s @ %d" % (npass, nfail, cycle, a.port, a.baud))
        finally:
            os.close(fd)
        return
    if a.lines < 2:
        fail("-n must be at least 2 (consecutive needs two lines)")
    fd = open_port(a.port, a.baud)
    try:
        seen = check_hello(fd, a.lines, a.lines + 6)
        msg = "hello %d..%d (%d lines)" % (seen[0], seen[-1], len(seen))
        if a.echo:
            check_echo(fd)
            msg += ", echo 256/256"
        print("PASS: %s on %s @ %d" % (msg, a.port, a.baud))
    finally:
        os.close(fd)


if __name__ == "__main__":
    main()
