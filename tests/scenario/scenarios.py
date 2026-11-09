"""Tricky fault scenarios on the mesh simulator.

* ``result_loss``: slow actions (ACCEPTED + RESULT) while every RESULT frame is lost for a
  while; the controller takes ACCEPTED as final and moves on. Does the device's open-record
  limit (``max_open``) then fill with records nobody closes, so that later slow commands are
  refused (NOT_RUN) long after the channel is fine again?
* ``nonce_audit``: the extreme scenarios of ``extreme_1022`` with every network-layer seal
  recorded: one nonce must always carry the same header and plain bytes.

    python tests/scenario/scenarios.py NAME SEED   # or through pytest -m slow: tests/scenario/test_scenarios.py
"""

from __future__ import annotations

import json
import sys

from cairnmesh import mesh as M
from cairnmesh import secure as S
from cairnmesh import session_bench as sb
import extreme_1022 as X

RESULT = 0x12


def result_loss(kind: str, seed: int, lossy: int = 4, after: int = 8, outage: float = 6 * 3600.0) -> dict:
    clock, network, controller, targets, _, ledger = X._setup(kind, seed, 0.05, device_options={"action_time": 12.0})
    target = targets[0]
    blocked = {"on": False, "dropped": 0}
    transmit = network.medium._transmit

    def lossy_transmit(source, data, preamble=8):
        if blocked["on"] and data and (data[0] & 0x1F) == RESULT:
            blocked["dropped"] += 1
            return
        transmit(source, data, preamble)

    network.medium._transmit = lossy_transmit
    sb.opened(clock, network, controller, target, 120.0)
    blocked["on"] = True
    phase1 = []
    for _ in range(lossy):
        phase1.append(ledger.command(clock, network, controller, target, give_up=120.0))
        sb.run(clock, network, controller, 30.0)  # each action over before the next: no max_open by throughput
    sb.run(clock, network, controller, outage)  # RESULT retries keep failing
    blocked["on"] = False
    sb.run(clock, network, controller, 60.0)
    dev = network.devices[target]
    open_before = sum(r[3] for recs in dev._recent.values() for r in recs)
    refused0 = dev.stats["refused_busy"]
    sb.opened(clock, network, controller, target, 120.0)
    phase2 = []
    for _ in range(after):
        phase2.append(ledger.command(clock, network, controller, target, give_up=600.0))
        sb.run(clock, network, controller, 30.0)
    out = ledger.audit()
    out.update(
        phase1=phase1, phase2=phase2, results_dropped=blocked["dropped"], open_records=open_before,
        refused_after_channel_ok=dev.stats["refused_busy"] - refused0,
        results_abandoned=dev.stats["results_abandoned"],
    )
    return out


def nonce_audit(name: str, seed: int) -> dict:
    seen: dict[bytes, bytes] = {}
    clash = {"n": 0, "example": None}
    original = S.seal

    def recording_seal(aead, plain, fcnt, tx=None):
        sealed = original(aead, plain, fcnt, tx)
        header = sealed[: S.HEADER]
        content = header + bytes(plain)
        old = seen.setdefault(S._nonce(header), content)
        if old != content:
            clash["n"] += 1
            clash["example"] = clash["example"] or (old.hex(), content.hex())
        return sealed

    S.seal = M.seal = recording_seal
    try:
        fn, jobs = X.SCENARIOS[name]
        out = fn(jobs([seed])[0])
    finally:
        S.seal = M.seal = original
    out.update(nonces=len(seen), nonce_clash=clash["n"], clash_example=clash["example"])
    return out


if __name__ == "__main__":
    name, seed = sys.argv[1], int(sys.argv[2])
    if name == "result_loss":
        print(json.dumps(result_loss("random40", seed), default=str, indent=1))
    else:
        print(json.dumps(nonce_audit(name, seed), default=str, indent=1))
