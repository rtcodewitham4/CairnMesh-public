"""Coverage-guided fuzzing (libFuzzer / AFL method) with branch coverage from ``sys.monitoring``.

Targets:

* ``decode``: ``frames.decode_frame`` / ``decode_routed`` on raw bytes. Oracle: only
  ``ValueError``; a decoded frame encodes back to the same bytes (canonical form).
* ``forge``: bit flips / truncation / extension of sealed radio frames. Oracle: never
  accepted by ``open_sealed`` (a 4-byte MIC: 2^-32 per try).
* ``node`` / ``gateway``: structure-aware: fuzzed plain frames, sealed properly with the
  network key (and the application key when the attacker holds one), put on the air at a
  device or at the gateway of a small mesh, then the mesh runs. Oracle: no exception out
  of the main loops, no observer error, the device's memory bounds hold.

    python tests/property/fuzz.py TARGET SECONDS SEED   # cap its memory (prlimit --as=...); or via tests/property/test_properties.py
"""

from __future__ import annotations

import json
import random
import signal
import sys
import time
import traceback
from pathlib import Path

from cairnmesh import frames as F
from cairnmesh import secure as S

PKG_DIR = str(Path(F.__file__).resolve().parent)
INTERESTING = (0x00, 0x01, 0x02, 0x0F, 0x10, 0x11, 0x12, 0x20, 0x40, 0x7F, 0x80, 0x81, 0xFE, 0xFF)


HANG = float(__import__("os").environ.get("FUZZ_HANG", "30"))


class Hang(Exception):
    pass


def _alarm(signum, frame):
    raise Hang()


class Coverage:
    def __init__(self) -> None:
        self.edges: set = set()
        mon = sys.monitoring
        self.tool = mon.COVERAGE_ID
        mon.use_tool_id(self.tool, "audit-fuzz")
        mon.register_callback(self.tool, mon.events.BRANCH, self._branch)
        mon.set_events(self.tool, mon.events.BRANCH)

    def _branch(self, code, src, dst):
        if not code.co_filename.startswith(PKG_DIR):
            return sys.monitoring.DISABLE
        self.edges.add((code.co_filename, code.co_firstlineno, src, dst))


def mutate(rng: random.Random, data: bytes, corpus: list[bytes]) -> bytes:
    b = bytearray(data)
    for _ in range(rng.choice((1, 1, 1, 2, 3, 4))):
        op = rng.randrange(9)
        if op == 0 and b:
            i = rng.randrange(len(b)); b[i] ^= 1 << rng.randrange(8)
        elif op == 1 and b:
            b[rng.randrange(len(b))] = rng.choice(INTERESTING)
        elif op == 2 and b:
            b[rng.randrange(len(b))] = rng.randrange(256)
        elif op == 3 and len(b) < 64:
            b.insert(rng.randrange(len(b) + 1), rng.randrange(256))
        elif op == 4 and b:
            del b[rng.randrange(len(b))]
        elif op == 5 and corpus:
            other = rng.choice(corpus)
            cut = rng.randrange(len(b) + 1)
            b = b[:cut] + other[rng.randrange(len(other) + 1):]
        elif op == 6 and b:
            b[0] = rng.choice(list(F.FrameKind)) | rng.choice((0, 0, F.ROUTED))
        elif op == 7 and len(b) > 1:
            i = rng.randrange(len(b)); j = rng.randrange(i, len(b) + 1)
            b[j:j] = b[i:j]
        elif op == 8 and b:
            b = b[: rng.randrange(len(b) + 1)]
    return bytes(b[:80])


# ------------------------------------------------------------------ targets


def _sample_frames() -> list[bytes]:
    """Valid plain frames of every kind, recorded from a short normal run of the mesh."""

    seen: list[bytes] = []
    original = S.SecureTransport.seal_frame

    def spy(self, data):
        seen.append(bytes(data))
        return original(self, data)

    S.SecureTransport.seal_frame = spy
    try:
        clock, network, controller = _mesh(3, full=True)
        from cairnmesh import session_bench as sb

        sb.opened(clock, network, controller, 3, 60.0)
        for v in range(4):
            network.sessions.request(3, 1, v)
            sb.run(clock, network, controller, 6.0)
        network.sessions.request(2, 2, 2)
        sb.run(clock, network, controller, 60.0)
    finally:
        S.SecureTransport.seal_frame = original
    unique = list(dict.fromkeys(seen))
    for path in Path(__file__).resolve().parent.parent.joinpath("vectors").glob("*.json"):
        text = path.read_text(encoding="utf-8")
        for token in json.loads(text).__repr__().split("'"):
            if len(token) >= 8 and len(token) % 2 == 0 and all(c in "0123456789abcdef" for c in token):
                unique.append(bytes.fromhex(token))
    return list(dict.fromkeys(unique))


def _mesh(n: int, full: bool, seed: int = 1):
    from cairnmesh import session_bench as sb
    from cairnmesh.controller import DEFAULT_CONFIRM_WINDOW
    from cairnmesh.mesh_sim import line, planned

    extra = dict(hello_interval=30.0, discovery_options={}) if full else {}
    return sb.build(line(n), seed=seed, gateway_options={"idle_timeout": None},
                    confirm_window=DEFAULT_CONFIRM_WINDOW, **planned(), **extra)


class Dummy:
    def set_receiver(self, cb):
        pass

    def send(self, data):
        pass


def _bounds(network) -> str | None:
    for node, dev in network.devices.items():
        if len(dev._recent) > dev.max_senders:
            return f"device {node}: {len(dev._recent)} senders > max_senders"
        for sender, recs in dev._recent.items():
            closed = sum(not r[3] for r in recs)
            if closed > dev.dedup_window or len(recs) - closed > dev.max_open:
                return f"device {node} sender {sender}: closed={closed} open={len(recs) - closed}"
        if len(dev._deferred) > dev.max_deferred:
            return f"device {node}: deferred {len(dev._deferred)}"
    for node, relay in network.relays.items():
        if relay.stats.get("observer_errors"):
            return f"relay {node}: observer_errors={relay.stats['observer_errors']}"
    return None


def _signature(exc: BaseException) -> str:
    frames = [f for f in traceback.extract_tb(exc.__traceback__) if f.filename.startswith(PKG_DIR)]
    where = f"{Path(frames[-1].filename).name}:{frames[-1].lineno}" if frames else "?"
    return f"{type(exc).__name__}@{where}"


def run(target: str, seconds: float, seed: int) -> dict:
    rng = random.Random(seed)
    findings: dict[str, dict] = {}
    execs = 0
    corpus = _sample_frames()
    seeds = len(corpus)
    cov = Coverage()

    def find(key, **info):
        f = findings.setdefault(key, {"count": 0, **info})
        f["count"] += 1

    end = time.monotonic() + seconds
    if target == "decode":
        while time.monotonic() < end:
            data = mutate(rng, rng.choice(corpus), corpus)
            before = len(cov.edges)
            execs += 1
            try:
                if F.is_routed(data):
                    frame, route = F.decode_routed(data)
                    again = F.add_route(frame.encode(), route)
                else:
                    frame = F.decode_frame(data)
                    again = frame.encode()
                if again != data:
                    find(f"noncanonical:{data[0]:#04x}", input=data.hex(), again=again.hex())
            except ValueError:
                pass
            except Exception as exc:  # noqa: BLE001
                find(_signature(exc), input=data.hex(), error=f"{type(exc).__name__}: {exc}")
            if len(cov.edges) > before:
                corpus.append(data)
    elif target == "forge":
        aead = S.SecureTransport(Dummy(), bytes(range(16)))._aead
        fcnt = 1
        while time.monotonic() < end:
            plain = rng.choice(corpus)
            if not plain or plain[0] & S.SECURE:
                continue
            fcnt += 1
            sealed = S.seal(aead, plain, fcnt, rng.randrange(256))
            bad = mutate(rng, sealed, [])
            if bad == sealed:
                continue
            execs += 1
            try:
                S.open_sealed(aead, bad)
            except Exception:  # noqa: BLE001 - rejected, as it must be
                continue
            find("forgery_accepted", input=bad.hex(), original=sealed.hex())
    else:
        from cairnmesh import session_bench as sb
        from cairnmesh.controller import NodeConfirming

        full = rng.random() < 0.5
        clock, network, controller = _mesh(4, full=full, seed=seed)
        ctl_id = network.controller_id
        attackers = [
            S.SecureTransport(Dummy(), sb.KEY, initial_fcnt=3_000_000_000, app=network.app_keys),  # holds every key
            S.SecureTransport(Dummy(), sb.KEY, initial_fcnt=3_000_000_000),  # network key only (a relay)
        ]
        inject_at = [2, 3] if target == "node" else [ctl_id]
        built = time.monotonic()
        while time.monotonic() < end:
            if execs and execs % 400 == 0 or time.monotonic() - built > 120:
                full = rng.random() < 0.5
                clock, network, controller = _mesh(4, full=full, seed=rng.randrange(10**6))
                built = time.monotonic()
            plain = bytearray(mutate(rng, rng.choice(corpus), corpus))
            if len(plain) < 3:
                continue
            if target == "gateway":
                plain[2] = ctl_id if rng.random() < 0.8 else plain[2]
                plain[1] = rng.choice((1, 2, 3, 4, plain[1]))
            else:
                plain[2] = rng.choice((2, 3, 4, F.BROADCAST_ID, plain[2]))
            attacker = rng.choice(attackers)
            try:
                sealed = attacker.seal_frame(bytes(plain))
            except Exception:  # noqa: BLE001 - the attacker's own encoder refused it
                continue
            sealed = S.reseal(attacker._aead, sealed, rng.choice((1, 2, 3, 4, ctl_id)))
            node = rng.choice(inject_at)
            before = len(cov.edges)
            execs += 1
            last = __import__("os").environ.get("FUZZ_LAST")
            if last:
                with open(last, "w") as fh:
                    fh.write(json.dumps({"execs": execs, "plain": bytes(plain).hex(), "sealed": sealed.hex(),
                                         "node": node, "full": full, "sim_time": clock.now}))
            signal.signal(signal.SIGALRM, _alarm)
            signal.setitimer(signal.ITIMER_REAL, HANG)
            t0 = time.monotonic()
            try:
                network.medium.port(node)._deliver(sealed, -70.0 - rng.random() * 50)
                sb.run(clock, network, controller, rng.choice((0.02, 0.02, 0.1, 0.1, 0.4, 1.5)))
                if rng.random() < 0.05:
                    try:
                        network.sessions.request(rng.choice((2, 3, 4)), 1, rng.randrange(256))
                    except NodeConfirming:
                        pass  # the harness asked too early: not a finding
                problem = _bounds(network)
            except (Hang, MemoryError) as exc:
                if isinstance(exc, MemoryError):
                    network = controller = None
                    import gc; gc.collect()
                find("hang" if isinstance(exc, Hang) else "memory", input=bytes(plain).hex(), sealed=sealed.hex(), node=node, full=full,
                     sim_time=clock.now, trace=traceback.format_exc()[-3000:])
                clock, network, controller = _mesh(4, full=full, seed=rng.randrange(10**6))
                built = time.monotonic()
                continue
            except Exception as exc:  # noqa: BLE001 - a crash is a finding
                signal.setitimer(signal.ITIMER_REAL, 0)
                find(_signature(exc), input=bytes(plain).hex(), error=f"{type(exc).__name__}: {exc}",
                     trace=traceback.format_exc()[-1500:])
                clock, network, controller = _mesh(4, full=full, seed=rng.randrange(10**6))
                built = time.monotonic()
                continue
            signal.setitimer(signal.ITIMER_REAL, 0)
            slow = time.monotonic() - t0
            if slow > 5:
                find("slow_input", input=bytes(plain).hex(), seconds=round(slow, 1), node=node, full=full)
            if problem:
                find("bound:" + problem.split(":")[0], input=bytes(plain).hex(), detail=problem)
                clock, network, controller = _mesh(4, full=full, seed=rng.randrange(10**6))
                built = time.monotonic()
            if len(cov.edges) > before:
                corpus.append(bytes(plain))
    sys.monitoring.set_events(cov.tool, 0)
    sys.monitoring.free_tool_id(cov.tool)
    return {"target": target, "seed": seed, "execs": execs, "edges": len(cov.edges), "seeds": seeds,
            "corpus": len(corpus), "findings": findings}


if __name__ == "__main__":
    print(json.dumps(run(sys.argv[1], float(sys.argv[2]), int(sys.argv[3])), indent=1))
