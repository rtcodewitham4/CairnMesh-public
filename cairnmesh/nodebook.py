"""The gateway's node book: diagnostic and optimisation knowledge, not a route table."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Callable, Optional

from .frames import BROADCAST_ID, MAX_RELAYS, LinkQuality, ProbeHop
from .latency import margin_db


@dataclass
class KnownNode:
    """A node the gateway has heard announce itself or answer a scan (task C)."""

    node_id: int
    #: Relays from the gateway outward, and one link per hop the same way.
    route: tuple[int, ...]
    links: tuple[LinkQuality, ...]
    seen_at: float
    #: ``join`` or ``scan``: how it was last heard of.
    how: str
    state: Optional[int] = None
    neighbours: tuple[ProbeHop, ...] = ()
    #: ``False`` for an entry loaded from a saved book (``Gateway.load_nodes``):
    #: its route is unknown (the controller has moved since) and ``last_hops``
    #: is how deep it was when last measured.
    measured: bool = True
    last_hops: Optional[int] = None

    @property
    def hops(self) -> int:
        if not self.measured and self.last_hops:
            return self.last_hops
        return len(self.route) + 1


@dataclass(frozen=True)
class WeakLink:
    """A measured hop with little margin: where an installer should add a relay (``Gateway.weak_links``)."""

    #: The hop from the gateway's side outward (links are taken as symmetric).
    sender: int
    receiver: int
    #: SNR above the base SF's demodulation limit, as last measured.
    margin_db: float
    measured_at: float
    #: Nodes whose known path has this hop as its weakest one.
    bottleneck_of: tuple[int, ...]
    #: Every node whose known path crosses it.
    carries: tuple[int, ...]


class NodeBook:
    """Persistent node knowledge used only for diagnostics and optimisation."""

    def __init__(self, controller_id: int) -> None:
        self.controller_id = controller_id
        # [MEM] nodes heard of (JOIN / scan) | 端: gateway(PC) | 上限: 网络节点数 | 每项: 路由 ≤16 B + 链路 ≤34 B + 邻居 9 B
        self.nodes: dict[int, KnownNode] = {}
        # [MEM] diagnostics | 端: gateway(PC) | 上限: 网络节点数 | 每项: 路由 ≤16 B + 链路 ≤34 B | 不上空口
        #: Each node's last probe-tested path and links (``weak_links`` only;
        #: routing reads ``nodes`` and the open session, never this).
        self.probed: dict[int, tuple[tuple[int, ...], tuple[LinkQuality, ...], float]] = {}

    def found(
        self,
        node: int,
        route: tuple[int, ...],
        links: tuple[LinkQuality, ...],
        how: str,
        now: float,
        state: Optional[int] = None,
        neighbours: tuple[ProbeHop, ...] = (),
    ) -> Optional[bool]:
        """Record a heard node; ``None`` if rejected, else whether it is new."""

        if node in (self.controller_id, BROADCAST_ID):
            return None
        neighbours = tuple(hop for hop in neighbours if hop.node_id not in (self.controller_id, BROADCAST_ID))
        new = node not in self.nodes
        self.nodes[node] = KnownNode(node, route, links, now, how, state, neighbours)
        return new

    def save_nodes(self, path: str) -> None:
        """Write the node book to ``path`` atomically (JSON; operations, task C/D).

        Kept: ids, depth, state, neighbours and when each was last heard
        (wall clock). Not kept: routes, which belong to where the controller
        stood (it moves).
        """

        from .durable import write_json

        entries = []
        for node in self.nodes.values():
            entries.append(
                {
                    "id": node.node_id,
                    "hops": node.hops,
                    "state": node.state,
                    "how": node.how if node.measured else "saved",
                    "neighbours": [[hop.node_id, hop.link.rssi, hop.link.snr] for hop in node.neighbours],
                }
            )
        book = {"version": 1, "saved_at": time.time(), "nodes": entries}
        write_json(path, book)

    def load_nodes(self, path: str, now: float) -> int:
        """Read a book written by ``save_nodes``; return how many entries were added.

        Entries already known (measured this run) win. A loaded entry has no
        route and only sizes scans (``KnownNode.hops``) and the follow-ups
        that ask silent known nodes one by one. A missing or broken file
        loads nothing.
        """

        import json

        try:
            with open(path, encoding="utf-8") as handle:
                book = json.load(handle)
            entries = book["nodes"] if book.get("version") == 1 else []
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            return 0
        added = 0
        for entry in entries:
            try:
                node = int(entry["id"])
                if not 0 <= node <= 0xFF or node in (self.controller_id, BROADCAST_ID) or node in self.nodes:
                    continue
                neighbours = tuple(
                    ProbeHop(int(n), LinkQuality(int(rssi), int(snr))) for n, rssi, snr in entry.get("neighbours", ())
                )
                state = entry.get("state")
                self.nodes[node] = KnownNode(
                    node, (), (), now, "saved",
                    None if state is None else int(state), neighbours,
                    measured=False, last_hops=max(1, min(MAX_RELAYS + 1, int(entry.get("hops", 1)))),
                )
            except (KeyError, TypeError, ValueError):
                continue
            added += 1
        return added

    def _book_path(
        self,
        target: int,
        usable,
        longest: int,
        fresh: tuple = (),
        *,
        cost: Callable[[tuple[LinkQuality, ...]], float],
        detour_margin: float,
    ) -> Optional[tuple[int, ...]]:
        """Relays of the cheapest path to ``target`` the node book knows; ``None``: none.

        Links come from each node's measured route and links and the
        neighbours its scan answer named, plus ``fresh`` ``((a, b), link)``
        pairs measured just now; each costs the relays' ETX, book links
        ``detour_margin`` dB worse. Only links ``usable(a, b)`` allows, at
        most ``longest`` hops. ``()`` when the target is the gateway's neighbour.
        """

        import heapq

        edges: dict[int, dict[int, float]] = {}

        def add(a: int, b: int, link: LinkQuality, stale: bool = True) -> None:
            if stale:
                # Measured once, maybe long ago, and a scan answer names its
                # strongest neighbours only: trust it ``detour_margin`` dB less
                # (measured: detours over links that looked fine and had a
                # median margin of −1.5–2 dB failed and cost a timeout).
                quarter = max(-128, link.snr - round(4 * detour_margin))
                link = LinkQuality(max(-255, link.rssi - round(detour_margin)), quarter)
            edge_cost = cost((link,))
            for x, y in ((a, b), (b, a)):
                row = edges.setdefault(x, {})
                row[y] = min(row.get(y, math.inf), edge_cost)

        for node in self.nodes.values():
            if node.measured:
                path = (self.controller_id, *node.route, node.node_id)
                for (a, b), link in zip(zip(path, path[1:]), node.links):
                    add(a, b, link)
            for hop in node.neighbours:
                add(node.node_id, hop.node_id, hop.link)
        for (a, b), link in fresh:
            add(a, b, link, stale=False)

        best ={self.controller_id: 0.0}
        before: dict[int, int] = {}
        hops = {self.controller_id: 0}
        queue = [(0.0, self.controller_id)]
        while queue:
            cost, node = heapq.heappop(queue)
            if node == target:
                break
            if cost > best.get(node, math.inf) or (node != self.controller_id and node == target):
                continue
            for other, step in edges.get(node, {}).items():
                if not usable(node, other) or hops[node] + 1 > longest:
                    continue
                if cost + step < best.get(other, math.inf):
                    best[other], before[other], hops[other] = cost + step, node, hops[node] + 1
                    heapq.heappush(queue, (cost + step, other))
        if target not in before:
            return None
        relays, node = [], before[target]
        while node != self.controller_id:
            relays.append(node)
            node = before[node]
        relays.reverse()
        return tuple(relays)

    def weak_links(self, threshold: float, floor: float) -> list[WeakLink]:
        """Measured hops with less than ``threshold`` dB margin, weakest first.

        Uses what probes, JOINs and scan answers already carry (each hop's
        smoothed SNR): no frame goes out. Per node the latest measurement
        counts: the node book or the last probed session path. Margin is the SNR above the base
        SF's limit, as in ``_choose_sf``; the default threshold is
        ``sf_margin``, below which a session has to slow down. Measured: on
        unplanned layouts the probed path is within about 0.6 dB of the best
        the radios allow, so a weak hop here means the layout needs a relay,
        not a better route.
        """

        paths: dict[int, tuple[tuple[int, ...], tuple[LinkQuality, ...], float]] = {}
        for node, known in self.nodes.items():
            if known.measured and known.links:
                paths[node] = ((self.controller_id, *known.route, node), known.links, known.seen_at)
        for node, probed in self.probed.items():
            if node not in paths or probed[2] >= paths[node][2]:
                paths[node] = probed

        latest: dict[tuple[int, int], tuple[int, int, float, float]] = {}
        carries: dict[tuple[int, int], set[int]] = {}
        bottleneck: dict[tuple[int, int], set[int]] = {}
        for node, (path, links, at) in paths.items():
            hops = list(zip(path, path[1:]))
            if len(hops) != len(links):
                continue  # a book entry whose route and links disagree: nothing to stand on
            margins = []
            for (a, b), link in zip(hops, links):
                key = (min(a, b), max(a, b))
                margin = margin_db(link.snr_db, floor)
                if key not in latest or at >= latest[key][3]:
                    latest[key] = (a, b, margin, at)
                carries.setdefault(key, set()).add(node)
                margins.append((margin, key))
            bottleneck.setdefault(min(margins)[1], set()).add(node)

        report = [
            WeakLink(a, b, margin, at, tuple(sorted(bottleneck.get(key, ()))), tuple(sorted(carries[key])))
            for key, (a, b, margin, at) in latest.items()
            if margin < threshold
        ]
        return sorted(report, key=lambda weak: (weak.margin_db, -len(weak.carries)))

    def _link_map(self, floor: float) -> dict[tuple[int, int], float]:
        """Every link margin (dB over the SF7 limit) the gateway has measured: node book and probed paths.

        Links are taken as symmetric; of two measurements of one link the
        lower counts. A scan answer lists only a node's strongest neighbours
        (``MAX_NEIGHBOURS``), so detours come out fewer than there are:
        acceptance errs on the strict side.
        """

        links: dict[tuple[int, int], float] = {}

        def put(a: int, b: int, margin: float) -> None:
            key = (min(a, b), max(a, b))
            links[key] = min(links.get(key, margin), margin)

        for node, known in self.nodes.items():
            for hop in known.neighbours:
                put(node, hop.node_id, margin_db(hop.link.snr_db, floor))
            path = (self.controller_id, *known.route, node)
            if known.measured and len(known.links) == len(path) - 1:
                for (a, b), quality in zip(zip(path, path[1:]), known.links):
                    put(a, b, margin_db(quality.snr_db, floor))
        for path, qualities, _ in self.probed.values():
            if len(qualities) == len(path) - 1:
                for (a, b), quality in zip(zip(path, path[1:]), qualities):
                    put(a, b, margin_db(quality.snr_db, floor))
        return links
