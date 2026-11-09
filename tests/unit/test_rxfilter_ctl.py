"""The test receive filter's bench script (``firmware/tools/rxfilter_ctl.py``) against the firmware's own command.

The node side is ``firmware/tools/rxsim``: the Go ``rxf`` command (firmware/rxfilter) behind a console,
on the host, so the script reads what the board prints. What is held here: the capability query
decides (an old image, a default build or a silent port FAILS), a change is accepted only when the
node's readback matches the request, a one-way link cannot be written into a topology, and the log
pages are read to the end.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "firmware" / "tools"))
import rxfilter_ctl as R  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("go") is None, reason="needs the Go toolchain (firmware/tools/rxsim)")


@pytest.fixture(scope="module")
def rxsim(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("rxsim") / "rxsim"
    subprocess.run(["go", "build", "-o", str(out), "./tools/rxsim"], cwd=ROOT / "firmware", check=True)
    return out


class Sim:
    """One simulated node console; ``send(line)`` is what Rxf takes."""

    def __init__(self, binary: Path, *flags: str) -> None:
        self.p = subprocess.Popen([str(binary), *flags], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        self.sent: list[str] = []

    def send(self, line: str) -> str:
        self.sent.append(line)
        assert self.p.stdin and self.p.stdout
        self.p.stdin.write(line + "\n")
        self.p.stdin.flush()
        out = []
        while (row := self.p.stdout.readline()) and row.strip() != "--END--":
            out.append(row)
        return "".join(out)

    def close(self) -> None:
        assert self.p.stdin
        self.p.stdin.close()
        self.p.wait(timeout=5)


@pytest.fixture
def node(rxsim):
    made = []

    def make(*flags: str) -> Sim:
        s = Sim(rxsim, *flags)
        made.append(s)
        return s

    yield make
    for s in made:
        s.close()


def test_caps_answered(node):
    assert "key=tx" in R.Rxf(node("-id", "2").send).caps()


@pytest.mark.parametrize("reply", ["", "unknown command; type help\n", "garbage\n"])
def test_no_capability_answer_is_a_failure_not_a_pass(reply):
    # An old image, a default build (no rxf command) or a silent port.
    sent: list[str] = []

    def send(line: str) -> str:
        sent.append(line)
        return reply

    rxf = R.Rxf(send)
    for call in (rxf.caps, lambda: rxf.topo("1-2"), lambda: rxf.asym("x", [2]), rxf.reset, rxf.drops):
        with pytest.raises(R.NoCapability):
            call()
    assert all(line == "rxf caps" for line in sent), f"a change was sent to a node that does not answer caps: {sent}"


def test_old_image_through_the_real_console(node):
    s = node("-old")
    with pytest.raises(R.NoCapability, match="unknown command"):
        R.Rxf(s.send).topo("1-2")
    assert s.sent == ["rxf caps"]


def test_version_other_than_ours_is_a_failure():
    with pytest.raises(R.NoCapability, match="version 2"):
        R.Rxf(lambda line: "rxf caps 2 key=tx atomic readback\n").caps()


@pytest.mark.parametrize("node_id, hear", [(66, [1]), (1, [2, 66]), (2, [1, 3]), (3, [2]), (9, [])])
def test_topo_is_read_back_per_node(node, node_id, hear):
    s = node("-id", str(node_id))
    st = R.Rxf(s.send).topo("66-1,2-1 3-2")
    assert st["mode"] == "topo" and st["node"] == node_id and st["hear"] == hear
    assert st["links"] == [(1, 66), (1, 2), (2, 3)] and st["gen"] == 1


def test_topo_readback_mismatch_is_a_failure(node):
    s = node("-id", "1")

    def stale(line: str) -> str:
        reply = s.send(line)
        return reply.replace("rxf hear 2,66", "rxf hear 2") if line.startswith("rxf topo") else reply

    with pytest.raises(R.RxfError, match="readback does not match"):
        R.Rxf(stale).topo("1-2,1-66")


def test_one_way_cannot_be_written_into_topo(node):
    s = node("-id", "1")
    with pytest.raises(R.RxfError, match="both ways"):
        R.Rxf(s.send).topo("1>2")
    assert s.sent == []  # refused before anything went to the node
    # And the node itself refuses it too (a hand-typed line).
    assert "one-way is 'rxf asym' only" in s.send("rxf topo 1>2")


def test_stopped_node_does_not_take_a_topology(node):
    s = node("-id", "0", "-stopped")
    with pytest.raises(R.RxfError):
        R.Rxf(s.send).topo("1-2")
    assert "mode all" in s.send("rxf")


def test_asym_is_named_and_marked_and_reset_clears_it(node):
    s = node("-id", "3")
    rxf = R.Rxf(s.send)
    st = rxf.asym("hidden-a", [2, 66])
    assert st["mode"] == "asym" and st["name"] == "hidden-a" and st["hear"] == [2, 66] and st["asym_ok"]
    assert "NONPHYSICAL" in s.send("rxf")
    st = rxf.reset()
    assert st["mode"] == "all" and not st["asym_ok"]
    with pytest.raises(R.RxfError, match="a name"):
        rxf.asym("Bad Name", [2])


def test_asym_without_the_switch_is_refused_by_the_node(node):
    s = node("-id", "3")
    s.send("rxf caps")
    assert "asym is off" in s.send("rxf asym x 2")
    assert "mode all" in s.send("rxf")


def test_drops_are_read_to_the_end(node):
    s = node("-id", "2")
    s.send("rxf topo 1-2")
    for i in range(20):
        s.send(f"!drop 66 7 33 {100 + i} {-80 - i if i % 5 else '?'}")
    got = R.Rxf(s.send).drops()
    # 16 are kept, the oldest 4 are gone; every kept one is read once, in order.
    assert [d["n"] for d in got] == list(range(5, 21))
    assert got[0]["fcnt"] == 104 and got[0]["sender"] == 66 and got[0]["tx"] == 7 and got[0]["kind"] == 0x21
    assert got[0]["rssi"] == -84 and got[1]["rssi"] is None
    assert len({d["mic"] for d in got}) == 16  # the frame's own hash tells the copies apart


def test_status_parse():
    st = R.parse_status(
        "rxf TESTFILTER v1 node 4 gen 3 mode asym NONPHYSICAL name nolink asym-ok yes passed 2 dropped 5 unauth 1\n"
        "rxf hear 7\n"
    )
    assert (st["node"], st["gen"], st["mode"], st["name"], st["asym_ok"], st["hear"]) == (4, 3, "asym", "nolink", True, [7])
    with pytest.raises(R.RxfError):
        R.parse_status("something else")


def test_asym_mismatch_rolls_back_to_the_previous_state(node):
    s = node("-id", "3")
    rxf = R.Rxf(s.send)
    rxf.topo("3-1,3-2")  # the state to come back to
    before = rxf.status()

    def stale(line: str) -> str:
        reply = s.send(line)
        return reply.replace("rxf hear 2,66", "rxf hear 2") if line.startswith("rxf asym ") else reply

    with pytest.raises(R.RxfError, match="rolled back to mode topo"):
        R.Rxf(stale).asym("x", [2, 66])
    now = rxf.status()
    assert (now["mode"], now["links"], now["hear"], now["asym_ok"]) == (before["mode"], before["links"], before["hear"], before["asym_ok"])
    assert not now["asym_ok"]  # the switch the script opened is closed again


def test_asym_failure_from_default_restores_all_and_closes_the_switch(node):
    s = node("-id", "3")

    def refuse(line: str) -> str:
        return "rxf: refused\n" if line.startswith("rxf asym ") else s.send(line)

    with pytest.raises(R.RxfError, match="rolled back to mode all"):
        R.Rxf(refuse).asym("x", [2])
    st = R.Rxf(s.send).status()
    assert st["mode"] == "all" and not st["asym_ok"]


def test_topo_mismatch_rolls_back(node):
    s = node("-id", "1")
    rxf = R.Rxf(s.send)
    rxf.topo("1-2")

    def stale(line: str) -> str:
        reply = s.send(line)
        return reply.replace("rxf hear 3,66", "rxf hear 3") if line.startswith("rxf topo") else reply

    with pytest.raises(R.RxfError, match="rolled back to mode topo"):
        R.Rxf(stale).topo("1-3,1-66")
    st = rxf.status()
    assert st["mode"] == "topo" and st["links"] == [(1, 2)] and st["hear"] == [2]


def test_asym_line_too_long_is_refused_before_anything_is_sent(node):
    s = node("-id", "3")
    with pytest.raises(R.RxfError, match="too long"):
        R.Rxf(s.send).asym("longname", list(range(100, 140)))
    assert s.sent == []


def test_refused_change_is_not_rolled_back(node):
    # The node refuses and keeps its old topology: no `rxf all`, no second topo, state untouched.
    s = node("-id", "1")
    rxf = R.Rxf(s.send)
    rxf.topo("1-2")
    before = rxf.status()
    s.sent.clear()

    def refuse(line: str) -> str:
        return "node is not running (no ID yet): nothing changed\n" if line.startswith("rxf topo ") else s.send(line)

    with pytest.raises(R.RxfError, match="state is unchanged"):
        R.Rxf(refuse).topo("1-3")
    assert "rxf all" not in s.sent and sum(1 for x in s.sent if x.startswith("rxf topo")) == 0
    assert rxf.status() == before


def test_refused_asym_closes_only_its_own_switch(node):
    s = node("-id", "1")
    rxf = R.Rxf(s.send)
    rxf.topo("1-2")
    before = rxf.status()
    s.sent.clear()

    def refuse(line: str) -> str:
        return "rxf: refused\n" if line.startswith("rxf asym ") else s.send(line)

    with pytest.raises(R.RxfError, match="rolled back to mode topo"):
        R.Rxf(refuse).asym("x", [2])
    assert "rxf all" not in s.sent  # the topology was never opened up
    assert "rxf asym-ok no" in s.sent
    now = rxf.status()
    assert (now["mode"], now["links"], now["hear"], now["asym_ok"]) == (before["mode"], before["links"], before["hear"], False)


def test_failed_rollback_is_reported_not_silent(node):
    s = node("-id", "1")
    rxf = R.Rxf(s.send)
    rxf.topo("1-2")
    topo_lines = []

    def flaky(line: str) -> str:
        if line.startswith("rxf topo "):
            topo_lines.append(line)
            if len(topo_lines) == 1:  # the change: reads back wrong
                return s.send(line).replace("rxf hear 3,66", "rxf hear 3")
            return "rxf: refused\n"  # the rollback's topo is lost
        return s.send(line)

    with pytest.raises(R.RxfError, match="ROLLBACK FAILED"):
        R.Rxf(flaky).topo("1-3,1-66")
    # The node really is not on its old topology, and the error said so.
    assert rxf.status()["mode"] == "all"
