// Package journal keeps small state through a power cut in flash: an
// append-only log of 8-byte entries in two banks of pages. When the bank
// in use is full, the live state is written into the other bank and the
// log goes on there. Pure Go (host tests run it on a fake flash); the board
// gives it the real flash (board.Flash).
//
// Entry (one double word): tag, 5 bytes of payload, CRC-16 of the first 6
// (link.CRC16, little endian). Slot 0 of a bank is its header: tag 'H',
// the journal's kind, a 32-bit generation. A bank is in use only once its
// header is written, and that is written last: a power cut while a bank is
// filled leaves the older one in use. Entries are replayed in the order
// written; a torn one (bad CRC or ECC) is skipped, the first erased slot
// ends the log.
package journal

import "cairnmesh/firmware/link"

// Flash is the storage under a journal: pages of PageSize bytes, written
// one double word at a time over erased bytes (0xFF).
type Flash interface {
	Erase(page int) bool
	// Program writes one double word at a byte offset of a page and
	// reports whether it reads back as written.
	Program(page, offset int, dw *[8]byte) bool
	// Read reads one double word; false when it is unreadable (its ECC
	// failed: a write cut by a power loss).
	Read(page, offset int, dw *[8]byte) bool
}

// PageSize is the flash page (STM32U083: 2 KB).
const PageSize = 2048

const (
	slotsPerPage = PageSize / 8
	tagHeader    = 'H'
)

// Entry is one entry's payload.
type Entry [5]byte

// Sink takes the entries Open replays.
type Sink interface {
	Replay(e *Entry)
}

// Source writes the live state into a fresh bank: it calls Keep for every
// live entry and reports whether all were kept.
type Source interface {
	Live(j *Journal) bool
}

// Stats counts what the journal did.
type Stats struct {
	// Appended entries; Compactions: switches to the other bank; Skipped:
	// slots that would not program (passed over); Failed: Appends that
	// stored nothing.
	Appended, Compactions, Skipped, Failed uint32
}

// Journal is one log. The zero value is unusable: Open first.
type Journal struct {
	flash  Flash
	first  int // first page of bank 0; bank 1 follows it
	pages  int // pages per bank
	kind   byte
	active int // bank in use; -1: none yet
	gen    uint32
	next   int // next free slot in the active bank
	fill   int // next slot of the bank being compacted
	into   int // the bank being compacted
	// ready pages of the other bank are erased (this boot): Prepare erases
	// them ahead, so a compaction does not stall the node for the whole bank.
	ready   int
	prepErr bool // an erase failed: Prepare waits for the next compaction
	Stats   Stats
}

// Open reads the newest bank of the journal at pages first ..
// first+2*pagesPerBank-1 and replays its entries into s in order.
func (j *Journal) Open(f Flash, first, pagesPerBank int, kind byte, s Sink) {
	*j = Journal{flash: f, first: first, pages: pagesPerBank, kind: kind, active: -1}
	var dw [8]byte
	for b := 0; b < 2; b++ {
		if valid, _ := j.read(b, 0, &dw); !valid || dw[0] != tagHeader || dw[1] != kind {
			continue
		}
		gen := uint32(dw[2]) | uint32(dw[3])<<8 | uint32(dw[4])<<16 | uint32(dw[5])<<24
		if j.active < 0 || gen > j.gen {
			j.active, j.gen = b, gen
		}
	}
	if j.active < 0 {
		return
	}
	j.next = j.slots()
	var e Entry
	for i := 1; i < j.slots(); i++ {
		valid, readable := j.read(j.active, i, &dw)
		if readable && erased(&dw) {
			j.next = i
			return
		}
		if valid && dw[0] == kind {
			copy(e[:], dw[1:6])
			s.Replay(&e)
		}
	}
}

// Append stores e. When the bank is full, src writes the live state into
// the other bank first, then e follows there. false: nothing stored.
func (j *Journal) Append(e *Entry, src Source) bool {
	if j.active >= 0 {
		for j.next < j.slots() {
			slot := j.next
			j.next++
			if j.write(j.active, slot, j.kind, e[:]) {
				j.Stats.Appended++
				return true
			}
			j.Stats.Skipped++
		}
	}
	if j.compact(e, src) {
		j.Stats.Appended++
		return true
	}
	j.Stats.Failed++
	return false
}

// Keep writes one live entry into the bank being compacted (call it only
// from Source.Live); false when it is full or the write failed.
func (j *Journal) Keep(e *Entry) bool {
	if j.fill >= j.slots() || !j.write(j.into, j.fill, j.kind, e[:]) {
		return false
	}
	j.fill++
	return true
}

// Prepare erases one page of the bank the next compaction fills (a page
// takes about 22 ms, the CPU stalled: call it while nothing is received).
// The other bank is never the one in use, so a power cut at any point
// loses nothing. false: nothing left to erase.
func (j *Journal) Prepare() bool {
	if !j.Unprepared() {
		return false
	}
	if !j.flash.Erase(j.first + (1-j.active)*j.pages + j.ready) {
		j.prepErr = true
		return false
	}
	j.ready++
	return true
}

// Unprepared reports whether Prepare has a page to erase.
func (j *Journal) Unprepared() bool { return j.active >= 0 && j.ready < j.pages && !j.prepErr }

// Free is how many entries fit before the next compaction.
func (j *Journal) Free() int {
	if j.active < 0 {
		return 0
	}
	return j.slots() - j.next
}

func (j *Journal) compact(e *Entry, src Source) bool {
	b := 0
	if j.active == 0 {
		b = 1
	}
	if j.active < 0 {
		j.ready = 0 // nothing prepared: no bank in use yet
	}
	for ; j.ready < j.pages; j.ready++ {
		if !j.flash.Erase(j.first + b*j.pages + j.ready) {
			return false
		}
	}
	j.into, j.fill = b, 1
	gen := j.gen + 1
	header := [5]byte{j.kind, byte(gen), byte(gen >> 8), byte(gen >> 16), byte(gen >> 24)}
	if src != nil && !src.Live(j) || !j.Keep(e) || !j.write(b, 0, tagHeader, header[:]) {
		j.ready = 0 // written into: the next try erases the bank again
		return false
	}
	j.active, j.gen, j.next = b, gen, j.fill
	j.ready, j.prepErr = 0, false // the old bank is the next one to erase
	j.Stats.Compactions++
	return true
}

func (j *Journal) slots() int { return j.pages * slotsPerPage }

func (j *Journal) at(bank, slot int) (page, offset int) {
	return j.first + bank*j.pages + slot/slotsPerPage, slot % slotsPerPage * 8
}

func (j *Journal) write(bank, slot int, tag byte, payload []byte) bool {
	var dw [8]byte
	dw[0] = tag
	copy(dw[1:6], payload)
	crc := link.CRC16(dw[:6])
	dw[6], dw[7] = byte(crc), byte(crc>>8)
	page, offset := j.at(bank, slot)
	return j.flash.Program(page, offset, &dw)
}

// read reads a slot: valid when it is readable and its CRC holds.
func (j *Journal) read(bank, slot int, dw *[8]byte) (valid, readable bool) {
	page, offset := j.at(bank, slot)
	if !j.flash.Read(page, offset, dw) {
		return false, false
	}
	return link.CRC16(dw[:6]) == uint16(dw[6])|uint16(dw[7])<<8, true
}

func erased(dw *[8]byte) bool {
	for _, b := range dw {
		if b != 0xFF {
			return false
		}
	}
	return true
}
