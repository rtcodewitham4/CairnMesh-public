"""Set and read the test receive filter of a TESTFILTER build of cmd/wle5node over its console.

    .venv/bin/python firmware/tools/rxfilter_ctl.py /dev/ttyUSB0 caps
    .venv/bin/python firmware/tools/rxfilter_ctl.py /dev/ttyUSB0 topo 66-1,1-2,2-3
    .venv/bin/python firmware/tools/rxfilter_ctl.py /dev/ttyUSB0 asym NAME 66,2     # NON-PHYSICAL, named tests only
    .venv/bin/python firmware/tools/rxfilter_ctl.py /dev/ttyUSB0 all
    .venv/bin/python firmware/tools/rxfilter_ctl.py /dev/ttyUSB0 status
    .venv/bin/python firmware/tools/rxfilter_ctl.py /dev/ttyUSB0 log

The filter drops frames by TX (the node that transmitted this copy, spec 5) after the MIC
verified and before any protocol state moves (firmware/rxfilter, node/rxhook_on.go). It exists
only in a build made with `make wle5-build CMD=wle5node WLE5_TESTFILTER=1`; the default build has
no `rxf` command. Every command here starts with the capability query: a node that does not
answer it with `rxf caps 1 ...` (an old image, a default build, a silent port) is a FAILURE, not
a pass, so a bench run never believes it has a topology it does not have. After every change the
node's own readback is compared with what was asked.

Exit status 0: done and read back; 1: failed (the reason on stderr); 2: usage. Run one node per
call; the same `topo` line goes to every node (each keeps its own neighbours, so the result is
symmetric). Take the bench lock around it (`flock -n ~/.cache/cairnmesh/bench.lock ...`) when the
port is the bench's.
"""

from __future__ import annotations

import argparse
import os
import re
import select
import sys
import termios
import time
from typing import Callable, Dict, List, Optional, Sequence, Tuple

CAPS_VERSION = 1
LOG_PAGE = 6


class RxfError(Exception):
    """The node did not do (or did not confirm) what was asked."""


class NoCapability(RxfError):
    """No `rxf caps 1` answer: an old image, a default build, or a silent port."""


def normalise_links(spec: str) -> List[Tuple[int, int]]:
    """'66-1, 2-1' -> [(1, 66), (1, 2)] in the order the node prints them (first mention, low-high)."""
    out: List[Tuple[int, int]] = []
    for item in re.split(r"[,\s]+", spec.strip()):
        if not item:
            continue
        if ">" in item or "<" in item:
            raise RxfError(f"{item!r}: a link holds both ways; one-way is the `asym` command")
        m = re.fullmatch(r"(\d+)-(\d+)", item)
        if not m:
            raise RxfError(f"bad link {item!r} (want A-B)")
        a, b = int(m[1]), int(m[2])
        if not (1 <= a <= 254 and 1 <= b <= 254) or a == b:
            raise RxfError(f"bad link {item!r} (IDs 1..254, A != B)")
        pair = (min(a, b), max(a, b))
        if pair not in out:
            out.append(pair)
    if not out:
        raise RxfError("no links")
    return out


def neighbours(links: Sequence[Tuple[int, int]], node: int) -> List[int]:
    """Who `node` hears under a symmetric link list, in ascending order (what `rxf hear` prints)."""
    heard = {b if a == node else a for a, b in links if node in (a, b)}
    return sorted(heard)


def parse_status(text: str) -> Dict[str, object]:
    """The `rxf` readback: node, gen, mode, asym_ok, counters, links, hear."""
    st: Dict[str, object] = {}
    first = re.search(
        r"rxf TESTFILTER v(\d+) node (\d+) gen (\d+) mode (all|topo|asym NONPHYSICAL name \S+) "
        r"asym-ok (yes|no)(?: \(node not running\))? passed (\d+) dropped (\d+) unauth (\d+)",
        text,
    )
    if not first:
        raise RxfError(f"unreadable rxf status: {text!r}")
    st["version"] = int(first[1])
    st["node"] = int(first[2])
    st["gen"] = int(first[3])
    st["mode"] = first[4].split()[0]
    if st["mode"] == "asym":
        st["name"] = first[4].split()[-1]
    st["asym_ok"] = first[5] == "yes"
    st["passed"], st["dropped"], st["unauth"] = int(first[6]), int(first[7]), int(first[8])
    links = re.search(r"^rxf links (\S+)", text, re.M)
    if links:
        st["links"] = [tuple(int(x) for x in p.split("-")) for p in links[1].split(",")]
    hear = re.search(r"^rxf hear (\S+)", text, re.M)
    if hear:
        st["hear"] = [] if hear[1] == "none" else [int(x) for x in hear[1].split(",")]
    return st


class Rxf:
    """The commands over any `send(line) -> reply text`."""

    def __init__(self, send: Callable[[str], str]) -> None:
        self.send = send

    def caps(self) -> str:
        reply = self.send("rxf caps")
        m = re.search(r"^rxf caps (\d+) (.*)$", reply, re.M)
        if not m:
            what = "unknown command (a default build, or an image from before the filter)" if "unknown command" in reply else (
                "no answer" if not reply.strip() else f"unexpected answer {reply.strip()!r}")
            raise NoCapability(f"the node does not answer `rxf caps`: {what}")
        if int(m[1]) != CAPS_VERSION:
            raise NoCapability(f"rxf caps version {m[1]}, this script speaks {CAPS_VERSION}")
        for need in ("key=tx", "atomic", "readback"):
            if need not in m[2]:
                raise NoCapability(f"rxf caps lacks {need!r}: {m[2]!r}")
        return m[2]

    def status(self) -> Dict[str, object]:
        return parse_status(self.send("rxf"))

    def topo(self, spec: str) -> Dict[str, object]:
        links = normalise_links(spec)
        self.caps()
        before = self.status()
        line = "rxf topo " + ",".join(f"{a}-{b}" for a, b in links)
        if len(line) > 110:
            raise RxfError("topology too long for the console line (120 characters)")
        def apply() -> Dict[str, object]:
            st = parse_status(self.send(line))
            node = int(st["node"])
            problems = []
            if st["mode"] != "topo":
                problems.append(f"mode {st['mode']}")
            if st.get("links") != links:
                problems.append(f"links {st.get('links')} != {links}")
            if st.get("hear") != neighbours(links, node):
                problems.append(f"hear {st.get('hear')} != {neighbours(links, node)}")
            if int(st["gen"]) != int(before["gen"]) + 1:
                problems.append(f"gen {before['gen']} -> {st['gen']}")
            if problems:
                raise RxfError(f"node {node}: the readback does not match the request: " + "; ".join(problems))
            # What the node holds now is what a second read says, not only the reply to the set.
            again = self.status()
            if (again["mode"], again.get("links"), again.get("hear"), again["gen"]) != (st["mode"], st.get("links"), st.get("hear"), st["gen"]):
                raise RxfError(f"node {node}: a second read differs from the reply")
            return again

        return self._or_roll_back(before, apply)

    def asym(self, name: str, ids: Sequence[int]) -> Dict[str, object]:
        if not re.fullmatch(r"[a-z0-9_-]{1,12}", name):
            raise RxfError("a name is 1..12 of a-z 0-9 _ -")
        if not ids or not all(1 <= i <= 254 for i in ids):
            raise RxfError("IDs are 1..254")
        want = sorted(set(ids))
        line = f"rxf asym {name} " + ",".join(str(i) for i in want)
        if len(line) > 110:
            raise RxfError("asym row too long for the console line (120 characters)")
        self.caps()
        before = self.status()

        def apply() -> Dict[str, object]:
            self.send("rxf asym-ok yes")
            st = parse_status(self.send(line))
            if st["mode"] != "asym" or st.get("name") != name or st.get("hear") != want or not st["asym_ok"]:
                raise RxfError(f"the readback does not match the request: {st}")
            return st

        return self._or_roll_back(before, apply)

    def _or_roll_back(self, before: Dict[str, object], apply: Callable[[], Dict[str, object]]) -> Dict[str, object]:
        """Run a change; if it does not read back as asked, put the node back as it was and fail."""
        try:
            return apply()
        except RxfError as err:
            # Read what the node holds before touching it: a change the node refused
            # ("nothing changed", same gen) needs no rollback, and `rxf all` would only
            # open a window with every transmitter heard.
            try:
                now = self.status()
            except RxfError as bad:
                raise RxfError(f"{err}; the node's state could not be read back ({bad}): check it with `status`") from err
            if now["gen"] == before["gen"]:
                raise RxfError(f"{err}; the node refused it, its state is unchanged (mode {before['mode']})") from err
            try:
                if all(now.get(k) == before.get(k) for k in ("mode", "links", "hear", "name")):
                    # Only the asym switch moved (the script opened it): close it, leave the matrix alone.
                    if now["asym_ok"] != before["asym_ok"]:
                        self.send("rxf asym-ok " + ("yes" if before["asym_ok"] else "no"))
                    self._check_same(before)
                else:
                    self._restore(before)
            except RxfError as bad:
                raise RxfError(f"{err}; ROLLBACK FAILED ({bad}): the node's filter state is unknown, run `all`") from err
            raise RxfError(f"{err}; rolled back to mode {before['mode']}") from err

    def _check_same(self, before: Dict[str, object]) -> None:
        now = self.status()
        for key in ("mode", "asym_ok", "links", "hear", "name"):
            if now.get(key) != before.get(key):
                raise RxfError(f"{key} is {now.get(key)!r}, was {before.get(key)!r}")

    def _restore(self, before: Dict[str, object]) -> None:
        self.send("rxf all")
        if before["asym_ok"]:
            self.send("rxf asym-ok yes")
        if before["mode"] == "topo" and before.get("links"):
            self.send("rxf topo " + ",".join(f"{a}-{b}" for a, b in before["links"]))  # type: ignore[union-attr]
        elif before["mode"] == "asym":
            self.send(f"rxf asym {before['name']} " + ",".join(str(i) for i in before["hear"]))  # type: ignore[union-attr]
        self._check_same(before)

    def reset(self) -> Dict[str, object]:
        self.caps()
        st = parse_status(self.send("rxf all"))
        if st["mode"] != "all" or st["asym_ok"]:
            raise RxfError(f"`rxf all` did not give the default: {st}")
        return st

    def drops(self) -> List[Dict[str, object]]:
        """All dropped frames the node still holds, oldest first (pages of LOG_PAGE)."""
        self.caps()
        out: List[Dict[str, object]] = []
        start = 1
        while True:
            reply = self.send(f"rxf log {start}")
            for m in re.finditer(
                r"^d (\d+) sender (\d+) tx (\d+) kind ([0-9a-f]{2}) fcnt (\d+) rssi (-?\d+|\?) mic ([0-9a-f]{8})$", reply, re.M
            ):
                out.append({
                    "n": int(m[1]), "sender": int(m[2]), "tx": int(m[3]), "kind": int(m[4], 16), "fcnt": int(m[5]),
                    "rssi": None if m[6] == "?" else int(m[6]), "mic": m[7],
                })
            tail = re.search(r"^rxf log next (\d+) lost (\d+) total (\d+)$", reply, re.M)
            if not tail:
                raise RxfError(f"unreadable rxf log reply: {reply!r}")
            nxt, total = int(tail[1]), int(tail[3])
            if nxt == start:
                if nxt <= total:
                    raise RxfError("rxf log made no progress")
                return out
            start = nxt
            if start > total:
                return out


class SerialConsole:
    """The node's console on a tty (115200 8N1, as provision.py)."""

    def __init__(self, path: str, quiet: float = 0.4) -> None:
        self.quiet = quiet
        self.fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        a = termios.tcgetattr(self.fd)
        a[0] = a[1] = a[3] = 0
        a[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
        a[4] = a[5] = termios.B115200
        termios.tcsetattr(self.fd, termios.TCSANOW, a)
        self._read(0.5)

    def _read(self, quiet: float) -> str:
        out, last = b"", time.time()
        while time.time() - last < quiet:
            r, _, _ = select.select([self.fd], [], [], 0.02)
            if r:
                try:
                    chunk = os.read(self.fd, 4096)
                except BlockingIOError:
                    continue
                except OSError:
                    break
                if not chunk:
                    break
                out += chunk
                last = time.time()
        return out.decode(errors="replace")

    def command(self, line: str) -> str:
        os.write(self.fd, line.encode() + b"\r")
        text = self._read(self.quiet).replace("\r", "")
        lines = text.split("\n")
        if lines and lines[0].strip() == line.strip():
            lines = lines[1:]  # the echo
        if lines and lines[-1].strip() == ">":
            lines = lines[:-1]  # the prompt
        return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("port")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("caps")
    sub.add_parser("status")
    sub.add_parser("all")
    sub.add_parser("log")
    t = sub.add_parser("topo")
    t.add_argument("links", nargs="+", help="A-B[,C-D ...]: undirected links, the same line for every node")
    a = sub.add_parser("asym")
    a.add_argument("name")
    a.add_argument("ids", help="ID[,ID ...]: the only transmitters this node hears (one way, NON-PHYSICAL)")
    args = p.parse_args(argv)
    rxf = Rxf(SerialConsole(args.port).command)
    try:
        if args.cmd == "caps":
            print(rxf.caps())
        elif args.cmd == "status":
            rxf.caps()
            print(rxf.status())
        elif args.cmd == "topo":
            print(rxf.topo(" ".join(args.links)))
        elif args.cmd == "asym":
            print("NON-PHYSICAL one-way row for the named protocol test", args.name, file=sys.stderr)
            print(rxf.asym(args.name, [int(x) for x in args.ids.split(",")]))
        elif args.cmd == "all":
            print(rxf.reset())
        else:
            for d in rxf.drops():
                print(d)
    except RxfError as e:
        print(f"FAIL: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
