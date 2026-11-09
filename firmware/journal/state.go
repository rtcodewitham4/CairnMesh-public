package journal

import "cairnmesh/firmware/proto"

// Journal kinds (the entry tag).
const (
	KindRecords = 'R'
	KindMarks   = 'M'
)

// Where is a journal's place in flash: the first page of bank 0 and the
// pages per bank (bank 1 follows bank 0).
type Where struct {
	Flash        Flash
	First, Pages int
}

const (
	flagOpen  = 1 << 0
	flagTouch = 1 << 1
)

// Records keeps a device's duplicate records (spec 4.1). Entry: sender, ID
// (little endian), result, flags (open; touch: only the sender order).
// Compaction writes the device's records as EachRecord lists them.
type Records struct {
	J     Journal
	where Where
	dev   *proto.Device
	e     Entry
}

// NewRecords places the journal (Load reads it).
func NewRecords(w Where) *Records { return &Records{where: w} }

// Load replays the journal into d (after d.Init; then d.Reset) and returns
// it as d's log.
func (r *Records) Load(d *proto.Device) proto.RecordLog {
	r.dev = d
	r.J.Open(r.where.Flash, r.where.First, r.where.Pages, KindRecords, r)
	return r
}

// Replay is the journal's Sink.
func (r *Records) Replay(e *Entry) {
	if e[4]&flagTouch != 0 {
		r.dev.RestoreTouch(e[0])
		return
	}
	r.dev.Restore(e[0], uint16(e[1])|uint16(e[2])<<8, e[3], e[4]&flagOpen != 0)
}

// Write is proto.RecordLog.
func (r *Records) Write(sender byte, id uint16, result byte, open bool) bool {
	record(&r.e, sender, id, result, open)
	return r.J.Append(&r.e, r)
}

// Touch is proto.RecordLog.
func (r *Records) Touch(sender byte) bool {
	r.e = Entry{sender, 0, 0, 0, flagTouch}
	return r.J.Append(&r.e, r)
}

// Live is the journal's Source.
func (r *Records) Live(*Journal) bool { return r.dev.EachRecord(r) }

// Record is proto.RecordVisitor (Live's).
func (r *Records) Record(sender byte, id uint16, result byte, open bool) bool {
	var e Entry
	record(&e, sender, id, result, open)
	return r.J.Keep(&e)
}

func record(e *Entry, sender byte, id uint16, result byte, open bool) {
	flags := byte(0)
	if open {
		flags = flagOpen
	}
	*e = Entry{sender, byte(id), byte(id >> 8), result, flags}
}

// Marks keeps the replay marks of a node's security layer (spec 5). Entry:
// sender, highest counter accepted (little endian); a later entry of the
// same sender is higher. Compaction writes every sender's mark.
type Marks struct {
	J     Journal
	where Where
	sec   *proto.Secure
	e     Entry
}

// NewMarks places the journal (Load reads it).
func NewMarks(w Where) *Marks { return &Marks{where: w} }

// Load replays the journal into s and returns it as s's mark store.
func (m *Marks) Load(s *proto.Secure) proto.MarkStore {
	m.sec = s
	m.J.Open(m.where.Flash, m.where.First, m.where.Pages, KindMarks, m)
	return m
}

// Replay is the journal's Sink.
func (m *Marks) Replay(e *Entry) {
	m.sec.RestoreMark(e[0], uint32(e[1])|uint32(e[2])<<8|uint32(e[3])<<16|uint32(e[4])<<24)
}

// Mark is proto.MarkStore.
func (m *Marks) Mark(sender byte, last uint32) bool {
	mark(&m.e, sender, last)
	return m.J.Append(&m.e, m)
}

// Live is the journal's Source.
func (m *Marks) Live(*Journal) bool { return m.sec.EachMark(m) }

// Visit is proto.MarkVisitor (Live's).
func (m *Marks) Visit(sender byte, last uint32) bool {
	var e Entry
	mark(&e, sender, last)
	return m.J.Keep(&e)
}

func mark(e *Entry, sender byte, last uint32) {
	*e = Entry{sender, byte(last), byte(last >> 8), byte(last >> 16), byte(last >> 24)}
}

// Prepare erases one page ahead of the next compaction (Journal.Prepare).
func (r *Records) Prepare() bool { return r.J.Prepare() }

// Unprepared reports whether Prepare has a page to erase.
func (r *Records) Unprepared() bool { return r.J.Unprepared() }

// Prepare erases one page ahead of the next compaction (Journal.Prepare).
func (m *Marks) Prepare() bool { return m.J.Prepare() }

// Unprepared reports whether Prepare has a page to erase.
func (m *Marks) Unprepared() bool { return m.J.Unprepared() }
