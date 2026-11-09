"""The firmware on the real NUCLEO dual-radio bench (``-m hardware``; skipped without the board).

What each test checks is what the bench programs print (``tests/hardware/board.py``
parses it); the numbers worth keeping go to ``firmware/results/hw-test-<date>.json``.
Low powers except where full power is the point (bus under a carrier, levels):
the antennas must be on. The board is left running the
gateway firmware.
"""

from __future__ import annotations

import math
import subprocess
import sys
import time

import pytest

from board import FIRMWARE, ROOT, done

pytestmark = pytest.mark.hardware


def log(name: str):
    return FIRMWARE / "results" / f"hw-{name}.log"


def test_power_on_check(board, record):
    """radiocheck (no transmission): pins, NSS sharing, status, sync word, 2000 reads each."""

    lines = board.run("radiocheck", 30, log=log("radiocheck"))
    end = done(lines)
    reads = {l.head[0]: l.int("bad") for l in lines if l.tag == "L"}
    record["radiocheck"] = {"pass": end.int("pass"), "fail": end.int("fail"), "bad_reads": reads}
    assert end.int("fail") == 0 and reads == {"0": 0, "1": 0}


def test_driver_bus_and_link(board, record):
    """radiotest: 200000 reads per radio at 6 and 3 MHz, 50 frames each way, DIO1 edges."""

    lines = board.run("radiotest", 60, log=log("radiotest"))
    end = done(lines)
    bus = [
        {
            "spi_hz": l.int("spi_hz"),
            "bad": [l.int("bad0"), l.int("bad1")],
            "retries": [l.int("retries0"), l.int("retries1")],
        }
        for l in lines
        if l.tag == "B"
    ]
    links = [{"dir": " ".join(l.head), "ok": l.int("ok"), "sent": l.int("sent"), "rssi": l.get("rssi")} for l in lines if l.tag == "D"]
    record["radiotest"] = {"pass": end.int("pass"), "fail": end.int("fail"), "bus": bus, "links": links}
    assert end.int("fail") == 0
    assert all(b["bad"] == [0, 0] for b in bus)
    assert [d["ok"] for d in links] == [50, 50]


def test_bus_under_a_full_power_carrier(board, record):
    """radioiso victim: one radio sends a carrier −9…+22 dBm, the other reads its bus 5000 times (receiving, standby)."""

    lines = board.run("radioiso", 40, ldflags="-X main.mode=victim", log=log("radioiso-victim"))
    done(lines)
    rows = [
        {
            "tx": l.int("tx"),
            "p_dbm": l.int("p_dbm"),
            "state": l.get("state"),
            "bad": l.int("bad"),
            "retries": l.int("retries"),
        }
        for l in lines
        if l.tag == "V"
    ]
    record["bus_under_carrier"] = rows
    assert len(rows) == 2 * (1 + 2 * 8)
    assert sum(r["bad"] for r in rows) == 0, [r for r in rows if r["bad"]]


def test_levels_and_isolation(board, record):
    """radioiso sweep: carrier −9 / 0 / +10 / +22 dBm, the other radio's RSSI at offsets; no bus fault."""

    lines = board.run("radioiso", 70, log=log("radioiso-sweep"))
    end = done(lines)
    points = {}
    for l in lines:
        if l.tag == "M" and "p_dbm" in l.fields:
            points[(l.int("tx"), l.int("p_dbm"), l.int("offset_khz"))] = (l.int("floor") / 10, l.int("on") / 10)
    summary = {}
    for tx in (0, 1):
        coupling = {p: points[(tx, p, 0)][1] for p in (-9, 0, 10, 22)}
        noise = {}
        for p in (-9, 0, 10, 22):
            floor, on = points[(tx, p, 10500)]
            rise = on - floor
            added = floor + 10 * math.log10(10 ** (rise / 10) - 1) if rise > 0.05 else None
            noise[p] = None if added is None else round(added - coupling[p], 1)
        summary[f"tx{tx}"] = {
            "rssi_at_carrier_dbm": coupling,
            "isolation_db_at_-9": round(-9 - coupling[-9], 1),
            "noise_10.5MHz_dBc": noise,
        }
    record["isolation"] = summary
    assert end.int("faults0") == 0 and end.int("faults1") == 0
    for tx in (0, 1):
        c = summary[f"tx{tx}"]["rssi_at_carrier_dbm"]
        # The carrier's level follows the power (before the RSSI saturates near 0 dBm).
        assert abs((c[0] - c[-9]) - 9) <= 2 and abs((c[10] - c[0]) - 10) <= 2, c


def test_gateway_bridge(board, record):
    """tools/bridge_bench.py on the gateway firmware: both ways, rules 1/2/4, TUNE, long preamble, counters."""

    board.flash("gateway")
    time.sleep(1.0)
    run = subprocess.run(
        [sys.executable, str(FIRMWARE / "tools" / "bridge_bench.py"), board.port],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=300,
    )
    log("bridge-bench").write_text(run.stdout + run.stderr)
    fails = [l.strip() for l in run.stdout.splitlines() if "FAIL" in l]
    record["bridge_bench"] = {"exit": run.returncode, "fails": fails}
    assert run.returncode == 0, run.stdout[-3000:]


def _status_until(bridge, predicate, timeout):
    end = time.monotonic() + timeout
    seen = []
    while time.monotonic() < end:
        status = bridge.ping(timeout=0.3)
        seen.append(status)
        if status is not None and predicate(status):
            return status, seen
        time.sleep(0.2)
    return None, seen


def test_radio_recovers_after_losing_its_setup(board, record):
    """GATEWAY_BENCH_FAULT=radio1: radio 1's reset pulses 5 s after boot; frames pass again."""

    board.flash("gateway", make_vars={"GATEWAY_BENCH_FAULT": "radio1"})
    time.sleep(1.0)
    bridge, transport = board.bridge()
    try:
        started = time.monotonic()
        status, _ = _status_until(bridge, lambda s: s.radios[1].resets >= 1 and s.radios[1].healthy, 12)
        took = time.monotonic() - started
        heard = []
        bridge.ports[1].set_receiver(lambda frame, rssi: heard.append(frame))
        for i in range(5):
            bridge.ports[0].send(bytes([0x52, i]) + b"recovered")
            time.sleep(0.2)
        time.sleep(0.5)
    finally:
        transport.close()
    record["radio_recovery"] = {
        "resets": None if status is None else status.radios[1].resets,
        "seconds_from_open": round(took, 2),
        "frames_after": len(heard),
    }
    assert status is not None, "radio 1 not reset"
    assert len(heard) == 5


def test_watchdog_resets_a_stuck_board(board, record):
    """GATEWAY_BENCH_FAULT=hang: the loop stops 5 s after boot; the watchdog resets the board."""

    board.flash("gateway", make_vars={"GATEWAY_BENCH_FAULT": "hang"})
    time.sleep(1.0)
    bridge, transport = board.bridge()
    try:
        first, _ = _status_until(bridge, lambda s: True, 3)
        gone_at = None
        start = time.monotonic()
        while time.monotonic() - start < 10:
            if bridge.ping(timeout=0.3) is None:
                gone_at = time.monotonic()
                break
            time.sleep(0.2)
        back, _ = _status_until(bridge, lambda s: s.counters["watchdog_boot"] == 1, 8)
        back_at = time.monotonic()
    finally:
        transport.close()
    record["watchdog"] = {
        "status_before": None if first is None else first.counters["watchdog_boot"],
        "silent_seconds": None if gone_at is None or back is None else round(back_at - gone_at, 2),
        "watchdog_boot_after": None if back is None else back.counters["watchdog_boot"],
    }
    assert first is not None and first.counters["watchdog_boot"] == 0
    assert gone_at is not None, "the loop never stopped"
    assert back is not None, "no STATUS with watchdog_boot after the hang"
