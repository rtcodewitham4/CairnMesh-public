"""U083 side of the wle5link bench pairing: listen and send test frames through the gateway bridge.

The U083 + SX1262 gateway board runs cmd/gateway (``make flash CMD=gateway``); this script drives it
over the ST-LINK serial port with the bridge protocol (``cairnmesh/bridge.py``), the way
``bridge_bench.py`` does. Run from the repository root while the WLE5 runs cmd/wle5link:

    .venv/bin/python firmware/tools/wle5link_peer.py /dev/ttyACM0 [--seconds 60] [--period 5] [--radio 0]

It prints every frame either radio hears ("heard radio=<n> n=<len> rssi=<dBm> hex=<first 16 bytes>",
the same layout as the wle5link rx line) and sends radiolink's test frame ("CAIRNMESH-BENCH-" + a sequence
byte) from ``--radio`` every ``--period`` s. At the end: ``peer sum heard=<n> from_wle5=<n> sent=<n> tx_ok=<n>``;
exit 0 when it heard at least one frame from the WLE5 (a test frame whose sequence byte it did not send
itself is not distinguishable by content, so ``from_wle5`` counts frames heard by the radio that did NOT
send in the last second, which excludes the other gateway radio's copy of its own frame).
Air parameters are the gateway's channel 0 (433.5 MHz, SF7, 125 kHz); nothing is changed here.

NOT RUN: written without the bench (2026-11-06); the bench agent runs it.
"""

from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.dirname(__file__))

from cairnmesh.bridge import TX_OK, GatewayBridge, TxDone  # noqa: E402
from cairnmesh.serial_link import SerialTransport  # noqa: E402
from serial_bench import Port  # noqa: E402

TAG = b"CAIRNMESH-BENCH-"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("port")
    parser.add_argument("--seconds", type=float, default=60.0)
    parser.add_argument("--period", type=float, default=5.0, help="seconds between test frames")
    parser.add_argument("--radio", type=int, choices=(0, 1), default=0, help="which gateway radio sends")
    args = parser.parse_args()

    transport = SerialTransport(Port(args.port))
    bridge = GatewayBridge(transport, on_error=lambda exc: print("  error:", exc))
    transport.start()
    counts = {"heard": 0, "from_wle5": 0, "sent": 0, "tx_ok": 0}
    last_tx = [0.0]

    def heard(radio: int, frame: bytes, rssi: float) -> None:
        counts["heard"] += 1
        # The sending radio hears nothing of its own frame; the other radio hears it (same board,
        # a few cm apart), so a frame heard within 1 s of our own send is ours.
        ours = time.monotonic() - last_tx[0] < 1.0
        if not ours:
            counts["from_wle5"] += 1
        print(f"heard radio={radio} n={len(frame)} rssi={rssi:.0f} hex={frame[:16].hex()}"
              + (" (our own frame)" if ours else ""), flush=True)

    def done(d: TxDone) -> None:
        if d.result == TX_OK:
            counts["tx_ok"] += 1
        print(f"tx_done radio={d.radio} result={d.result} air_ms={d.air * 1000:.1f}", flush=True)

    for i, port in enumerate(bridge.ports):
        port.set_receiver(lambda frame, rssi, i=i: heard(i, frame, rssi))
    bridge.on_tx_done = done
    try:
        status = bridge.ping(timeout=2.0)
        if status is None:
            print("FAIL: gateway STATUS did not answer")
            return 1
        print("gateway:", [(r.healthy, r.channel, r.sf) for r in status.radios], flush=True)
        end = time.monotonic() + args.seconds
        seq = 0
        next_tx = time.monotonic() + 1.0
        while time.monotonic() < end:
            now = time.monotonic()
            if now >= next_tx:
                next_tx += args.period
                last_tx[0] = now
                counts["sent"] += 1
                bridge.ports[args.radio].send(TAG + bytes([seq & 0xFF]))
                seq += 1
            time.sleep(0.02)
    finally:
        transport.close()
    print(f"peer sum heard={counts['heard']} from_wle5={counts['from_wle5']} "
          f"sent={counts['sent']} tx_ok={counts['tx_ok']}")
    return 0 if counts["from_wle5"] > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
