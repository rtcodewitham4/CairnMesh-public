package rxfilter

import (
	"os/exec"
	"strings"
	"testing"

	"cairnmesh/firmware/console"
)

type fakePort struct {
	in   []byte
	out  strings.Builder
	conn bool
}

func (f *fakePort) Connected() bool { return f.conn }
func (f *fakePort) Buffered() int   { return len(f.in) }
func (f *fakePort) ReadByte() (byte, error) {
	b := f.in[0]
	f.in = f.in[1:]
	return b, nil
}
func (f *fakePort) Write(p []byte) int       { f.out.Write(p); return len(p) }
func (f *fakePort) WriteString(s string) int { f.out.WriteString(s); return len(s) }
func (f *fakePort) Writable() int            { return 1 << 20 }

type rig struct {
	c       *console.Console
	p       *fakePort
	f       *Filter
	self    byte
	running bool
	now     int64
}

func newRig(self byte) *rig {
	r := &rig{p: &fakePort{conn: true}, f: &Filter{}, self: self, running: true}
	r.c = &console.Console{}
	r.c.Init(r.p)
	r.c.Handle("rxf", "", func(c *console.Console, args [][]byte) { Command(c, args, r.self, r.running, r.f) })
	r.c.Poll(0)
	r.c.Poll(console.GreetDelay)
	r.p.out.Reset()
	return r
}

// run types a line and returns what the command printed (the echo and the prompt cut off).
func (r *rig) run(line string) string {
	r.p.out.Reset()
	r.p.in = []byte(line + "\r")
	r.c.Poll(console.GreetDelay + 1)
	out := r.p.out.String()
	if i := strings.Index(out, "\r\n"); i >= 0 {
		out = out[i+2:]
	}
	return strings.TrimSuffix(out, "> ")
}

func TestDefaultPassesEverything(t *testing.T) {
	var f Filter
	if f.Active() {
		t.Fatal("the zero filter judges frames")
	}
	for tx := 0; tx < 256; tx++ {
		if !f.Allows(byte(tx)) {
			t.Fatalf("default drops TX %d", tx)
		}
	}
}

func TestCapsAndStatusReadback(t *testing.T) {
	r := newRig(2)
	if got := r.run("rxf caps"); !strings.HasPrefix(got, "rxf caps 1 key=tx ") || !strings.Contains(got, "atomic") {
		t.Fatalf("caps: %q", got)
	}
	got := r.run("rxf")
	for _, want := range []string{"rxf TESTFILTER v1", "node 2", "gen 0", "mode all", "asym-ok no", "dropped 0"} {
		if !strings.Contains(got, want) {
			t.Fatalf("status lacks %q: %q", want, got)
		}
	}
}

func TestTopoIsSymmetricAndReadBack(t *testing.T) {
	// The same line given to every node: 66-1, 1-2, 2-3 (a line of four).
	hears := map[byte][]byte{66: {1}, 1: {2, 66}, 2: {1, 3}, 3: {2}, 4: nil}
	for self, want := range hears {
		r := newRig(self)
		got := r.run("rxf topo 66-1,2-1 3-2")
		if !strings.Contains(got, "mode topo") || !strings.Contains(got, "gen 1") ||
			!strings.Contains(got, "rxf links 1-66,1-2,2-3") {
			t.Fatalf("node %d readback: %q", self, got)
		}
		for tx := 0; tx < 256; tx++ {
			heard := false
			for _, w := range want {
				heard = heard || int(w) == tx
			}
			if r.f.Allows(byte(tx)) != heard {
				t.Fatalf("node %d: Allows(%d) = %v, want %v", self, tx, !heard, heard)
			}
		}
	}
	// The relation is reciprocal: A hears B exactly when B hears A.
	all := []byte{1, 2, 3, 4, 66}
	rigs := map[byte]*rig{}
	for _, n := range all {
		rigs[n] = newRig(n)
		rigs[n].run("rxf topo 66-1,1-2,2-3")
	}
	for _, a := range all {
		for _, b := range all {
			if a != b && rigs[a].f.Allows(b) != rigs[b].f.Allows(a) {
				t.Fatalf("topo is not symmetric between %d and %d", a, b)
			}
		}
	}
}

func TestTopoRefusalsChangeNothing(t *testing.T) {
	r := newRig(1)
	r.run("rxf topo 1-2")
	before := r.run("rxf")
	for _, bad := range []string{
		"rxf topo 1>2",       // one-way written into a symmetric list
		"rxf topo 1-2,2<3",   // the first link was fine: all or nothing
		"rxf topo 1-2,3-3",   // a node with itself
		"rxf topo 1-2,0-3",   // ID 0
		"rxf topo 1-2,255-3", // ID 255 is broadcast
		"rxf topo 1-2,x-3",
		"rxf topo 1-2,,3-4",
		"rxf topo 1-2-3",
		"rxf topo",
	} {
		got := r.run(bad)
		if !strings.Contains(got, "nothing changed") && !strings.Contains(got, "bad link") {
			t.Fatalf("%q: not refused: %q", bad, got)
		}
		if after := r.run("rxf"); after != before {
			t.Fatalf("%q changed the state:\n%q\n%q", bad, before, after)
		}
	}
	if got := r.run("rxf topo 1>2"); !strings.Contains(got, "one-way is 'rxf asym' only") {
		t.Fatalf("one-way refusal does not point at asym: %q", got)
	}
	// Too many links (a console line is too short to say it, so parse directly).
	var cfg config
	var words [][]byte
	for i := 2; i < 2+MaxLinks+1; i++ {
		words = append(words, []byte("1-"+itoa(i)))
	}
	if msg := parseLinks(&cfg, words); !strings.Contains(msg, "too many") {
		t.Fatalf("over MaxLinks: %q", msg)
	}
}

func itoa(n int) string {
	if n < 10 {
		return string(rune('0' + n))
	}
	return itoa(n/10) + string(rune('0'+n%10))
}

func TestAsymNeedsTheSwitchAndANameAndIsMarkedNonPhysical(t *testing.T) {
	r := newRig(3)
	if got := r.run("rxf asym oneway 1,2"); !strings.Contains(got, "asym is off") || r.f.Active() {
		t.Fatalf("asym took effect with the switch off: %q", got)
	}
	r.run("rxf asym-ok yes")
	for _, bad := range []string{"rxf asym 1,2", "rxf asym BAD 1", "rxf asym ok", "rxf asym ok 1,x", "rxf asym ok 1,", "rxf asym ok 0", "rxf asym averyveryverylongname 1"} {
		if got := r.run(bad); r.f.Active() {
			t.Fatalf("%q took effect: %q", bad, got)
		}
	}
	got := r.run("rxf asym oneway 1,2")
	if !strings.Contains(got, "mode asym NONPHYSICAL name oneway") || !strings.Contains(got, "rxf hear 1,2") {
		t.Fatalf("asym readback: %q", got)
	}
	if !r.f.Allows(1) || !r.f.Allows(2) || r.f.Allows(4) {
		t.Fatal("asym row wrong")
	}
	// Closing the switch ends a live one-way row.
	got = r.run("rxf asym-ok no")
	if r.f.Active() || !strings.Contains(got, "mode all") {
		t.Fatalf("a one-way matrix survived its switch: %q", got)
	}
	// "all" also clears the switch.
	r.run("rxf asym-ok yes")
	r.run("rxf all")
	if got := r.run("rxf"); !strings.Contains(got, "asym-ok no") || !strings.Contains(got, "mode all") {
		t.Fatalf("all: %q", got)
	}
}

func TestNotRunningRefusesChangesButAnswersCaps(t *testing.T) {
	r := newRig(0)
	r.running = false
	if got := r.run("rxf caps"); !strings.HasPrefix(got, "rxf caps 1") {
		t.Fatalf("caps: %q", got)
	}
	r.run("rxf asym-ok yes")
	for _, line := range []string{"rxf topo 1-2", "rxf asym x 1"} {
		if got := r.run(line); !strings.Contains(got, "not running") || r.f.Active() {
			t.Fatalf("%q: %q", line, got)
		}
	}
	if got := r.run("rxf"); !strings.Contains(got, "(node not running)") {
		t.Fatalf("status: %q", got)
	}
}

func TestLogRecordsDroppedFramesOnlyAndPages(t *testing.T) {
	r := newRig(2)
	r.run("rxf topo 1-2")
	if got := r.run("rxf log"); got != "rxf log next 1 lost 0 total 0\r\n" {
		t.Fatalf("empty log: %q", got)
	}
	for i := 1; i <= 20; i++ {
		r.f.Record(Drop{Sender: 5, TX: 7, Kind: 0x04, Fcnt: uint32(100 + i), RSSI: -90, HasRSSI: true, MIC: [4]byte{1, 2, 3, byte(i)}})
	}
	r.f.Record(Drop{Sender: 6, TX: 9, Kind: 0x0c, Fcnt: 7}) // no RSSI measured
	// 21 dropped, 16 kept: 6..21; ask from the start.
	got := r.run("rxf log 1")
	if !strings.HasPrefix(got, "d 6 sender 5 tx 7 kind 04 fcnt 106 rssi -90 mic 01020306\r\n") {
		t.Fatalf("first page: %q", got)
	}
	if !strings.Contains(got, "rxf log next 12 lost 5 total 21") || strings.Count(got, "\r\nd ")+1 != logPage {
		t.Fatalf("paging: %q", got)
	}
	next := r.run("rxf log 18")
	if strings.Count(next, "\r\nd ")+1 != 4 || !strings.Contains(next, "d 21 sender 6 tx 9 kind 0c fcnt 7 rssi ? mic 00000000") ||
		!strings.Contains(next, "rxf log next 22 lost 0 total 21") {
		t.Fatalf("last page: %q", next)
	}
	if got := r.run("rxf log 22"); got != "rxf log next 22 lost 0 total 21\r\n" {
		t.Fatalf("past the end: %q", got)
	}
	// A line of the log fits what the console prints per command.
	if len(got) > console.MaxOutput || len(r.run("rxf log 1")) > console.MaxOutput {
		t.Fatalf("one page is longer than the console's MaxOutput")
	}
	r.run("rxf log clear")
	if got := r.run("rxf log"); got != "rxf log next 1 lost 0 total 0\r\n" {
		t.Fatalf("after clear: %q", got)
	}
}

func TestStatusFitsTheConsoleOutput(t *testing.T) {
	r := newRig(1)
	r.run("rxf asym-ok yes")
	list := "rxf asym longname "
	for id := 2; id < 30; id++ {
		list += itoa(id) + ","
	}
	r.run(strings.TrimSuffix(list, ","))
	if got := r.run("rxf"); len(got) > console.MaxOutput {
		t.Fatalf("status is %d bytes", len(got))
	}
}

// No production package may import rxfilter: the default build of the node
// and of the firmware programs does not link it. (The images are checked by
// make wle5-assert-prod; this is the same on the host.)
func TestDefaultBuildsDoNotDependOnIt(t *testing.T) {
	for _, pkg := range []string{"cairnmesh/firmware/node", "cairnmesh/firmware/proto", "cairnmesh/firmware/mesh", "cairnmesh/firmware/board/wle5/adapt"} {
		out, err := exec.Command("go", "list", "-deps", pkg).CombinedOutput()
		if err != nil {
			t.Skipf("go list: %v: %s", err, out)
		}
		if strings.Contains(string(out), "firmware/rxfilter") {
			t.Fatalf("%s depends on rxfilter in a default build", pkg)
		}
	}
	// With the tag it does (so the check above is not vacuous).
	out, err := exec.Command("go", "list", "-tags", "cairn_testfilter", "-deps", "cairnmesh/firmware/node").CombinedOutput()
	if err != nil {
		t.Skipf("go list: %v: %s", err, out)
	}
	if !strings.Contains(string(out), "firmware/rxfilter") {
		t.Fatal("node with the tag does not use rxfilter: the hook is not wired")
	}
}

// The console keeps 120 characters of a longer line. A topology cut after an
// earlier link is still a valid list, so rxf must refuse such a line, whatever
// it says, and change nothing.
func TestLongLineIsRefusedNotTruncated(t *testing.T) {
	r := newRig(1)
	r.run("rxf topo 1-2")
	before := r.run("rxf")
	links := "rxf topo 1-2,1-3,1-4,1-5,1-6,1-7,1-8,1-9,1-10,1-11,1-12,1-13,1-14,1-15,1-16,1-17,1-18,1-19,1-20,1-21,1-22,1-23,1-24,1-25,1-26,1-27"
	if len(links) <= 120 {
		t.Fatalf("test line is %d long", len(links))
	}
	got := r.run(links)
	if !strings.Contains(got, "nothing changed") || !strings.Contains(got, "120") {
		t.Fatalf("long topo: %q", got)
	}
	if after := r.run("rxf"); after != before {
		t.Fatalf("a long line changed the state:\n%q\n%q", before, after)
	}
	r.run("rxf asym-ok yes")
	long := "rxf asym " + strings.Repeat("a", 12) + " " + strings.Repeat("2,", 50) + "3"
	if got := r.run(long); !strings.Contains(got, "nothing changed") || r.f.cfg.mode == Asym {
		t.Fatalf("long asym: %q", got)
	}
	// A short line is not refused.
	ok := "rxf topo 1-2"
	r.run(ok)
	if got := r.run(ok); strings.Contains(got, "line longer") {
		t.Fatalf("a short line was refused: %q", got)
	}
}
