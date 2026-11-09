"""``FilteredPort``: the gateway-side test receive filter (twin of the node firmware's ``rxfilter``).

It wraps ONE radio port (a ``bridge.BridgeRadio`` or anything with ``mesh.RadioPort``'s shape) and
stands between that radio and the protocol stack on top of it. For every received frame:

1. the MIC is checked with the link key (``secure.open_sealed``). A frame that fails (or is not
   sealed) is passed on untouched: the stack rejects it and counts ``auth_failed`` itself, the same
   as with no filter ("the filter does not judge unauthenticated frames", as on the node);
2. a good frame's TX byte (``secure.transmitter``: who put this copy on the air, MIC-covered) is
   looked up in this port's neighbour set; a TX that is not a neighbour is DROPPED here: the stack
   never sees it, so no replay window, relay window, neighbour table or ``_heard_at`` moves for it;
3. everything else is passed on as the very same call (same bytes, same extra arguments).

Default is symmetric and all links physical: the set comes from the topology text
(``topo.parse_links``), which both ends of a link share. An asymmetric rule is NOT physical (a real
radio link is two-way on a bench); it needs ``nonphysical=True`` plus a name, and shows up in the
status and the banner as NONPHYSICAL.

Guards against use outside a test bench (the isolation tests are in
``tests/unit/test_testbed_isolation.py``):

* it lives under ``tests/``, never in the ``cairnmesh`` package, and is imported by nothing there;
* constructing one needs ``topo`` passed explicitly AND ``CAIRN_TESTFILTER=1`` in the environment;
  either missing raises ``FilterError``;
* every live filter is in a registry; ``assert_no_active_filters()`` fails while any is, and a
  full-network test ends with it.

What it cannot do (README.md): it does not make a neighbour's transmission "unheard" for carrier
sense (``busy_until`` stays physical), it does not model RSSI/SNR, and the board still sends it
every frame over USB.
"""

from __future__ import annotations

import hashlib
import logging
import os
import threading
from collections import deque
from dataclasses import dataclass
from typing import Callable, Deque, Dict, Iterable, List, Optional, Set

from cairnmesh.secure import KEY_LEN, MIC_LEN, open_sealed, transmitter

from . import topo as topo_mod

ENV = "CAIRN_TESTFILTER"
LOG = logging.getLogger("cairnmesh.testbed")

_registry_lock = threading.Lock()
_ACTIVE: Set["FilteredPort"] = set()


class FilterError(RuntimeError):
    """The filter was asked to start without everything it needs to be a deliberate test setup."""


@dataclass(frozen=True)
class DropRecord:
    """One dropped frame (only drops are logged; passes are counted)."""

    sender: int  #: origin node (SENDER byte)
    tx: int  #: who transmitted this copy
    kind: int  #: first plain byte (frame kind, ROUTED flag included)
    fcnt: int
    rssi: Optional[float]
    mic_hash: str  #: first 4 bytes (hex) of SHA-256 over the frame's own MIC; never the key or plaintext


def active_filters() -> List["FilteredPort"]:
    """Every filter that is constructed and not closed."""

    with _registry_lock:
        return sorted(_ACTIVE, key=lambda f: (f.node, id(f)))


def assert_no_active_filters(*ports: object) -> None:
    """End of every full-network / production-path test: no test filter is alive.

    ``ports`` are the radio objects the run actually used; a ``FilteredPort`` passed here also fails,
    even after ``close()``. Ports nested inside a stack are not looked into: the registry is what
    covers those (a filter is registered from construction until ``close()``).
    """

    live = active_filters()
    wrapped = [p for p in ports if isinstance(p, FilteredPort)]
    if live or wrapped:
        names = [f.banner for f in live] + [f"port in use is a FilteredPort: {p.banner}" for p in wrapped]
        raise AssertionError("test receive filter active where none is allowed: " + "; ".join(names))


def close_all() -> None:
    """Close every live filter (test teardown). A strict filter that was suspect raises after all are closed."""

    errors: List[FilterError] = []
    for f in active_filters():
        try:
            f.close()
        except FilterError as exc:
            errors.append(exc)
    if errors:
        raise errors[0]


class FilteredPort:
    """Wrap one radio port; see the module docstring."""

    def __init__(
        self,
        port: object,
        *,
        key: bytes,
        node: int,
        topo: Optional[str],
        asym_hear: Optional[Iterable[int]] = None,
        asym_name: Optional[str] = None,
        nonphysical: bool = False,
        log_size: int = 256,
        strict: bool = False,
        suspect_after: int = 5,
    ) -> None:
        if os.environ.get(ENV) != "1":
            raise FilterError(f"{ENV}=1 is not set: the test filter only starts on purpose")
        if topo is None or not str(topo).strip():
            raise FilterError("topo must be passed explicitly (the topology text, e.g. '66-1,1-2')")
        if len(key) != KEY_LEN:
            raise FilterError(f"key must be {KEY_LEN} bytes")
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESCCM
        except ImportError as exc:  # pragma: no cover - depends on the environment
            raise FilterError("the filter checks MICs and needs cryptography") from exc

        links = topo_mod.parse_links(topo)  # TopoError (a ValueError) on a bad text
        if node not in topo_mod.nodes(links):
            raise FilterError(f"node {node} is in no link of {topo_mod.canonical(links)!r}: it would hear nothing")
        if (asym_hear is not None or asym_name is not None) and not (nonphysical and asym_hear is not None and asym_name):
            raise FilterError("an asymmetric rule needs asym_hear, a name and nonphysical=True (it is not a real radio link)")
        if asym_hear is not None and not all(1 <= h <= 254 and h != node for h in asym_hear):
            raise FilterError("asym_hear: node IDs 1..254, not this node")

        self._port = port
        self._aead = AESCCM(bytes(key), tag_length=MIC_LEN)
        self.node = node
        self.links = links
        self.mode = "topo" if asym_hear is None else "asym"
        self.name = asym_name
        self.hear: frozenset = frozenset(topo_mod.neighbours(links, node) if asym_hear is None else asym_hear)
        self._receiver: Optional[Callable[..., None]] = None
        self._lock = threading.Lock()
        self.passed = 0
        self.dropped = 0
        self.unauth = 0
        self.log: Deque[DropRecord] = deque(maxlen=log_size)
        self._closed = False
        self.strict = strict
        self.suspect_after = suspect_after
        self._warned = False
        self.banner = (
            f"TESTFILTER ACTIVE node={node} topo={topo_mod.canonical(links)} "
            + ("mode=topo" if self.mode == "topo" else f"mode=asym NONPHYSICAL name={asym_name} hear={sorted(self.hear)}")
        )
        with _registry_lock:
            _ACTIVE.add(self)
        port.set_receiver(self._on_raw)  # type: ignore[attr-defined]
        LOG.warning(self.banner)

    # ------------------------------------------------------------ RadioPort

    def set_receiver(self, callback: Callable[..., None]) -> None:
        self._receiver = callback
        if self._closed:
            self._port.set_receiver(callback)  # type: ignore[attr-defined]

    def __getattr__(self, name: str):
        # send(), set_radio(), busy_until(), index, ...: everything else is the real radio's.
        if name.startswith("__") or name == "_port":
            raise AttributeError(name)
        return getattr(self._port, name)

    # ------------------------------------------------------------- receiving

    def _on_raw(self, raw: bytes, *rest) -> None:
        callback = self._receiver
        if callback is None:
            return  # as BridgeRadio._deliver with no receiver
        try:
            plain, fcnt = open_sealed(self._aead, bytes(raw))
        except Exception:  # noqa: BLE001 - ValueError (short / plain) or InvalidTag: the stack judges it
            with self._lock:
                self.unauth += 1
            callback(raw, *rest)
            self._watch()
            return
        tx = transmitter(raw)
        if tx in self.hear:
            with self._lock:
                self.passed += 1
            callback(raw, *rest)
            return
        rssi = rest[0] if rest and isinstance(rest[0], (int, float)) else None
        record = DropRecord(
            sender=plain[1],
            tx=tx,
            kind=plain[0],
            fcnt=fcnt,
            rssi=None if rssi is None else float(rssi),
            mic_hash=hashlib.sha256(bytes(raw[-MIC_LEN:])).hexdigest()[:8],
        )
        with self._lock:
            self.dropped += 1
            self.log.append(record)

    # ------------------------------------------------- fail-open watch

    def suspect(self) -> Optional[str]:
        """Why this filter may be passing everything (``None``: nothing odd).

        Unauthenticated frames go through untouched, so a wrong key (or a stack on another key, an
        application key) makes the filter silently do nothing. Most of what it saw failing the MIC,
        and at least ``suspect_after`` of it, is that sign.
        """

        with self._lock:
            seen = self.passed + self.dropped
            if self.unauth >= self.suspect_after and self.unauth > seen:
                return (
                    f"{self.unauth} of {self.unauth + seen} frames failed the MIC (passed {self.passed}, dropped "
                    f"{self.dropped}): wrong key? the filter is passing them all, so no topology is in force"
                )
        return None

    def _watch(self) -> None:
        reason = self.suspect()
        if reason is None:
            return
        if not self._warned:
            self._warned = True
            LOG.warning("TESTFILTER SUSPECT node=%d: %s", self.node, reason)
        if self.strict:
            raise FilterError(f"node {self.node}: {reason}")

    def check(self) -> None:
        """Raise ``FilterError`` if the filter looks like it is passing everything (end of a bench run)."""

        reason = self.suspect()
        if reason is not None:
            raise FilterError(f"node {self.node}: {reason}")

    # ---------------------------------------------------------------- status

    def status(self) -> Dict[str, object]:
        with self._lock:
            out: Dict[str, object] = {
                "node": self.node,
                "mode": self.mode,
                "name": self.name,
                "hear": sorted(self.hear),
                "passed": self.passed,
                "dropped": self.dropped,
                "unauth": self.unauth,
            }
        out["suspect"] = self.suspect()
        return out

    def close(self) -> None:
        """Take the filter out: the radio talks to the stack directly, the registry forgets it."""

        with _registry_lock:
            _ACTIVE.discard(self)
        if not self._closed:
            self._closed = True
            if self._receiver is not None:
                self._port.set_receiver(self._receiver)  # type: ignore[attr-defined]
            if self.strict:
                self.check()  # a strict filter that was passing everything fails the run that used it
