package journal

import (
	"testing"

	"cairnmesh/firmware/proto"
)

// fakeFlash behaves like the STM32U0's: a double word is written only over
// erased bytes. Power is cut at operation cutAt (0: never): that operation
// and every later one fail, the cut one left as torn says.
type fakeFlash struct {
	pages      map[int]*[PageSize]byte
	bad        map[[2]int]bool // double words whose ECC fails
	ops, cutAt int
	torn       tear
	erases     int
}

type tear int

const (
	clean   tear = iota // the cut operation did nothing
	halfway             // half its bytes changed
	eccFail             // and the double words it touched fail their ECC
)

var tears = []tear{clean, halfway, eccFail}

func newFlash() *fakeFlash {
	return &fakeFlash{pages: map[int]*[PageSize]byte{}, bad: map[[2]int]bool{}}
}

func (f *fakeFlash) page(p int) *[PageSize]byte {
	if f.pages[p] == nil {
		f.pages[p] = new([PageSize]byte)
		for i := range f.pages[p] {
			f.pages[p][i] = 0xFF
		}
	}
	return f.pages[p]
}

// alive counts an operation; false from the cut on.
func (f *fakeFlash) alive() bool {
	f.ops++
	return f.cutAt == 0 || f.ops < f.cutAt
}

func (f *fakeFlash) Erase(p int) bool {
	pg := f.page(p)
	if !f.alive() {
		if f.torn != clean && f.ops == f.cutAt {
			for i := 0; i < PageSize/2; i++ {
				pg[i] = 0xFF
				f.bad[[2]int{p, i &^ 7}] = f.torn == eccFail
			}
		}
		return false
	}
	f.erases++
	for i := range pg {
		pg[i] = 0xFF
		delete(f.bad, [2]int{p, i &^ 7})
	}
	return true
}

func (f *fakeFlash) Program(p, offset int, dw *[8]byte) bool {
	pg := f.page(p)
	for _, b := range pg[offset : offset+8] {
		if b != 0xFF {
			return false // PROGERR: not erased
		}
	}
	if !f.alive() {
		if f.torn != clean && f.ops == f.cutAt {
			copy(pg[offset:offset+4], dw[:4])
			f.bad[[2]int{p, offset}] = f.torn == eccFail
		}
		return false
	}
	copy(pg[offset:offset+8], dw[:])
	return true
}

func (f *fakeFlash) Read(p, offset int, dw *[8]byte) bool {
	copy(dw[:], f.page(p)[offset:offset+8])
	if f.bad[[2]int{p, offset}] {
		dw[3] ^= 0x5A // what comes back is garbage
		return false
	}
	return true
}

// reboot: power is back.
func (f *fakeFlash) reboot() { f.ops, f.cutAt = 0, 0 }

func (f *fakeFlash) dead() bool { return f.cutAt != 0 && f.ops >= f.cutAt }

// ---- records ----

type countApp struct{ runs map[uint16]int }

func (countApp) Duration(byte, byte) proto.Time { return 0 }

func (a countApp) Perform(r proto.Request) byte {
	a.runs[r.ID]++
	return proto.ResultOK
}

type answers struct{ last []byte }

func (a *answers) Send(plain []byte) {
	if plain[0] == proto.KindReply {
		a.last = append(a.last[:0], plain...)
	}
}

type node struct {
	d   proto.Device
	rec *Records
	app countApp
	out answers
	cfg proto.DeviceConfig
}

const nodeID = 5

func boot(f *fakeFlash, pages int, app countApp) *node {
	n := &node{app: app}
	n.cfg = proto.DefaultDeviceConfig(nodeID)
	n.cfg.Awake, n.cfg.HeartbeatInterval = true, proto.Never
	n.d.Init(n.cfg, n.app, &n.out, proto.Midpoint{}, 0)
	n.rec = NewRecords(Where{Flash: f, First: 10, Pages: pages})
	log := n.rec.Load(&n.d)
	n.d.Reset(0)
	n.d.SetLog(log)
	return n
}

// ask sends REQUEST id from sender 1 and returns the answer (nil: none).
func (n *node) ask(id uint16) []byte {
	n.out.last = nil
	n.d.OnFrame([]byte{proto.KindRequest, 1, nodeID, byte(id), byte(id >> 8), 1, 0}, 0)
	n.d.Tick(0)
	return n.out.last
}

func TestRecordsThroughCompactions(t *testing.T) {
	f := newFlash()
	runs := map[uint16]int{}
	n := boot(f, 1, countApp{runs})
	for id := uint16(0); id < 600; id++ { // 2 writes each: several compactions
		if n.ask(id) == nil {
			t.Fatalf("request %d not answered", id)
		}
	}
	if n.rec.J.Stats.Compactions < 4 || n.rec.J.Stats.Failed != 0 {
		t.Fatalf("stats %+v", n.rec.J.Stats)
	}
	n = boot(f, 1, countApp{runs})
	for id := uint16(596); id < 600; id++ {
		if a := n.ask(id); a == nil || a[5] != proto.ResultOK || runs[id] != 1 {
			t.Fatalf("request %d after the reboot: answer %x, ran %d times", id, a, runs[id])
		}
	}
}

// Power is cut at every flash operation in turn, torn or not: a request
// that ran is never run again by a late copy (spec 4.1), as long as it is
// among the last DedupWindow ones.
func TestRecordsPowerCutAnywhere(t *testing.T) {
	for _, torn := range tears {
		total := 0
		for cut := 1; ; cut++ {
			f := newFlash()
			f.cutAt, f.torn = cut, torn
			runs := map[uint16]int{}
			n := boot(f, 1, countApp{runs})
			var ran []uint16
			for id := uint16(0); id < 300 && !f.dead(); id++ {
				n.ask(id)
				if runs[id] > 0 {
					ran = append(ran, id)
				}
			}
			if !f.dead() {
				total = cut
				break
			}
			f.reboot()
			n = boot(f, 1, countApp{runs})
			for _, id := range ran[max(0, len(ran)-n.cfg.DedupWindow):] {
				a := n.ask(id)
				if runs[id] != 1 || a == nil {
					t.Fatalf("torn %d, cut at %d: request %d ran %d times, answer %x", torn, cut, id, runs[id], a)
				}
			}
		}
		if total < 600 {
			t.Fatalf("only %d operations: no compaction covered", total)
		}
	}
}

// ---- marks ----

func TestMarksPowerCutAnywhere(t *testing.T) {
	const steps = 700 // over two compactions of a 1-page bank
	for _, torn := range tears {
		for cut := 1; cut < steps+20; cut++ {
			f := newFlash()
			f.cutAt, f.torn = cut, torn
			var sec proto.Secure
			m := NewMarks(Where{Flash: f, First: 20, Pages: 1})
			store := m.Load(&sec)
			acked := map[byte]uint32{}
			tried := map[byte]uint32{}
			for i := uint32(1); i <= steps && !f.dead(); i++ {
				sender := byte(i % 7)
				sec.RestoreMark(sender, i) // as if a frame was accepted
				tried[sender] = i
				if store.Mark(sender, i) {
					acked[sender] = i
				}
			}
			f.reboot()
			var again proto.Secure
			NewMarks(Where{Flash: f, First: 20, Pages: 1}).Load(&again)
			for s := byte(0); s < 7; s++ {
				last, ok := again.Window(s).Last()
				if acked[s] > 0 && (!ok || last < acked[s] || last > tried[s]) {
					t.Fatalf("torn %d, cut %d: sender %d restored %d (%v), stored %d, tried %d", torn, cut, s, last, ok, acked[s], tried[s])
				}
			}
		}
	}
}

func TestMarksWearAndReplay(t *testing.T) {
	f := newFlash()
	var sec proto.Secure
	m := NewMarks(Where{Flash: f, First: 20, Pages: 2})
	store := m.Load(&sec)
	for i := uint32(1); i <= 5000; i++ {
		sec.RestoreMark(byte(i%41), i)
		if !store.Mark(byte(i%41), i) {
			t.Fatal("mark not stored")
		}
	}
	// 41 live marks in a 511-entry bank: a compaction every ~470 marks, 2 pages each.
	if c := m.J.Stats.Compactions; c < 9 || c > 12 || f.erases != 2*int(c) {
		t.Fatalf("compactions %d, erases %d", c, f.erases)
	}
	var again proto.Secure
	NewMarks(Where{Flash: f, First: 20, Pages: 2}).Load(&again)
	for s := 0; s < 41; s++ {
		want := uint32(5000 - (5000-s)%41)
		if last, _ := again.Window(byte(s)).Last(); last != want {
			t.Fatalf("sender %d: %d, want %d", s, last, want)
		}
	}
}

func TestOtherKindAndGarbageIgnored(t *testing.T) {
	f := newFlash()
	var sec proto.Secure
	store := NewMarks(Where{Flash: f, First: 20, Pages: 1}).Load(&sec)
	sec.RestoreMark(3, 77)
	store.Mark(3, 77)
	// The same pages read as records: nothing (a different kind).
	var d proto.Device
	d.Init(proto.DefaultDeviceConfig(nodeID), countApp{map[uint16]int{}}, &answers{}, proto.Midpoint{}, 0)
	r := NewRecords(Where{Flash: f, First: 20, Pages: 1})
	r.Load(&d)
	if r.J.active >= 0 {
		t.Fatal("a marks bank taken for records")
	}
	// Garbage where a header would be: no bank.
	g := newFlash()
	g.page(30)[0] = tagHeader
	var s2 proto.Secure
	m := NewMarks(Where{Flash: g, First: 30, Pages: 1})
	m.Load(&s2)
	if m.J.active >= 0 {
		t.Fatal("garbage taken for a header")
	}
}

// Pages erased ahead (Prepare) leave a compaction nothing to erase, and a
// power cut at any flash operation, a Prepare's erase too, loses no mark
// that was stored.
func TestPrepareErasesAheadAndSurvivesCuts(t *testing.T) {
	f := newFlash()
	var sec proto.Secure
	m := NewMarks(Where{Flash: f, First: 20, Pages: 2})
	store := m.Load(&sec)
	stalled := 0
	for i := uint32(1); i <= 3000; i++ {
		sec.RestoreMark(1, i)
		before, compactions := f.erases, m.J.Stats.Compactions
		if !store.Mark(1, i) {
			t.Fatal("mark not stored")
		}
		if m.J.Stats.Compactions > compactions && compactions > 0 && f.erases != before {
			stalled++
		}
		for m.Prepare() {
		}
	}
	if m.J.Stats.Compactions < 5 || stalled != 0 {
		t.Fatalf("compactions %d, %d of them erased pages themselves", m.J.Stats.Compactions, stalled)
	}

	const steps = 1300 // over two compactions of a 2-page bank
	for _, torn := range tears {
		for cut := 1; cut < steps+60; cut++ {
			f := newFlash()
			f.cutAt, f.torn = cut, torn
			var sec proto.Secure
			m := NewMarks(Where{Flash: f, First: 20, Pages: 2})
			store := m.Load(&sec)
			var acked, tried uint32
			for i := uint32(1); i <= steps && !f.dead(); i++ {
				sec.RestoreMark(1, i)
				tried = i
				if store.Mark(1, i) {
					acked = i
				}
				if i%3 == 0 {
					m.Prepare() // one page now and then, as the main loop does
				}
			}
			f.reboot()
			var again proto.Secure
			NewMarks(Where{Flash: f, First: 20, Pages: 2}).Load(&again)
			last, ok := again.Window(1).Last()
			if acked > 0 && (!ok || last < acked || last > tried) {
				t.Fatalf("torn %d, cut %d: restored %d (%v), stored %d, tried %d", torn, cut, last, ok, acked, tried)
			}
		}
	}
}
