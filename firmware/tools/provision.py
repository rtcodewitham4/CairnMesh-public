"""Provision a node's address and keys over its USB debug console.

    .venv/bin/python firmware/tools/provision.py /dev/ttyACM1 --node 1 --test-keys
    .venv/bin/python firmware/tools/provision.py /dev/ttyACM1 --node 7 --net HEX32 --app HEX32 [--keyver N]

Sends "keys set", checks the key check values the node reports against
the ones computed here (AES(key, zero block)[:3]), then reboots the node
so it uses them. Keys never come back from the node: only check values.
--test-keys uses tests/vectors' keys (bench only). Run from the
repository root; the port needs access (udev rule).
"""

from __future__ import annotations

import argparse
import os
import select
import sys
import termios
import time

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from cairnmesh.vectors import APP_KEY, NETWORK_KEY  # noqa: E402


def kcv(key: bytes) -> str:
    enc = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    return (enc.update(bytes(16)) + enc.finalize())[:3].hex()


class Console:
    def __init__(self, path: str) -> None:
        self.fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        a = termios.tcgetattr(self.fd)
        a[0] = a[1] = a[3] = 0
        a[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
        termios.tcsetattr(self.fd, termios.TCSANOW, a)
        self.read(0.5)  # the banner, if the port was just opened

    def read(self, quiet: float) -> str:
        out, last = b"", time.time()
        while time.time() - last < quiet:
            r, _, _ = select.select([self.fd], [], [], 0.02)
            if r:
                try:
                    chunk = os.read(self.fd, 4096)
                except BlockingIOError:
                    continue
                except OSError:
                    break  # the device went away (reboot)
                if not chunk:
                    break  # hung up: readable forever, nothing to read
                out += chunk
                last = time.time()
        return out.decode(errors="replace")

    def command(self, line: str, quiet: float = 0.4) -> str:
        os.write(self.fd, line.encode() + b"\r")
        return self.read(quiet)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("port")
    p.add_argument("--node", type=int, required=True)
    p.add_argument("--net", help="network key, 32 hex digits")
    p.add_argument("--app", help="application key, 32 hex digits")
    p.add_argument("--keyver", type=int, default=0)
    p.add_argument("--test-keys", action="store_true", help="tests/vectors' keys: bench only")
    p.add_argument("--no-reboot", action="store_true")
    args = p.parse_args()
    if args.test_keys:
        net, app = NETWORK_KEY, APP_KEY
    elif args.net and args.app:
        net, app = bytes.fromhex(args.net), bytes.fromhex(args.app)
    else:
        p.error("give --net and --app, or --test-keys")
    if len(net) != 16 or len(app) != 16:
        p.error("keys are 16 bytes")

    con = Console(args.port)
    reply = con.command(f"keys set {args.node} {net.hex()} {app.hex()} {args.keyver}", 1.0)
    want = f"stored: node {args.node} net kcv {kcv(net)} app kcv {kcv(app)}"
    if want not in reply:
        print("FAIL: the node did not confirm the keys:", reply.split("\r\n")[-3:])
        return 1
    print(f"ok   node {args.node}: net kcv {kcv(net)}, app kcv {kcv(app)} (match)")
    if not args.no_reboot:
        con.command("reboot", 0.3)
        print("rebooting; check with 'status' that it runs")
    return 0


if __name__ == "__main__":
    sys.exit(main())
