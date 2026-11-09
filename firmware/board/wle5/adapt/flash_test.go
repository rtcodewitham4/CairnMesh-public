package adapt

import (
	"os"
	"regexp"
	"strconv"
	"testing"

	"cairnmesh/firmware/keystore"
)

// fakeFlash behaves like the STM32WLE5's: a double word is written only over
// erased bytes (else PROGERR), an erase takes a whole page, and a power cut at
// operation cutAt (0: never) makes that operation and every later one fail,
// the cut one left as torn says. A torn double word may fail its ECC.
type fakeFlash struct {
	pages      map[int]*[PageSize]byte
	bad        map[[2]int]bool // double words whose ECC fails
	ops, cutAt int
	torn       tear
	erases     int
	// lie: Program reports success but writes nothing (a chip that lost it).
	lie bool
	// refuse: Program reports failure at once (write protection).
	refuse bool
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
	if p < 0 || p >= Pages {
		panic("page out of range: " + strconv.Itoa(p))
	}
	if f.pages[p] == nil {
		f.pages[p] = new([PageSize]byte)
		for i := range f.pages[p] {
			f.pages[p][i] = 0xFF
		}
	}
	return f.pages[p]
}

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
	if offset%8 != 0 || offset < 0 || offset+8 > PageSize {
		panic("misaligned program")
	}
	pg := f.page(p)
	if f.refuse {
		return false
	}
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
	if f.lie {
		return true
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

func (f *fakeFlash) reboot() { f.ops, f.cutAt = 0, 0 }

// ---- layout ----

func TestLayoutFitsAndDoesNotOverlap(t *testing.T) {
	type span struct {
		name        string
		first, last int
	}
	spans := []span{
		{"records", RecordsPage, RecordsPage + 2*RecordsBank - 1},
		{"marks", MarksPage, MarksPage + 2*MarksBank - 1},
		{"keys", KeyPage, KeyPage},
		{"counter A", CounterPageA, CounterPageA},
		{"counter B", CounterPageB, CounterPageB},
	}
	used := map[int]string{}
	for _, s := range spans {
		if s.first < 0 || s.last >= Pages {
			t.Errorf("%s pages %d-%d outside the flash (%d pages)", s.name, s.first, s.last, Pages)
		}
		for p := s.first; p <= s.last; p++ {
			if other, dup := used[p]; dup {
				t.Errorf("page %d is both %s and %s", p, other, s.name)
			}
			used[p] = s.name
		}
	}
	// The journals, the key page and the counter pages are one block up to
	// the last page, and nothing below RecordsPage is taken.
	for p := RecordsPage; p < Pages; p++ {
		if used[p] == "" {
			t.Errorf("page %d is unused inside the data block", p)
		}
	}
	if PageSize != 2048 {
		t.Errorf("the WLE5CC's flash page is 2 KB, got %d", PageSize)
	}
}

// The Makefile's FLASH_LIMIT (the image check for the U083) is the same address:
// the WLE5 node shares the pages, so the same numbers hold for both chips.
func TestImageLimitMatchesMakefile(t *testing.T) {
	b, err := os.ReadFile("../../../Makefile")
	if err != nil {
		t.Skip("Makefile not found:", err)
	}
	m := regexp.MustCompile(`(?m)^FLASH_LIMIT\s*\?=\s*(0x[0-9A-Fa-f]+)`).FindSubmatch(b)
	if m == nil {
		t.Fatal("FLASH_LIMIT not found in the Makefile")
	}
	v, err := strconv.ParseUint(string(m[1]), 0, 32)
	if err != nil {
		t.Fatal(err)
	}
	if v != ImageLimit {
		t.Errorf("Makefile FLASH_LIMIT = %#x, ImageLimit = %#x", v, ImageLimit)
	}
}

// ---- Verified ----

func TestVerifiedCatchesAWriteThatDidNotTake(t *testing.T) {
	f := newFlash()
	v := Verified{Raw: f}
	dw := [8]byte{1, 2, 3, 4, 5, 6, 7, 8}
	if !v.Program(3, 16, &dw) {
		t.Fatal("a good program failed")
	}
	var back [8]byte
	if !v.Read(3, 16, &back) || back != dw {
		t.Fatalf("read back %v", back)
	}
	f.lie = true
	if v.Program(3, 24, &dw) {
		t.Error("a program that wrote nothing was taken for good")
	}
	f.lie = false
	if v.Program(3, 16, &dw) {
		t.Error("a program over a written double word (PROGERR) was taken for good")
	}
	f.refuse = true
	if v.Program(4, 0, &dw) {
		t.Error("a refused program was taken for good")
	}
}

// ---- counter ----

func newCounter(f *fakeFlash) (*CounterStore, uint32) {
	return NewCounterStore(Verified{Raw: f}, CounterPageA, CounterPageB)
}

func TestCounterStartsAtZeroAndKeepsReservations(t *testing.T) {
	f := newFlash()
	s, last := newCounter(f)
	if last != 0 {
		t.Fatalf("blank flash: bound %d", last)
	}
	for bound := uint32(256); bound <= 256*600; bound += 256 { // crosses several page switches
		if !s.ReserveTx(bound) {
			t.Fatalf("ReserveTx(%d) failed", bound)
		}
	}
	s2, last := newCounter(f)
	if last != 256*600 || s2.Bound() != last {
		t.Fatalf("after a restart the bound is %d, want %d", last, 256*600)
	}
	// 600 reservations over two 256-record pages: erased pages, never the
	// valid one being written.
	if f.erases < 2 || f.erases > 4 {
		t.Errorf("%d erases for 600 reservations", f.erases)
	}
}

func TestCounterRaiseOnlyUpward(t *testing.T) {
	f := newFlash()
	s, _ := newCounter(f)
	if !s.Raise(1000) {
		t.Fatal("Raise on a blank counter failed")
	}
	if s.Raise(1000) || s.Raise(999) {
		t.Error("Raise accepted a bound that is not higher")
	}
	if s.Bound() != 1000 {
		t.Errorf("bound %d", s.Bound())
	}
	_, last := newCounter(f)
	if last != 1000 {
		t.Errorf("restart: %d", last)
	}
}

func TestCounterFailedWriteIsNotAReservation(t *testing.T) {
	f := newFlash()
	s, _ := newCounter(f)
	if !s.ReserveTx(256) {
		t.Fatal("first reservation failed")
	}
	f.refuse = true
	if s.ReserveTx(512) || s.Errors == 0 {
		t.Error("a refused write was reported as stored")
	}
	f.refuse = false
	f.lie = true
	if s.ReserveTx(512) {
		t.Error("a write that did not take was reported as stored")
	}
	f.lie = false
	_, last := newCounter(f)
	if last != 256 {
		t.Errorf("restart reads %d, want 256", last)
	}
}

// Power is cut at every flash operation of a run of reservations (clean, half
// written, ECC-failed), across the page switches. After the cut the stored
// bound is never below the last reservation that returned true, never above
// the one in flight, and the counter works on afterwards.
func TestCounterPowerCutAtEveryOperation(t *testing.T) {
	const reservations = 600
	total := 0
	{
		f := newFlash()
		s, _ := newCounter(f)
		for i := 1; i <= reservations; i++ {
			s.ReserveTx(uint32(i) * 256)
		}
		total = f.ops
	}
	for _, torn := range tears {
		for cut := 1; cut <= total; cut++ {
			f := newFlash()
			f.cutAt, f.torn = cut, torn
			s, _ := newCounter(f)
			var acked, inFlight uint32
			for i := 1; i <= reservations; i++ {
				inFlight = uint32(i) * 256
				if !s.ReserveTx(inFlight) {
					break
				}
				acked = inFlight
			}
			f.reboot()
			s2, last := newCounter(f)
			if last < acked || last > inFlight {
				t.Fatalf("torn %d cut %d: bound %d after reboot, acked %d, in flight %d", torn, cut, last, acked, inFlight)
			}
			// It must still be usable through at least two more page switches
			// (a page left half erased by the cut is reused on the way): every
			// reservation succeeds and is read back.
			next := inFlight
			for k := 0; k < 520; k++ {
				next += 256
				if !s2.ReserveTx(next) {
					t.Fatalf("torn %d cut %d: reservation %d after the cut failed", torn, cut, k+1)
				}
			}
			if _, now := newCounter(f); now != next {
				t.Fatalf("torn %d cut %d: stored %d, want %d", torn, cut, now, next)
			}
		}
	}
}

// ---- key page ----

func TestKeyStoreRoundTrip(t *testing.T) {
	f := newFlash()
	k := KeyStore{F: Verified{Raw: f}, Page: KeyPage}
	var r keystore.Record
	if k.Load(&r) {
		t.Fatal("blank page loads as keys")
	}
	want := keystore.Record{Node: 7, AppKeyVersion: 2}
	for i := range want.Network {
		want.Network[i], want.App[i] = byte(i+1), byte(0xA0+i)
	}
	if !k.Store(&want) {
		t.Fatal("Store failed")
	}
	if !k.Load(&r) || r != want {
		t.Fatalf("loaded %+v", r)
	}
	// A second provisioning replaces the first.
	want.Node = 9
	if !k.Store(&want) || !k.Load(&r) || r.Node != 9 {
		t.Fatalf("second Store: %+v", r)
	}
	if !k.Erase() || k.Load(&r) {
		t.Error("Erase left keys behind")
	}
	// Nothing but the key page was touched.
	for p := range f.pages {
		if p != KeyPage {
			var dw [8]byte
			for off := 0; off < PageSize; off += 8 {
				f.Read(p, off, &dw)
				if !blank(&dw) {
					t.Fatalf("page %d written", p)
				}
			}
		}
	}
}

func TestKeyStoreRefusesDamageAndFailures(t *testing.T) {
	f := newFlash()
	k := KeyStore{F: Verified{Raw: f}, Page: KeyPage}
	rec := keystore.Record{Node: 3}
	f.lie = true
	if k.Store(&rec) {
		t.Error("Store reported success for a write that did not take")
	}
	f.lie = false
	f.refuse = true
	if k.Store(&rec) {
		t.Error("Store reported success for a refused write")
	}
	f.refuse = false
	if !k.Store(&rec) {
		t.Fatal("Store failed")
	}
	// A double word of the record failing its ECC: no keys, not garbage keys.
	f.bad[[2]int{KeyPage, 16}] = true
	var r keystore.Record
	if k.Load(&r) {
		t.Error("a record with an ECC failure loaded")
	}
	// A power cut during the store leaves the old keys (cut before the erase), NO
	// keys (the erase is done, the record not complete: the old keys cannot
	// survive it, Store is erase-then-write) or the new ones; the
	// record's CRC refuses a mixture.
	for _, torn := range tears {
		for cut := 1; cut <= 10; cut++ {
			g := newFlash()
			g.torn = torn
			kk := KeyStore{F: Verified{Raw: g}, Page: KeyPage}
			kk.Store(&keystore.Record{Node: 5})
			g.ops, g.cutAt = 0, cut
			kk.Store(&keystore.Record{Node: 6})
			g.reboot()
			var got keystore.Record
			if kk.Load(&got) && got.Node != 5 && got.Node != 6 {
				t.Fatalf("torn %d cut %d: keys of node %d appeared", torn, cut, got.Node)
			}
		}
	}
}

func TestRaiseHasAFloorAndACeiling(t *testing.T) {
	f := newFlash()
	s, _ := newCounter(f)
	if !s.Raise(MaxRaise) {
		t.Fatal("Raise to the ceiling refused")
	}
	if s.Raise(MaxRaise+1) || s.Raise(0xFFFFFFFF) {
		t.Error("Raise accepted a bound that would end the node's transmissions")
	}
	g := newFlash()
	s2, _ := newCounter(g)
	if s2.Raise(0xFFFFFFFF) {
		t.Error("Raise(0xFFFFFFFF) accepted on a blank counter")
	}
	if _, last := newCounter(g); last != 0 {
		t.Errorf("a refused Raise changed the stored bound to %d", last)
	}
}

func TestReserveTxNeverLowersTheBound(t *testing.T) {
	f := newFlash()
	s, _ := newCounter(f)
	if !s.ReserveTx(5000) {
		t.Fatal("reserve")
	}
	if s.ReserveTx(4000) {
		t.Error("a lower bound was stored")
	}
	if _, last := newCounter(f); last != 5000 {
		t.Errorf("stored %d", last)
	}
	if !s.ReserveTx(5000) {
		t.Error("the same bound again must still be accepted")
	}
}
