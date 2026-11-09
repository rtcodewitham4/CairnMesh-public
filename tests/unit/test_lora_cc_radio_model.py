"""Task B1: link budget with shadowing, fading, per-SF reach, channels and capture."""

from __future__ import annotations

import random

import pytest

from cairnmesh.airtime import lora_airtime
from cairnmesh.mesh_sim import GeoMedium
from cairnmesh.radio_model import LogDistance, sensitivity_dbm


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def medium(signal: LogDistance, clock: Clock, **options) -> tuple[GeoMedium, dict[int, list[float]]]:
    geo = GeoMedium(radio_range=100.0, airtime=0.05, rng=random.Random(1), clock=clock, signal=signal, **options)
    heard: dict[int, list[float]] = {}
    return geo, heard


def listen(geo: GeoMedium, node_id: int, position, heard: dict[int, list[float]]):
    port = geo.add(node_id, position)
    heard[node_id] = []
    port.set_receiver(lambda data, rssi, node_id=node_id: heard[node_id].append(rssi))
    return port


def send_and_settle(geo: GeoMedium, port, clock: Clock, data: bytes = b"\x81hello") -> None:
    port.send(data)
    clock.now += 1.0
    geo.tick()


def test_sensitivity_follows_the_snr_limits():
    assert sensitivity_dbm(7) == pytest.approx(-124.5)
    assert sensitivity_dbm(12) == pytest.approx(-137.0)
    with pytest.raises(ValueError):
        sensitivity_dbm(6)


def test_median_link_is_at_the_limit_at_radio_range():
    model = LogDistance(shadowing_db=0.0)
    assert model.median_rssi(100.0, 100.0, 1, 2) == pytest.approx(sensitivity_dbm(7))
    # 10x closer gains 10·n dB.
    assert model.median_rssi(10.0, 100.0, 1, 2) == pytest.approx(sensitivity_dbm(7) + 27.0)


def test_higher_sf_reaches_further():
    model = LogDistance()
    assert model.reach(7) == pytest.approx(1.0)
    assert 2.5 < model.reach(12) < 3.0  # 12.5 dB at n = 2.7


def test_shadowing_is_fixed_per_link_and_symmetric():
    model = LogDistance(seed=3)
    assert model.shadowing(4, 9) == model.shadowing(9, 4)
    assert model.shadowing(4, 9) == LogDistance(seed=3).shadowing(4, 9)
    assert model.shadowing(4, 9) != LogDistance(seed=4).shadowing(4, 9)
    spread = [model.shadowing(1, n) for n in range(2, 2000)]
    mean = sum(spread) / len(spread)
    sigma = (sum((x - mean) ** 2 for x in spread) / len(spread)) ** 0.5
    assert abs(mean) < 0.5 and 5.5 < sigma < 6.5


def test_edge_link_is_heard_about_half_the_time():
    clock = Clock()
    geo, heard = medium(LogDistance(shadowing_db=0.0, fading_db=2.0), clock)
    sender = listen(geo, 1, (0.0, 0.0), heard)
    listen(geo, 2, (100.0, 0.0), heard)
    for _ in range(400):
        send_and_settle(geo, sender, clock)
    assert 0.4 < len(heard[2]) / 400 < 0.6
    assert all(rssi >= sensitivity_dbm(7) for rssi in heard[2])
    assert geo.stats["too_weak"] == 400 - len(heard[2])


def test_sf12_hears_a_link_sf7_cannot():
    clock = Clock()
    geo, heard = medium(LogDistance(shadowing_db=0.0, fading_db=0.0), clock)
    sender = listen(geo, 1, (0.0, 0.0), heard)
    receiver = listen(geo, 2, (200.0, 0.0), heard)
    send_and_settle(geo, sender, clock)
    assert heard[2] == [] and geo.neighbours(1) == []
    sender.sf = receiver.sf = 12
    send_and_settle(geo, sender, clock)
    assert len(heard[2]) == 1 and geo.neighbours(1) == [2]


def test_radios_hear_only_their_own_channel_and_sf():
    clock = Clock()
    geo, heard = medium(LogDistance(shadowing_db=0.0, fading_db=0.0), clock)
    sender = listen(geo, 1, (0.0, 0.0), heard)
    other_channel = listen(geo, 2, (10.0, 0.0), heard)
    other_sf = listen(geo, 3, (10.0, 0.0), heard)
    listen(geo, 4, (10.0, 0.0), heard)
    other_channel.channel = 1
    other_sf.sf = 9
    send_and_settle(geo, sender, clock)
    assert (len(heard[2]), len(heard[3]), len(heard[4])) == (0, 0, 1)


def test_airtime_follows_the_senders_sf():
    geo = GeoMedium(airtime=0.05)
    assert geo.airtime_of(20) == 0.05
    assert geo.airtime_of(20, 7) == 0.05  # the medium's own SF keeps its airtime
    assert geo.airtime_of(20, 10) == pytest.approx(lora_airtime(20, 10))


def overlap(capture_db):
    clock = Clock()
    geo, heard = medium(LogDistance(shadowing_db=0.0, fading_db=0.0, capture_db=capture_db), clock)
    near = listen(geo, 1, (10.0, 0.0), heard)
    far = listen(geo, 2, (-90.0, 0.0), heard)
    listen(geo, 3, (0.0, 0.0), heard)
    near.send(b"\x81near")
    far.send(b"\x81far")
    clock.now += 1.0
    geo.tick()
    return geo, heard[3]


def test_capture_keeps_the_much_stronger_frame():
    geo, heard = overlap(6.0)
    assert len(heard) == 1 and heard[0] > -100 and geo.stats["captured"] == 1


def test_without_capture_overlaps_lose_both():
    geo, heard = overlap(None)
    assert heard == [] and geo.stats["captured"] == 0


def test_disc_model_is_unchanged_without_signal():
    clock = Clock()
    geo = GeoMedium(radio_range=100.0, airtime=0.05, rng=random.Random(1), clock=clock)
    heard: dict[int, list[float]] = {}
    sender = listen(geo, 1, (0.0, 0.0), heard)
    listen(geo, 2, (50.0, 0.0), heard)
    listen(geo, 3, (150.0, 0.0), heard)
    send_and_settle(geo, sender, clock)
    assert heard[2] == [pytest.approx(-80.0)] and heard[3] == []


# ------------------------------------------------- the default for simulations


def test_networks_default_to_the_link_budget_with_terrain_per_seed(monkeypatch):
    from cairnmesh import mesh_sim

    monkeypatch.setattr(mesh_sim, "DEFAULT_SIGNAL", LogDistance())
    network = mesh_sim.MeshNetwork({1: (50.0, 0.0)}, key=bytes(16), seed=3)
    assert network.medium.signal == LogDistance(seed=3)
    assert mesh_sim.MeshNetwork({1: (50.0, 0.0)}, key=bytes(16), signal=None).medium.signal is None
    assert mesh_sim.planned()["radio_range"] == pytest.approx(80.0 * 10 ** (10 / 27))


def test_a_planned_line_carries_a_session_on_the_link_budget(monkeypatch):
    from cairnmesh import mesh_sim
    from cairnmesh.airtime import airtime_for

    monkeypatch.setattr(mesh_sim, "DEFAULT_SIGNAL", LogDistance())
    clock = Clock()
    network = mesh_sim.MeshNetwork(
        mesh_sim.line(5), key=bytes(16), clock=clock, airtime=airtime_for(7), seed=2,
        gateway_options={}, **mesh_sim.planned(),
    )
    controller = network.controller(network.sessions, clock=clock, timeout=8.0, max_retries=2)
    network.sessions.attach(controller)

    def run(seconds, until):
        end = clock.now + seconds
        while clock.now < end and not until():
            clock.now += 0.005
            network.tick(clock.now)
            controller.tick(clock.now)

    session = network.sessions.open_session(5)
    run(20.0, lambda: session.state == "open")
    assert session.state == "open"
    delivered = 0
    for value in range(5):
        future = network.sessions.request(5, 3, value)
        run(20.0, future.done)
        delivered += future.result(timeout=0).ok
    assert delivered == 5 and network.devices[5].handled_count == 5
    assert network.medium.stats["too_weak"] > 0  # the far links really fade


def test_the_detection_bound_allows_for_skipped_keepalives():
    from cairnmesh.airtime import airtime_for
    from cairnmesh.gateway import LatencyModel

    model = LatencyModel(airtime_for(7), jitter=0.02, slack=0.02, answer=0.02)
    one = model.detection(3, 2.0)
    assert model.detection(3, 2.0, None) == one
    assert model.detection(3, 2.0, 2) == pytest.approx(2 * one)
