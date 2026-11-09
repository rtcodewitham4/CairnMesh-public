"""Gateway E4g behaviour recorder for pytest.

Set ``E4G_OUT`` to a file; ``tests/conftest.py`` then imports this module.
Explicitly passing ``-p tests.tools.e4g_plugin`` also works.  With no
``E4G_OUT`` this module is not imported by normal test runs and installs no
hooks or monkeypatches.

Always invoke this from the repository root as ``python -m pytest``.  Under
``E4G_OUT``, the bare ``pytest`` entry point cannot import
``tests.tools.e4g_plugin`` and raises ``ImportError``.

Each test that successfully constructed a Gateway contributes one JSON line,
with one entry per Gateway.  Wall-clock event ``at`` values are reduced to
zero and gateway timestamps are expressed relative to that Gateway's clock,
so the output is byte-for-byte reproducible while command payloads,
``history``, ``stats``, event fields and per-radio send bytes remain exact.
Timestamps and floats have nine decimal places.  Unseeded ``MeshNetwork``
instances get ``seed=0`` and a deterministic clock in E4g mode; otherwise
medium entropy, controller ids and wall-clock progress make even unchanged
code incomparable.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
from collections import deque
from pathlib import Path
from typing import Any, Iterable

import pytest

_OUTPUT = os.environ.get("E4G_OUT")
_ENABLED = bool(_OUTPUT)
_ORIGINAL_INIT = None
_ORIGINAL_ATTACH = None
_ORIGINAL_NETWORK_INIT = None
_CURRENT: list[dict[str, Any]] = []
_RECORDS: dict[str, list[dict[str, Any]]] = {}


class _SimClock:
    """A fixed clock for simulations that did not pass one explicitly."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _number(value: Any) -> float:
    """Round a float to the nine-decimal precision used by the fingerprint."""

    return round(float(value), 9)


def _relative(value: Any, origin: float) -> Any:
    """Return a timestamp relative to the Gateway's clock."""

    if value is None:
        return None
    return _number(float(value) - origin)


def _jsonable(value: Any, origin: float) -> Any:
    """Convert a value to deterministic JSON data."""

    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return _number(value)
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, (list, tuple, deque)):
        return [_jsonable(item, origin) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted((_jsonable(item, origin) for item in value), key=repr)
    if isinstance(value, dict):
        return {str(key): _jsonable(item, origin) for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))}
    if dataclasses.is_dataclass(value):
        return {
            "type": type(value).__name__,
            "fields": {
                field.name: _jsonable(getattr(value, field.name), origin)
                for field in dataclasses.fields(value)
            },
        }
    return re.sub(r"0x[0-9a-fA-F]+", "0x…", repr(value))


def _link_test(test: Any, origin: float) -> dict[str, Any]:
    """Summarize every public and diagnostic field of a LinkTest."""

    return {
        "target": test.target,
        "rounds": test.rounds,
        "started": _relative(test.started, origin),
        "gap": _number(test.gap),
        "margin_needed": _number(test.margin_needed),
        "base_sf": test.base_sf,
        "results": [[_relative(at, origin), None if rtt is None else _number(rtt), sf] for at, rtt, sf in test.results],
        "pending": None if test.pending is None else list(test.pending),
        "sent_at": _relative(test.sent_at, origin),
        "next_at": _relative(test.next_at, origin),
        "progress_at": _relative(test.progress_at, origin),
        "breaks": test.breaks,
        "path": list(test.path),
        "margins": [_number(margin) for margin in test.margins],
        "paths": test.paths,
        "sf": test.sf,
        "expected": _number(test.expected),
        "worst": _number(test.worst),
        "finished": _relative(test.finished, origin),
        "stopped": test.stopped,
        "done": test.done,
        "answered": test.answered,
        "loss": None if test.loss is None else _number(test.loss),
        "verdict": test.verdict,
    }


def _acceptance(acceptance: Any, origin: float) -> dict[str, Any]:
    """Summarize an Acceptance, including its link-test evidence."""

    return {
        "target": acceptance.target,
        "at": _relative(acceptance.at, origin),
        "passed": acceptance.passed,
        "path": list(acceptance.path),
        "hops": [_jsonable(hop, origin) for hop in acceptance.hops],
        "test": None if acceptance.test is None else _link_test(acceptance.test, origin),
        "reasons": list(acceptance.reasons),
        "remedies": list(acceptance.remedies),
        "warnings": list(acceptance.warnings),
        "saved": acceptance.saved,
    }


def _event_repr(event: Any, origin: float) -> str:
    """Canonical event repr with nondeterministic wall-clock fields removed."""

    fields = {}
    for field in dataclasses.fields(event):
        value = getattr(event, field.name)
        if field.name == "at":
            value = 0.0
        elif field.name == "test" and hasattr(value, "verdict"):
            value = _link_test(value, origin)
        elif field.name == "acceptance" and hasattr(value, "hops"):
            value = _acceptance(value, origin)
        else:
            value = _jsonable(value, origin)
        fields[field.name] = value
    return f"{type(event).__name__}({json.dumps(fields, ensure_ascii=False, separators=(',', ':'))})"


def _nodes(gateway: Any, origin: float) -> list[dict[str, Any]]:
    """Summarize the mutable node book without routes' object identities."""

    return [
        {
            "node_id": node_id,
            "route": list(known.route),
            "links": [_jsonable(link, origin) for link in known.links],
            "seen_at": _relative(known.seen_at, origin),
            "how": known.how,
            "state": known.state,
            "neighbours": [_jsonable(hop, origin) for hop in known.neighbours],
            "measured": known.measured,
            "last_hops": known.last_hops,
            "hops": known.hops,
        }
        for node_id, known in sorted(gateway.nodes.items())
    ]


def _health(gateway: Any, origin: float) -> list[dict[str, Any]]:
    """Summarize layer-2 health, including the attempt window."""

    return [
        {
            "target": target,
            "reasons": sorted(health.reasons.items()),
            "since": _relative(health.since, origin),
            "attempts": list(health.attempts),
            "bad_paths": health.bad_paths,
            "failed_probes": health.failed_probes,
            "ok": health.ok,
        }
        for target, health in sorted(gateway.health.items())
    ]


def _capture_send(
    transport: Any,
    bucket: list[list[Any]],
    clock: Any,
    origin: float,
) -> dict[str, Any]:
    """Wrap one transport's ``send`` and return the data needed to restore it."""

    had_own = "send" in getattr(transport, "__dict__", {})
    previous = getattr(transport, "send")
    original = previous

    def send(data: bytes, *args: Any, **kwargs: Any) -> Any:
        bucket.append([_relative(clock(), origin), bytes(data).hex()])
        return original(data, *args, **kwargs)

    setattr(transport, "send", send)
    return {"target": transport, "method": "send", "had_own": had_own, "previous": previous}


def _capture_set_radio(
    relay: Any,
    bucket: list[list[Any]],
    clock: Any,
    origin: float,
) -> dict[str, Any]:
    """Wrap one relay's ``set_radio`` and record successful tuning changes."""

    had_own = "set_radio" in getattr(relay, "__dict__", {})
    previous = getattr(relay, "set_radio")
    original = previous

    def set_radio(channel: int, sf: int, *args: Any, **kwargs: Any) -> Any:
        at = _relative(clock(), origin)
        result = original(channel, sf, *args, **kwargs)
        bucket.append([at, channel, sf])
        return result

    setattr(relay, "set_radio", set_radio)
    return {"target": relay, "method": "set_radio", "had_own": had_own, "previous": previous}


def _record_gateway(gateway: Any) -> dict[str, Any]:
    """Install a recorder immediately after a Gateway is constructed."""

    origin = float(gateway.clock())
    record: dict[str, Any] = {
        "origin": origin,
        "gateway": gateway,
        "events": [],
        "sends": [],
        "restores": [],
        "subscribed": set(),
    }
    for radio, arm in enumerate(gateway._arms):
        bucket: list[list[Any]] = []
        radio_changes: list[list[Any]] = []
        record["sends"].append(
            {
                "radio": radio,
                "channel": arm.channel,
                "bytes": bucket,
                "set_radio": radio_changes,
            }
        )
        try:
            record["restores"].append(
                _capture_send(arm.transport, bucket, gateway.clock, origin)
            )
        except (AttributeError, TypeError):
            record["sends"][-1]["send_error"] = "transport does not allow send interception"
        try:
            record["restores"].append(
                _capture_set_radio(arm.relay, radio_changes, gateway.clock, origin)
            )
        except (AttributeError, TypeError):
            record["sends"][-1]["set_radio_error"] = "relay does not allow set_radio interception"
    _CURRENT.append(record)
    return record


def _subscribe(record: dict[str, Any]) -> None:
    """Capture events from the attached controller once per controller."""

    gateway = record["gateway"]
    controller = gateway.controller
    if controller is None or id(controller) in record["subscribed"]:
        return
    record["subscribed"].add(id(controller))
    unsubscribe = controller.events.subscribe(
        lambda event: record["events"].append(_event_repr(event, record["origin"]))
    )
    record["restores"].append({"unsubscribe": unsubscribe})


def _gateway_summary(record: dict[str, Any]) -> dict[str, Any]:
    """Build the deterministic per-Gateway JSON object."""

    gateway = record["gateway"]
    origin = record["origin"]
    return {
        "history": [[_relative(at, origin), event, node] for at, event, node in gateway.history],
        "stats": _jsonable(gateway.stats, origin),
        "events": record["events"],
        "sends": [
            {
                "radio": item["radio"],
                "channel": item["channel"],
                "bytes": item["bytes"],
                "set_radio": item["set_radio"],
                **({"send_error": item["send_error"]} if "send_error" in item else {}),
                **({"set_radio_error": item["set_radio_error"]} if "set_radio_error" in item else {}),
            }
            for item in record["sends"]
        ],
        "nodes": _nodes(gateway, origin),
        "health": _health(gateway, origin),
        "acceptance": [
            _acceptance(acceptance, origin) for _, acceptance in sorted(gateway.acceptance.items())
        ],
        "link_tests": [_link_test(test, origin) for _, test in sorted(gateway.link_tests.items())],
        "sessions": [
            {
                "target": target,
                "state": session.state,
                "radio": session.radio,
                "sf": session.sf,
                "route": list(session.route),
            }
            for target, session in sorted(gateway.sessions.items())
        ],
    }


def _restore(records: Iterable[dict[str, Any]]) -> None:
    """Remove subscriptions and instance-level send wrappers."""

    for record in records:
        for restore in reversed(record["restores"]):
            if "unsubscribe" in restore:
                restore["unsubscribe"]()
                continue
            target = restore["target"]
            method = restore["method"]
            if restore["had_own"]:
                setattr(target, method, restore["previous"])
            elif method in getattr(target, "__dict__", {}):
                delattr(target, method)


def _activate() -> None:
    """Install Gateway hooks once when E4G_OUT is set."""

    global _ORIGINAL_INIT, _ORIGINAL_ATTACH, _ORIGINAL_NETWORK_INIT
    if not _ENABLED or _ORIGINAL_INIT is not None:
        return

    from cairnmesh.gateway import Gateway
    from cairnmesh.mesh_sim import MeshNetwork

    _ORIGINAL_INIT = Gateway.__init__
    _ORIGINAL_ATTACH = Gateway.attach
    _ORIGINAL_NETWORK_INIT = MeshNetwork.__init__

    def network_init(self: Any, *args: Any, **kwargs: Any) -> None:
        if kwargs.get("seed") is None:
            kwargs["seed"] = 0
        if "clock" not in kwargs:
            kwargs["clock"] = _SimClock()
        _ORIGINAL_NETWORK_INIT(self, *args, **kwargs)

    def init(self: Any, *args: Any, **kwargs: Any) -> None:
        _ORIGINAL_INIT(self, *args, **kwargs)
        _record_gateway(self)

    def attach(self: Any, controller: Any) -> None:
        _ORIGINAL_ATTACH(self, controller)
        for record in _CURRENT:
            if record["gateway"] is self:
                _subscribe(record)

    Gateway.__init__ = init
    Gateway.attach = attach
    MeshNetwork.__init__ = network_init


def pytest_configure(config: pytest.Config) -> None:
    """Install Gateway hooks only when E4G_OUT is set."""

    _activate()


def pytest_runtest_setup(item: pytest.Item) -> None:
    """Start a fresh Gateway list for this test."""

    if _ENABLED:
        _CURRENT.clear()


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_teardown(item: pytest.Item, nextitem: pytest.Item | None):
    """Finish fixture teardown, then serialize every Gateway made by the test."""

    yield
    if not _ENABLED or not _CURRENT:
        return
    records = list(_CURRENT)
    _CURRENT.clear()
    _RECORDS[item.nodeid] = [_gateway_summary(record) for record in records]
    _restore(records)


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Write JSON lines sorted by pytest node id."""

    if not _ENABLED:
        return
    output = Path(_OUTPUT)
    output.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        json.dumps({"test": test, "gateways": _RECORDS[test]}, ensure_ascii=False, sort_keys=True)
        for test in sorted(_RECORDS)
    ]
    output.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def pytest_unconfigure(config: pytest.Config) -> None:
    """Restore the original Gateway methods."""

    if _ORIGINAL_INIT is None:
        return
    from cairnmesh.gateway import Gateway
    from cairnmesh.mesh_sim import MeshNetwork

    Gateway.__init__ = _ORIGINAL_INIT
    Gateway.attach = _ORIGINAL_ATTACH
    MeshNetwork.__init__ = _ORIGINAL_NETWORK_INIT


if _ENABLED:
    _activate()
