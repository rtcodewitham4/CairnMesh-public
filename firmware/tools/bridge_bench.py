"""Drive the gateway bridge firmware (cmd/gateway) on the NUCLEO dual-radio bench.

The two radios of the board talk to each other through the air, the PC
drives both over the bridge (spec 6.1, ``cairnmesh/bridge.py``). Run
from the repository root after ``make flash CMD=gateway``:

    .venv/bin/python firmware/tools/bridge_bench.py /dev/ttyACM0 [--count 20]

Checks: STATUS; radio 0 → 1 and 1 → 0 (every frame received whole, TX_DONE
airtime against the Semtech formula); rule 1 (two frames at once go out one
after the other); rule 2 (a frame not urgent waits for the other radio's
answer window, an urgent one does not); TUNE to channel 1 at SF9 and back;
a long preamble; rule 4 (a radio receiving holds its frame, BUSY); the
board's counters. Prints a summary, exits non-zero on a
mismatch. Uses only the standard library for the port.
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.dirname(__file__))

from cairnmesh.airtime import lora_airtime  # noqa: E402
from cairnmesh.bridge import TX_OK, GatewayBridge, TxDone  # noqa: E402
from cairnmesh.energy import symbol_time  # noqa: E402
from cairnmesh.serial_link import SerialTransport  # noqa: E402
from serial_bench import Port  # noqa: E402

FRAME_LEN = 16


class Bench:
    def __init__(self, bridge: GatewayBridge) -> None:
        self.bridge = bridge
        self.heard: list[list[tuple[bytes, float]]] = [[], []]
        self.done: list[TxDone] = []
        self.cond = threading.Condition()
        self.failures: list[str] = []
        for i, port in enumerate(bridge.ports):
            port.set_receiver(lambda frame, rssi, i=i: self._heard(i, frame, rssi))
        bridge.on_tx_done = self._done

    def _heard(self, radio: int, frame: bytes, rssi: float) -> None:
        with self.cond:
            self.heard[radio].append((frame, rssi))
            self.cond.notify_all()

    def _done(self, done: TxDone) -> None:
        with self.cond:
            self.done.append(done)
            self.cond.notify_all()

    def wait(self, predicate, timeout: float) -> bool:
        with self.cond:
            return self.cond.wait_for(predicate, timeout)

    def heard_by(self, radio: int, f: bytes) -> bool:
        return any(h[0] == f for h in self.heard[radio])

    def check(self, ok: bool, what: str) -> bool:
        print(("  ok   " if ok else "  FAIL ") + what)
        if not ok:
            self.failures.append(what)
        return ok

    def clear(self) -> None:
        with self.cond:
            self.heard = [[], []]
            self.done = []


def frame(tag: int, seq: int) -> bytes:
    return (b"BRIDGE" + bytes([tag, seq])).ljust(FRAME_LEN, b"-")


def loopback(b: Bench, tx: int, rx: int, count: int, sf: int) -> None:
    b.clear()
    air = lora_airtime(FRAME_LEN, sf)
    ok, rssis = 0, []
    for seq in range(count):
        f = frame(0x10 + tx, seq)
        b.bridge.ports[tx].send(f)
        if b.wait(lambda: b.heard_by(rx, f) and len(b.done) > seq, air + 1.0):
            ok += 1
            rssis.append(next(r for fr, r in b.heard[rx] if fr == f))
        time.sleep(0.02)
    b.wait(lambda: len(b.done) >= count, air + 1.0)
    airs = [d.air for d in b.done if d.result == TX_OK]
    b.check(ok == count, f"radio {tx} → {rx} at SF{sf}: {ok}/{count} received whole"
            + (f", RSSI {min(rssis):.0f}..{max(rssis):.0f} dBm" if rssis else ""))
    if airs:
        mean = sum(airs) / len(airs)
        b.check(abs(mean - air) < 0.004, f"  TX_DONE airtime {mean * 1000:.1f} ms vs formula {air * 1000:.1f} ms")


def rule1(b: Bench) -> None:
    b.clear()
    f0, f1 = frame(0x20, 0), frame(0x21, 0)
    b.bridge.ports[0].send(f0)
    b.bridge.ports[1].send(f1)
    air = lora_airtime(FRAME_LEN, 7)
    b.wait(lambda: len(b.done) >= 2 and b.heard_by(1, f0) and b.heard_by(0, f1), 2 * air + 1.0)
    done = {d.radio: d for d in b.done}
    if b.check(len(done) == 2, "rule 1: two TX_DONE"):
        b.check(done[1].queued >= done[0].air - 0.003,
                f"rule 1: radio 1 waited {done[1].queued * 1000:.0f} ms for radio 0's {done[0].air * 1000:.0f} ms")
    b.check(b.heard_by(0, f1), "rule 1: radio 0 heard radio 1's frame")
    # Before rule 4 radio 1 started at once when radio 0's frame ended,
    # before its own RxDone for that frame, and never heard it (2026-10-26).
    b.check(b.heard_by(1, f0), "rule 4: radio 1 heard radio 0's frame before sending its own")


def rule2(b: Bench) -> None:
    window = 0.5
    air = lora_airtime(FRAME_LEN, 7)
    for urgent in (False, True):
        b.clear()
        before = b.bridge.ping().counters["deferred"]
        b.bridge.ports[0].send(frame(0x30, int(urgent)), answer=window)
        b.bridge.ports[1].send(frame(0x31, int(urgent)), urgent=urgent)
        b.wait(lambda: len(b.done) >= 2, air * 2 + window + 1.0)
        done = {d.radio: d for d in b.done}
        kind = "urgent" if urgent else "not urgent"
        if not b.check(len(done) == 2, f"rule 2 ({kind}): two TX_DONE"):
            continue
        deferred = b.bridge.ping().counters["deferred"] - before
        waited = done[1].queued
        if urgent:
            b.check(waited < done[0].air + 0.05 and deferred == 0,
                    f"rule 2: urgent frame waited {waited * 1000:.0f} ms (radio 0's airtime only), not deferred")
        else:
            want = done[0].air + window
            b.check(abs(waited - want) < 0.03 and deferred == 1,
                    f"rule 2: frame not urgent waited {waited * 1000:.0f} ms "
                    f"(airtime + window {want * 1000:.0f} ms), deferred {deferred}")
        time.sleep(window)


def long_preamble(b: Bench) -> None:
    b.clear()
    preamble = 200
    f = frame(0x40, 0)
    b.bridge.ports[0].send(f, preamble=preamble)
    air = lora_airtime(FRAME_LEN, 7) + (preamble - 8) * symbol_time(7)
    b.wait(lambda: bool(b.done) and b.heard_by(1, f), air + 1.0)
    got = b.done[0].air if b.done else 0.0
    b.check(b.heard_by(1, f) and abs(got - air) < 0.005,
            f"preamble {preamble}: heard, airtime {got * 1000:.1f} ms vs {air * 1000:.1f} ms")


def tuned(bridge: GatewayBridge, channel: int, sf: int, timeout: float = 0.5) -> bool:
    """Wait until STATUS shows every radio on (channel, sf): TUNE waits in the board's queue."""

    end = time.monotonic() + timeout
    while time.monotonic() < end:
        status = bridge.ping()
        if status and all((r.channel, r.sf) == (channel, sf) for r in status.radios):
            return True
        time.sleep(0.02)
    return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("port")
    parser.add_argument("--count", type=int, default=20, help="frames per direction")
    args = parser.parse_args()

    transport = SerialTransport(Port(args.port))
    bridge = GatewayBridge(transport, on_error=lambda exc: print("  error:", exc))
    transport.start()
    b = Bench(bridge)
    try:
        status = bridge.ping(timeout=2.0)
        if not b.check(status is not None, "STATUS answered"):
            return 1
        b.check(status.version == 1 and len(status.radios) == 2 and all(r.healthy for r in status.radios),
                f"bridge v{status.version}, radios {[(r.healthy, r.channel, r.sf) for r in status.radios]}")
        loopback(b, 0, 1, args.count, 7)
        loopback(b, 1, 0, args.count, 7)
        rule1(b)
        rule2(b)
        for port in bridge.ports:
            port.set_radio(1, 9)
        b.check(tuned(bridge, 1, 9), "TUNE: both radios on channel 1, SF9")
        loopback(b, 0, 1, 5, 9)
        for port in bridge.ports:
            port.set_radio(0, 7)
        long_preamble(b)
        b.check(tuned(bridge, 0, 7), "TUNE back to channel 0, SF7")
        status = bridge.ping()
        c = status.counters
        print("  board counters:", c)
        for i, r in enumerate(status.radios):
            print(f"  radio {i}: crc {r.crc_errors} header {r.header_errors} bus retries {r.bus_retries} faults {r.faults}")
        b.check(c["held"] >= 1, f"rule 4: {c['held']} frames held for a reception")
        b.check(c["malformed"] == 0 and c["refused"] == 0 and c["queue_full"] == 0 and c["tune_failed"] == 0,
                "no malformed, refused, queue-full or failed TUNE on the board")
        b.check(bridge.malformed == 0 and bridge.ignored == 0 and transport.decoder.crc_errors == 0,
                "no bad or unknown message on the PC")
        b.check(not any(bridge.receiving), "BUSY: no radio left receiving")
    finally:
        transport.close()
    print("PASS" if not b.failures else f"FAIL: {len(b.failures)}")
    return 1 if b.failures else 0


if __name__ == "__main__":
    sys.exit(main())
