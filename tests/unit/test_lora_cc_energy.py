"""Task D1: preamble sampling of dormant radios, energy metering, a moving radio."""

from __future__ import annotations

import random

import pytest

from cairnmesh.energy import EnergyMeter, RadioCurrents, Sniffer, preamble_for, symbol_time
from cairnmesh.mesh_sim import GeoMedium
from cairnmesh.radio_model import LogDistance


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_a_look_must_fit_inside_the_preamble():
    sniffer = Sniffer(period=1.0, phase=0.3, cad_time=0.01)
    assert sniffer.looks_within(0.0, 0.305) == []  # the look at 0.3 would end after the preamble
    assert sniffer.looks_within(0.0, 0.31) == [pytest.approx(0.3)]
    assert sniffer.looks_within(0.0, 2.5) == [pytest.approx(0.3), pytest.approx(1.3), pytest.approx(2.3)]


def test_a_short_preamble_is_caught_only_by_luck_of_phase():
    rng = random.Random(1)
    preamble, period, cad = 0.2, 1.0, 0.01
    caught = sum(
        Sniffer(period=period, phase=rng.uniform(0, period), cad_time=cad, p_detect=1.0).catches(5.0, preamble, rng)
        is not None
        for _ in range(4000)
    )
    assert caught / 4000 == pytest.approx((preamble - cad) / period, abs=0.03)


def test_a_preamble_longer_than_the_period_is_always_caught_but_for_detection():
    rng = random.Random(2)
    sniffer_of = lambda: Sniffer(period=0.5, phase=rng.uniform(0, 0.5), p_detect=0.9)  # noqa: E731
    symbols = preamble_for(sniffer_of(), sf=7)
    preamble = symbols * symbol_time(7)
    assert preamble >= 0.5
    caught = sum(sniffer_of().catches(3.0, preamble, rng) is not None for _ in range(4000))
    # Nearly always exactly one look fits, which misses with 0.1.
    assert caught / 4000 == pytest.approx(0.9, abs=0.02)
    # Two periods of preamble: two looks, both must miss.
    preamble = preamble_for(sniffer_of(), sf=7, margin=2.0) * symbol_time(7)
    caught = sum(sniffer_of().catches(3.0, preamble, rng) is not None for _ in range(4000))
    assert caught / 4000 == pytest.approx(0.99, abs=0.01)


def medium(clock: Clock) -> GeoMedium:
    return GeoMedium(radio_range=100.0, airtime=0.05, rng=random.Random(3), clock=clock,
                     signal=LogDistance(shadowing_db=0.0, fading_db=0.0))


def test_a_dormant_radio_hears_only_long_preambles():
    clock = Clock()
    geo = medium(clock)
    sender = geo.add(1, (0.0, 0.0))
    sleeper = geo.add(2, (20.0, 0.0))
    heard: list[bytes] = []
    sleeper.set_receiver(lambda data, rssi: heard.append(data))
    sleeper.sniffer = Sniffer(period=1.0, phase=0.5, p_detect=1.0)
    sleeper.set_dormant(True)
    sender.send(b"\x81short")  # 8 symbols, ~8 ms: the look at 0.5 is long past
    clock.now += 1.0
    geo.tick()
    assert heard == [] and geo.stats["slept_through"] == 1
    clock.now = 2.0
    sender.send(b"\x81long", preamble=preamble_for(sleeper.sniffer))
    clock.now += 2.0
    geo.tick()
    assert heard == [b"\x81long"]
    assert sleeper.meter.dormant_rx > 0


def test_a_long_preamble_costs_its_airtime():
    clock = Clock()
    geo = medium(clock)
    sender = geo.add(1, (0.0, 0.0))
    sender.send(b"\x81x", preamble=108)
    assert sender.meter.tx == pytest.approx(0.05 + 100 * symbol_time(7))


def test_the_meter_adds_up_like_by_hand():
    currents = RadioCurrents(sleep=1e-6, rx=5e-3, tx=25e-3, cad=4e-3, mcu_sleep=0.0, mcu_run=1e-3, regulator=0.0)
    sniffer = Sniffer(period=0.5, cad_time=0.002, p_false=0.01, false_rx=0.02)
    meter = EnergyMeter(currents=currents, sniffer=sniffer)
    meter.start(0.0)
    meter.add_tx(1.0)
    meter.set_dormant(True, 10.0)  # 10 s awake, 1 s of it sending
    meter.add_dormant_rx(0.1)
    charge = meter.charge(110.0)  # 100 s dormant
    looks = 100 / 0.5
    cad, false = looks * 0.002, looks * 0.01 * 0.02
    expected = 9 * 5e-3 + 1 * 25e-3 + 10 * 1e-3
    expected += (100 - cad - false - 0.1) * 1e-6 + cad * 4e-3 + (false + 0.1) * (5e-3 + 1e-3)
    assert charge == pytest.approx(expected)
    meter.set_dormant(False, 110.0)
    assert meter.wakeups == 1


def test_a_moved_radio_gets_new_terrain_on_its_links_only():
    clock = Clock()
    geo = GeoMedium(radio_range=100.0, clock=clock, signal=LogDistance(seed=4))
    for node, x in ((1, 0.0), (2, 50.0), (3, 90.0)):
        geo.add(node, (x, 0.0))
    before = {pair: geo.median_rssi(*pair) for pair in ((1, 2), (1, 3), (2, 3))}
    geo.move(1, (0.0, 0.0))  # same place, but a new visit: new terrain
    after = {pair: geo.median_rssi(*pair) for pair in before}
    assert after[(2, 3)] == before[(2, 3)]
    assert after[(1, 2)] != before[(1, 2)] and after[(1, 3)] != before[(1, 3)]
    geo.move(1, (500.0, 0.0))
    assert 1 not in geo.neighbours(2)


def test_the_controller_can_walk_in_from_outside():
    from cairnmesh.mesh_sim import MeshNetwork, line

    network = MeshNetwork(line(3), key=bytes(16), controller_position=(-1000.0, 0.0), signal=None)
    assert not network.reachable(1)
    network.move(network.controller_id, (0.0, 0.0))
    assert network.reachable(3)


def test_the_regulator_draws_all_the_time():
    from cairnmesh.energy import dormant_current

    base = dormant_current(Sniffer(period=0.5), RadioCurrents(regulator=0.0))
    assert dormant_current(Sniffer(period=0.5), RadioCurrents(regulator=5e-6)) == pytest.approx(base + 5e-6)
    meter = EnergyMeter(currents=RadioCurrents(regulator=5e-6), sniffer=Sniffer(period=0.5))
    other = EnergyMeter(currents=RadioCurrents(regulator=0.0), sniffer=Sniffer(period=0.5))
    for m in (meter, other):
        m.start(0.0)
        m.set_dormant(True, 10.0)
    assert meter.charge(100.0) - other.charge(100.0) == pytest.approx(100 * 5e-6)
