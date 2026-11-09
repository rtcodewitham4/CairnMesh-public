"""Gateway report of weak measured links (``Gateway.weak_links``): diagnostics, no frames."""

from __future__ import annotations

import pytest

pytest.importorskip("cryptography")

from cairnmesh.frames import LinkQuality
from cairnmesh.gateway import KnownNode

from _session_helpers import LINE, build, open_session


def margin(db: float) -> LinkQuality:
    """A link ``db`` above the SF7 limit (−7.5 dB SNR)."""

    return LinkQuality(-100, round((db - 7.5) * 4))


def gateway_with_book():
    clock, network, controller = build(LINE, seed=1, signal=None, gateway_options={"idle_timeout": None})
    gateway = network.sessions
    # G→1→2→3 and G→1→2→4: the 1→2 hop is shared; 2→4 is the worst.
    gateway.nodes[3] = KnownNode(3, (1, 2), (margin(12), margin(2), margin(9)), 5.0, "scan")
    gateway.nodes[4] = KnownNode(4, (1, 2), (margin(12), margin(2), margin(-1)), 5.0, "scan")
    return clock, network, controller, gateway


def test_weak_links_lists_hops_under_the_threshold_weakest_first():
    _, _, _, gateway = gateway_with_book()
    report = gateway.weak_links()  # default: sf_margin, 4 dB
    assert [(w.sender, w.receiver) for w in report] == [(2, 4), (1, 2)]
    worst, shared = report
    assert worst.margin_db == pytest.approx(-1.0) and worst.bottleneck_of == (4,) and worst.carries == (4,)
    assert shared.margin_db == pytest.approx(2.0) and shared.bottleneck_of == (3,) and shared.carries == (3, 4)


def test_threshold_widens_or_narrows_the_report():
    _, _, _, gateway = gateway_with_book()
    assert len(gateway.weak_links(10.0)) == 3  # 2→3 at 9 dB too
    assert [(w.sender, w.receiver) for w in gateway.weak_links(0.0)] == [(2, 4)]


def test_a_newer_probed_path_replaces_the_book_entry():
    _, _, _, gateway = gateway_with_book()
    gateway.probed[4] = ((gateway.controller_id, 1, 5, 4), (margin(12), margin(11), margin(10)), 9.0)
    report = gateway.weak_links()
    assert [(w.sender, w.receiver) for w in report] == [(1, 2)]
    assert report[0].carries == (3,)


def test_a_probed_session_path_is_kept_for_the_report():
    clock, network, controller = build(LINE, seed=1, signal=None, gateway_options={"idle_timeout": None})
    gateway = network.sessions
    session = open_session(clock, network, controller, 5)
    assert session.state == "open"
    path, links, _ = gateway.probed[5]
    assert path == (gateway.controller_id, *session.route, 5) and links == session.links
    report = gateway.weak_links(threshold=1000.0)
    assert {(w.sender, w.receiver) for w in report} == set(zip(path, path[1:]))
    assert sum(5 in w.bottleneck_of for w in report) == 1
