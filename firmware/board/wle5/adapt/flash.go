package adapt

import (
	"cairnmesh/firmware/journal"
	"cairnmesh/firmware/keystore"
)

// Flash layout of the STM32WLE5CC: 128 pages of 2 KB (RM0461 3.3), written in
// double words (64 bits plus 8 bits of ECC), about 10 000 erases per page.
// The pages are the U083's (board/journal.go, board/keys.go), so the image
// limit of the Makefile (FLASH_LIMIT) holds for both chips. The
// image must end below RecordsPage.
const (
	PageSize = journal.PageSize
	Pages    = 128

	RecordsPage  = 101 // 101-116: the device's duplicate records, two banks of 8 pages
	RecordsBank  = 8
	MarksPage    = 117 // 117-124: the controller's replay mark, two banks of 4 pages
	MarksBank    = 4
	KeyPage      = 125 // the key record (keystore)
	CounterPageA = 126 // the FCNT reservation, two alternating pages
	CounterPageB = 127

	// ImageLimit is where the firmware image must end: the first journal page.
	ImageLimit = RecordsPage * PageSize
)

// Verified is a journal.Flash that reads every double word back after
// writing it (a write that did not take, or that the chip reported as done but
// reads differently, is a failure). Raw is the chip.
type Verified struct {
	Raw journal.Flash
}

func (v Verified) Erase(page int) bool { return v.Raw.Erase(page) }

func (v Verified) Program(page, offset int, dw *[8]byte) bool {
	if !v.Raw.Program(page, offset, dw) {
		return false
	}
	var back [8]byte
	return v.Raw.Read(page, offset, &back) && back == *dw
}

func (v Verified) Read(page, offset int, dw *[8]byte) bool { return v.Raw.Read(page, offset, dw) }

// Counter is a 32-bit value that survives resets (the FCNT bound, spec 5). It
// appends 8-byte records to one of two pages. When a page is full it writes
// the next record to the other page first and erases the full page after that,
// so a power loss at any point leaves at least one valid record. It is
// hw/flashhw.Counter (same record format) over a journal.Flash, so a power cut
// can be tested on the host; and a double word that fails its ECC (a write cut
// by power loss) is skipped, not mistaken for a free slot or a record.
//
// Record: word 0 = 0x5254 << 16 | ^value&0xFFFF (a check), word 1 = value,
// little endian.
type Counter struct {
	F            journal.Flash
	PageA, PageB int

	page int // page with the newest record
	next int // byte offset of the next free record in page; -1 before the first record
	val  uint32
}

const recordTag = 0x5254

func decodeRecord(dw *[8]byte) (v uint32, ok bool) {
	w0 := uint32(dw[0]) | uint32(dw[1])<<8 | uint32(dw[2])<<16 | uint32(dw[3])<<24
	w1 := uint32(dw[4]) | uint32(dw[5])<<8 | uint32(dw[6])<<16 | uint32(dw[7])<<24
	return w1, w0>>16 == recordTag && uint16(w0) == ^uint16(w1)
}

func blank(dw *[8]byte) bool { return *dw == [8]byte{0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF} }

// scan returns the newest value in a page, the offset of its first free slot
// (PageSize when full) and whether it holds a record at all.
func (c *Counter) scan(page int) (val uint32, free int, ok bool) {
	var dw [8]byte
	for off := 0; off < PageSize; off += 8 {
		if !c.F.Read(page, off, &dw) {
			continue // torn by a power loss: neither a record nor a free slot
		}
		if blank(&dw) {
			return val, off, ok
		}
		if v, good := decodeRecord(&dw); good {
			val, ok = v, true
		}
	}
	return val, PageSize, ok
}

// pageBlank: every slot of the page reads as erased. Not only the first: an
// erase cut by a power loss can leave the first slots erased and later ones
// still holding old records (or torn); a page taken for empty by its first
// slot would be written over them (PROGERR at the first leftover, and every
// reservation fails from then on). 256 reads at a page switch, no extra erase
// (an unconditional erase would double the wear of the two pages).
func (c *Counter) pageBlank(page int) bool {
	var dw [8]byte
	for off := 0; off < PageSize; off += 8 {
		if !c.F.Read(page, off, &dw) || !blank(&dw) {
			return false
		}
	}
	return true
}

// Load finds the newest value. It returns 0 when both pages are empty.
func (c *Counter) Load() uint32 {
	va, fa, oka := c.scan(c.PageA)
	vb, fb, okb := c.scan(c.PageB)
	switch {
	case oka && (!okb || va >= vb):
		c.page, c.val, c.next = c.PageA, va, fa
	case okb:
		c.page, c.val, c.next = c.PageB, vb, fb
	default:
		c.page, c.val, c.next = c.PageA, 0, -1
	}
	return c.val
}

// Value returns the last loaded or stored value.
func (c *Counter) Value() uint32 { return c.val }

// Store appends v; false when the flash refused. Values must not decrease.
// Call Load once before.
func (c *Counter) Store(v uint32) bool {
	if c.next >= 0 && c.next < PageSize {
		return c.write(v)
	}
	// No record yet, or the page is full: continue on the other page.
	old := c.page
	other := c.PageB
	if old == c.PageB {
		other = c.PageA
	}
	if !c.pageBlank(other) && !c.F.Erase(other) {
		return false
	}
	first, prevNext := c.next < 0, c.next
	c.page, c.next = other, 0
	if !c.write(v) {
		// Back to the page that holds the records, due to switch again: the
		// next Store erases the page that failed, never the valid one.
		c.page, c.next = old, prevNext
		return false
	}
	if !first || !c.pageBlank(old) {
		return c.F.Erase(old)
	}
	return true
}

func (c *Counter) write(v uint32) bool {
	w0 := uint32(recordTag)<<16 | uint32(^uint16(v))
	rec := [8]byte{byte(w0), byte(w0 >> 8), byte(w0 >> 16), byte(w0 >> 24), byte(v), byte(v >> 8), byte(v >> 16), byte(v >> 24)}
	if !c.F.Program(c.page, c.next, &rec) {
		return false
	}
	c.next += 8
	c.val = v
	return true
}

// MaxRaise is the highest bound "fcnt raise" accepts.
const MaxRaise = 0xF0000000

// CounterStore is proto.CounterStore on the two counter pages: the FCNT
// reservation (spec 5). A page erase stalls the CPU about 22 ms; it happens
// once per 256 reservations.
type CounterStore struct {
	c Counter
	// Errors counts failed or unverified writes.
	Errors uint32
}

// NewCounterStore loads the stored bound: the counter restarts there (spec 5:
// a counter never repeats under one key).
func NewCounterStore(f journal.Flash, pageA, pageB int) (*CounterStore, uint32) {
	s := &CounterStore{c: Counter{F: f, PageA: pageA, PageB: pageB}}
	return s, s.c.Load()
}

// ReserveTx stores bound and reads it back from the flash; a counter is used
// only after this returned true. A bound below the stored one is refused
// (the stored bound never goes down: a lower one would let a counter repeat).
func (s *CounterStore) ReserveTx(bound uint32) bool {
	if bound < s.c.Value() {
		s.Errors++
		return false
	}
	if !s.c.Store(bound) {
		s.Errors++
		return false
	}
	check := Counter{F: s.c.F, PageA: s.c.PageA, PageB: s.c.PageB}
	if check.Load() != bound {
		s.Errors++
		return false
	}
	return true
}

// Bound is the stored FCNT bound: counters up to it may already have been
// used, the node restarts above it.
func (s *CounterStore) Bound() uint32 { return s.c.Value() }

// Raise stores a higher bound (re-provisioning after the counter pages were
// erased: above the highest FCNT the controller saw, spec 5). Only upward: a
// lower bound would let the node reuse counters. Reboot right after: the
// running protocol keeps its own reservation. Refused above MaxRaise: the
// protocol seals no frame at counter 0xFFFFFFFF (proto.SealFrame), and each
// boot reserves another 256 on top, so a bound near the top would end the
// node's transmissions for good (until the pages are erased and re-provisioned
// with a new network key). MaxRaise leaves 2^28 counters of headroom; the
// controller's own counters start at the Unix time (about 1.8e9).
func (s *CounterStore) Raise(bound uint32) bool {
	if bound <= s.c.Value() || bound > MaxRaise {
		return false
	}
	return s.ReserveTx(bound)
}

// KeyStore is the key record in a page of its own (keystore). An RDP
// regression erases it with the rest of the flash.
type KeyStore struct {
	F    journal.Flash
	Page int
}

// Load reads the key record; false when none is stored, it is damaged or any
// of its double words fails its ECC.
func (k KeyStore) Load(r *keystore.Record) bool {
	var b [keystore.RecordSize]byte
	var dw [8]byte
	for off := 0; off < keystore.RecordSize; off += 8 {
		if !k.F.Read(k.Page, off, &dw) {
			return false
		}
		copy(b[off:], dw[:])
	}
	return r.Decode(b[:])
}

// Store erases the page, writes the record and reads it back. The erase
// stalls the CPU about 22 ms. A failure may leave the page erased (no keys).
func (k KeyStore) Store(r *keystore.Record) bool {
	var b [keystore.RecordSize]byte
	r.Encode(&b)
	if !k.F.Erase(k.Page) {
		return false
	}
	var dw [8]byte
	for off := 0; off < keystore.RecordSize; off += 8 {
		copy(dw[:], b[off:off+8])
		if !k.F.Program(k.Page, off, &dw) {
			return false
		}
	}
	var check keystore.Record
	return k.Load(&check) && check == *r
}

// Erase removes the key record.
func (k KeyStore) Erase() bool { return k.F.Erase(k.Page) }
