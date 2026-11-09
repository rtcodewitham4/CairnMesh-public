"""NodeBook invariants N1-N7."""

from __future__ import annotations

import json

import pytest

from cairnmesh.frames import BROADCAST_ID, MAX_RELAYS, LinkQuality, ProbeHop
from cairnmesh.nodebook import KnownNode, NodeBook


def link(margin_db: float) -> LinkQuality:
    """A link at ``margin_db`` above the SF7 limit."""

    return LinkQuality(-100, round((margin_db - 7.5) * 4))


def flat_cost(_links: tuple[LinkQuality, ...]) -> float:
    return 1.0


def path(book: NodeBook, target: int, *, longest: int = MAX_RELAYS + 1, usable=lambda a, b: True, fresh=()):
    return book._book_path(
        target, usable, longest, fresh, cost=flat_cost, detour_margin=3.0,
    )


@pytest.mark.parametrize("node", [0, BROADCAST_ID])
def test_n1_gateway_and_broadcast_are_rejected_and_neighbours_are_filtered(node):
    book = NodeBook(controller_id=0)

    assert book.found(node, (), (), "scan", 1.0) is None
    assert book.nodes == {}

    neighbours = (
        ProbeHop(2, link(8.0)),
        ProbeHop(0, link(9.0)),
        ProbeHop(BROADCAST_ID, link(10.0)),
    )
    assert book.found(1, (), (), "scan", 2.0, neighbours=neighbours) is True
    assert tuple(hop.node_id for hop in book.nodes[1].neighbours) == (2,)


@pytest.mark.parametrize(
    ("saved_hops", "expected_hops"),
    [(0, 1), (1, 1), (MAX_RELAYS + 1, MAX_RELAYS + 1), (99, MAX_RELAYS + 1)],
)
def test_n2_load_only_adds_unmeasured_entries_with_bounded_hops(
    tmp_path, saved_hops, expected_hops
):
    book = NodeBook(controller_id=0)
    saved = tmp_path / "nodes.json"
    saved.write_text(
        json.dumps(
            {
                "version": 1,
                "nodes": [{"id": 3, "hops": saved_hops, "state": 4, "neighbours": [[2, -90, 20]]}],
            }
        ),
        encoding="utf-8",
    )

    assert book.load_nodes(str(saved), 7.0) == 1
    assert book.nodes[3] == KnownNode(
        3, (), (), 7.0, "saved", 4, (ProbeHop(2, LinkQuality(-90, 20)),), measured=False,
        last_hops=expected_hops,
    )

    assert book.found(3, (1, 2), (link(1.0), link(2.0), link(3.0)), "scan", 8.0) is False
    assert book.load_nodes(str(saved), 9.0) == 0
    assert book.nodes[3].measured and book.nodes[3].route == (1, 2)


@pytest.mark.parametrize(
    "entry",
    [
        {"id": "not-an-int"},
        {"id": 3, "neighbours": [[1, -80]]},
        {"id": 3, "state": "not-an-int"},
        "not-an-object",
    ],
)
def test_n2_malformed_entries_are_skipped(tmp_path, entry):
    saved = tmp_path / "nodes.json"
    saved.write_text(json.dumps({"version": 1, "nodes": [entry]}), encoding="utf-8")

    assert NodeBook(controller_id=0).load_nodes(str(saved), 1.0) == 0


@pytest.mark.parametrize(
    "text",
    ["{not json", "[]", '{"version": 2, "nodes": [{"id": 3}]}', '{"version": 1}'],
)
def test_n3_bad_books_load_nothing(tmp_path, text):
    saved = tmp_path / "nodes.json"
    saved.write_text(text, encoding="utf-8")
    assert NodeBook(controller_id=0).load_nodes(str(saved), 1.0) == 0


def test_n3_save_omits_routes_and_links(tmp_path):
    book = NodeBook(controller_id=0)
    book.found(2, (1,), (link(8.0), link(6.0)), "scan", 1.0)
    saved = tmp_path / "nodes.json"

    book.save_nodes(str(saved))
    data = json.loads(saved.read_text(encoding="utf-8"))

    assert data["version"] == 1 and data["nodes"][0]["id"] == 2
    assert not {"route", "links"} & data["nodes"][0].keys()


@pytest.mark.parametrize(
    ("order", "expected_links"),
    [
        ((3, 4), [1.5, -2.5, 1.5, -6.5]),
        ((4, 3), [1.5, -2.5, 1.5, -6.5]),
    ],
)
def test_n4_equal_paths_keep_node_book_iteration_order(order, expected_links):
    book = NodeBook(controller_id=0)
    for position, node in enumerate(order):
        book.nodes[node] = KnownNode(
            node, (), (), 1.0, "scan",
            neighbours=(ProbeHop(0, link(12.0)), ProbeHop(9, link(8.0 if position == 0 else 4.0))),
        )
    seen = []

    def record(links: tuple[LinkQuality, ...]) -> float:
        seen.append(links[0].snr_db)
        return 1.0

    assert book._book_path(9, lambda a, b: True, 3, (), cost=record, detour_margin=3.0) == (3,)
    assert seen == expected_links


@pytest.mark.parametrize(
    ("case", "arguments", "expected"),
    [
        ("neighbour", {"target": 9}, ()),
        ("none", {"target": 9}, None),
        ("unusable", {"target": 9, "usable": lambda a, b: False}, None),
        ("too-long", {"target": 9, "longest": 1}, None),
    ],
)
def test_n5_path_edges_and_boundaries(case, arguments, expected):
    book = NodeBook(controller_id=0)
    if case == "neighbour":
        book.nodes[9] = KnownNode(
            9, (), (), 1.0, "scan", neighbours=(ProbeHop(0, link(8.0)),),
        )
    elif case != "none":
        book.nodes[3] = KnownNode(
            3, (), (), 1.0, "scan",
            neighbours=(ProbeHop(0, link(8.0)), ProbeHop(9, link(8.0))),
        )

    assert path(book, **arguments) == expected


def test_n5_book_links_are_discounted_but_fresh_links_are_not():
    book = NodeBook(controller_id=0)
    book.nodes[3] = KnownNode(
        3, (), (), 1.0, "scan",
        neighbours=(ProbeHop(0, link(12.0)), ProbeHop(9, link(6.0))),
    )
    seen = []

    def record(links: tuple[LinkQuality, ...]) -> float:
        seen.append(links[0])
        return 1.0

    assert book._book_path(9, lambda a, b: True, 3, (), cost=record, detour_margin=3.0) == (3,)
    assert seen[-1].snr_db == -4.5

    seen.clear()
    assert book._book_path(
        9, lambda a, b: True, 3, (((0, 9), link(6.0)),), cost=record, detour_margin=3.0,
    ) == ()
    assert seen[-1].snr_db == -1.5


def test_n6_probed_paths_do_not_participate_in_routing():
    book = NodeBook(controller_id=0)
    book.probed[9] = ((0, 3, 9), (link(9.0), link(9.0)), 10.0)

    assert path(book, 9) is None


@pytest.mark.parametrize(
    ("threshold", "expected"),
    [
        (2.0, [(2, 4)]),
        (100.0, [(2, 4), (1, 5), (5, 3), (1, 2), (5, 8), (0, 1)]),
        (-10.0, []),
    ],
)
def test_n7_weak_links_latest_measurements_and_tied_order(threshold, expected):
    book = NodeBook(controller_id=0)
    book.nodes[3] = KnownNode(3, (1, 2), (link(12.0), link(5.0), link(2.0)), 5.0, "scan")
    book.nodes[4] = KnownNode(4, (1, 2), (link(12.0), link(5.0), link(-1.0)), 6.0, "scan")
    book.nodes[8] = KnownNode(8, (1, 5), (link(12.0), link(2.0), link(9.0)), 6.0, "scan")
    book.probed[3] = ((0, 1, 5, 3), (link(12.0), link(2.0), link(2.0)), 9.0)

    report = book.weak_links(threshold, floor=-7.5)

    assert [(weak.sender, weak.receiver) for weak in report] == expected
    assert all(set(weak.bottleneck_of) <= set(weak.carries) for weak in report)


@pytest.mark.parametrize("links", [(), (link(1.0),), (link(1.0), link(2.0), link(3.0))])
def test_n7_route_and_links_length_mismatch_is_ignored(links):
    book = NodeBook(controller_id=0)
    book.nodes[3] = KnownNode(3, (1,), links, 1.0, "scan")

    assert book.weak_links(100.0, floor=0.0) == []
