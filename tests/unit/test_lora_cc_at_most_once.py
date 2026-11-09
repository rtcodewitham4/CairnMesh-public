"""Whole-system properties under loss, reordering and garbage.

Deterministic: every random source is seeded and time is a fake clock. The
properties are the ones the protocol exists for:

* an action never runs twice for one request, however frames are lost,
  delayed past the timeout, or delivered out of order;
* a request that was answered OK really ran exactly once;
* every future resolves (nothing is left hanging);
* malformed input never raises anything but ``ValueError`` out of the decoders.
"""

from __future__ import annotations

import random
from collections import Counter

import pytest

from cairnmesh.channel import SimulatedMedium
from cairnmesh.controller import DEFAULT_MAX_RETRY_WINDOW, Controller
from cairnmesh.device import Device
from cairnmesh.frames import DEFAULT_CONTROLLER_ID, decode_frame
from cairnmesh.serial_link import SlipDecoder, slip_encode

KEY = b"0123456789abcdef"


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class CountingDevice(Device):
    """Counts how many times each distinct request actually ran."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.runs: Counter = Counter()

    def perform_action(self, frame):
        self.runs[(frame.sender_id, frame.sequence, frame.action, frame.value)] += 1
        return super().perform_action(frame)


def simulate(seed, *, drop, latency, secure=False, nodes=3, per_node=60):
    """Run ``per_node`` requests to each node; return (devices, futures)."""

    clock = FakeClock()
    medium = SimulatedMedium(
        downlink_drop=drop,
        uplink_drop=drop,
        latency=latency,
        rng=random.Random(seed + 1),
        clock=clock,
    )

    def wrap(transport):
        if not secure:
            return transport
        from cairnmesh.secure import SecureTransport

        return SecureTransport(transport, KEY, initial_fcnt=0)

    devices = {
        node: CountingDevice(
            wrap(medium.node(node)),
            node,
            awake=True,
            heartbeat_interval=1.0,
            rng=random.Random(node),
            clock=clock,
        )
        for node in range(1, nodes + 1)
    }
    controller = Controller(
        wrap(medium.gateway),
        max_queue=1_000,
        clock=clock,
        rng=random.Random(seed),
    )
    futures = [
        (index, controller.request(node, index // 256, index % 256))
        for index in range(per_node)
        for node in devices
    ]
    for _ in range(40_000):  # at most 400 simulated seconds
        clock.now += 0.01
        medium.tick()
        for device in devices.values():
            device.tick()
        controller.tick()
        if all(future.done() for _, future in futures):
            break
    return devices, futures


SCENARIOS = {
    "light loss": dict(drop=0.2, latency=(0.01, 0.08)),
    "latency above the timeout (reordering)": dict(drop=0.3, latency=(0.01, 1.5)),
    "harsh reordering": dict(drop=0.1, latency=(0.01, 3.0)),
    "sequence numbers wrap": dict(drop=0.1, latency=(0.01, 0.08), per_node=300),
}


@pytest.mark.parametrize("name", SCENARIOS)
@pytest.mark.parametrize("seed", [0, 1, 2])
def test_actions_run_at_most_once_and_every_future_resolves(name, seed):
    devices, futures = simulate(seed, **SCENARIOS[name])

    assert all(future.done() for _, future in futures), "a future was left hanging"
    for device in devices.values():
        assert max(device.runs.values(), default=0) <= 1, "an action ran twice"
    for index, future in futures:
        outcome = future.result(timeout=0)
        if outcome.ok:
            device = devices[outcome.node_id]
            key = (DEFAULT_CONTROLLER_ID, outcome.sequence, index // 256, index % 256)
            assert device.runs[key] == 1, "answered OK but did not run exactly once"


def test_actions_run_at_most_once_through_the_secure_layer():
    pytest.importorskip("cryptography")
    devices, futures = simulate(
        4, drop=0.2, latency=(0.01, 1.0), secure=True, nodes=2, per_node=40
    )

    assert all(future.done() for _, future in futures)
    assert all(max(d.runs.values(), default=0) <= 1 for d in devices.values())
    assert any(future.result(timeout=0).ok for _, future in futures)


# ------------------------------------------------------------------ garbage


def random_bytes(rng, longest=40):
    return bytes(rng.randrange(256) for _ in range(rng.randrange(0, longest)))


def test_decoders_only_ever_raise_value_error():
    rng = random.Random(7)
    for _ in range(5_000):
        try:
            decode_frame(random_bytes(rng))
        except ValueError:
            pass


def test_slip_decoder_survives_arbitrary_bytes():
    rng = random.Random(8)
    decoder = SlipDecoder()
    for _ in range(5_000):
        decoder.feed(random_bytes(rng, longest=300))
    good = bytes([2, 1, 0x42, 1, 0])

    assert decoder.feed(b"\xc0" + slip_encode(good)) == [good]  # still in sync


def test_secure_layer_survives_arbitrary_bytes_and_delivers_nothing():
    pytest.importorskip("cryptography")
    from cairnmesh.secure import SecureTransport

    class Wire:
        def send(self, data):
            pass

        def set_receiver(self, callback):
            self.callback = callback

    wire, delivered = Wire(), []
    layer = SecureTransport(wire, KEY, initial_fcnt=0)
    layer.set_receiver(delivered.append)
    rng = random.Random(9)
    for _ in range(5_000):
        data = random_bytes(rng)
        if data and rng.random() < 0.5:
            data = bytes([data[0] | 0x80]) + data[1:]  # look like a sealed frame
        wire.callback(data)

    assert delivered == []  # nothing without a valid MIC gets through


# ------------------------------------------------------------ defaults


def test_default_retry_window_fits_inside_the_device_cache_lifetime():
    """The controller's cap and the device's TTL are one contract in two places.

    A retry must never arrive after the device forgot the request, including
    the time a sleeping device holds a frame before handling it.
    """

    medium = SimulatedMedium()
    device = Device(medium.node(1), 1)
    controller = Controller(medium.gateway)
    slowest_sleep = max(device.sleep_delay)

    assert DEFAULT_MAX_RETRY_WINDOW + slowest_sleep <= device.dedup_ttl
    for retries in (controller.max_retries, controller.wake_max_retries):
        assert (retries + 1) * controller.timeout + slowest_sleep <= device.dedup_ttl
