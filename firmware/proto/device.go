package proto

// Device is the device endpoint (spec 4.1; cairnmesh/device.py, the
// reference, checked with tests/vectors/lora_device_behaviour.json). It
// sees plain frames above the security layer (and above the route endpoint
// in a mesh) and executes each request at most once.
//
// Tables are fixed arrays sized by the Max* constants; DeviceConfig picks
// limits at or below them. The duplicate records survive Reset (in RAM),
// and a power cut through a RecordLog (spec 4.1: written before the request
// runs or is answered; journal.Records on the board).

// Table sizes (spec section 9).
const (
	MaxDedupWindow = 8
	MaxSenders     = 4
	MaxOpen        = 4
	MaxDeferred    = 8
	maxRecords     = MaxDedupWindow + MaxOpen + 1 // + 1: the newest before the trim
	maxPending     = MaxSenders * MaxOpen
)

// DeviceConfig holds the parameters of spec section 18 (Device rows).
type DeviceConfig struct {
	ID, Controller byte
	// HeartbeatInterval between single-hop HEARTBEATs (±15 %); Never: off
	// (a mesh uses HELLO instead).
	HeartbeatInterval Time
	// SleepDelayMin..Max: a frame received in SLEEP is handled this much later.
	SleepDelayMin, SleepDelayMax Time
	Awake                        bool
	DedupWindow                  int
	// DedupTTL is the records' safety bound, counted from boot; Never: kept until pushed out.
	DedupTTL      Time
	MaxSenders    int
	MaxDeferred   int
	AnswerBudget  Time
	ResultTimeout Time
	ResultRetries int
	MaxOpen       int
}

// DefaultDeviceConfig is device.py's defaults (spec section 18).
func DefaultDeviceConfig(id byte) DeviceConfig {
	return DeviceConfig{
		ID:                id,
		Controller:        DefaultControllerID,
		HeartbeatInterval: 3 * Second,
		SleepDelayMin:     1 * Second,
		SleepDelayMax:     3 * Second,
		DedupWindow:       4,
		DedupTTL:          7 * 24 * 3600 * Second,
		MaxSenders:        2,
		MaxDeferred:       8,
		AnswerBudget:      600 * Millisecond,
		ResultTimeout:     10 * Second,
		ResultRetries:     4,
		MaxOpen:           4,
	}
}

// ResultPending is what Perform returns when the action's result is known
// only later: the application reports it with Device.Complete (spec 4.1,
// result known later; device.PENDING). Internal, never on air.
const ResultPending = 0xFF

// Request is the REQUEST an action runs for: Sender and ID identify it to
// Device.Complete.
type Request struct {
	Sender        byte
	ID            uint16
	Action, Value byte
}

// Application runs the actions of REQUESTs.
type Application interface {
	// Duration is how long the action takes (0: instant). Within
	// AnswerBudget the answer waits for it; longer ones answer ACCEPTED and
	// report a RESULT. For a ResultPending action it is the deadline.
	Duration(action, value byte) Time
	// Perform starts the action and returns its result (ResultOK,
	// ResultError), which counts once Duration is over; or ResultPending,
	// and Device.Complete gives the result before the deadline.
	Perform(r Request) byte
}

// RecordLog keeps the duplicate records through a power cut (spec 4.1).
// Write stores one record's state (a record's newest write wins), Touch that
// a sender became the one heard from last; both report whether it is
// stored. A request whose record could not be stored is neither run nor
// answered: the controller's retry comes back to it.
type RecordLog interface {
	Write(sender byte, id uint16, result byte, open bool) bool
	Touch(sender byte) bool
}

// RecordVisitor takes the records EachRecord lists; false stops it.
type RecordVisitor interface {
	Record(sender byte, id uint16, result byte, open bool) bool
}

// FrameSender takes a plain frame for the layer below (the buffer is only
// valid during the call).
type FrameSender interface {
	Send(plain []byte)
}

// DeviceStats counts what the device did.
type DeviceStats struct {
	Accepted, ResultsSent, ResultsAcked, ResultsAbandoned, RefusedBusy uint32
	DeferredDropped, Ignored                                           uint32
	// DeadlineMissed: pending actions not completed by their deadline
	// (ERROR); LateCompletions: Complete calls that changed nothing.
	DeadlineMissed, LateCompletions uint32
	// LogFailed: writes the RecordLog did not store.
	LogFailed uint32
}

type record struct {
	logged Time
	id     uint16
	result byte
	open   bool
}

// senderRecords is one sender's records, oldest first.
type senderRecords struct {
	recs   [maxRecords]record
	n      int
	sender byte
}

type key struct {
	sender byte
	id     uint16
}

// pending is an action running, an answer waiting for a short action, or a
// RESULT to send; the tables keep insertion order (device.py's dicts).
type pending struct {
	at     Time // done at (actions, answers) or next send (results)
	key    key
	result byte
	sent   int
	frame  [7]byte // the REQUEST an answer waits on
}

type pendingTable struct {
	items [maxPending]pending
	n     int
}

func (t *pendingTable) find(k key) int {
	for i := 0; i < t.n; i++ {
		if t.items[i].key == k {
			return i
		}
	}
	return -1
}

// set updates an entry in place or appends it.
func (t *pendingTable) set(p pending) {
	if i := t.find(p.key); i >= 0 {
		t.items[i] = p
		return
	}
	if t.n < len(t.items) {
		t.items[t.n] = p
		t.n++
	}
}

func (t *pendingTable) remove(i int) {
	copy(t.items[i:], t.items[i+1:t.n])
	t.n--
}

func (t *pendingTable) pop(k key) bool {
	if i := t.find(k); i >= 0 {
		t.remove(i)
		return true
	}
	return false
}

type deferred struct {
	due     Time
	arrival uint32
	frame   [7]byte
	n       int
}

// Device is one device endpoint.
type Device struct {
	cfg  DeviceConfig
	app  Application
	out  FrameSender
	rand Rand
	log  RecordLog

	// slots[order[0]] is the sender used least recently. Slots in use are
	// always 0..nSenders-1 (an eviction reuses its slot), so no record is
	// copied when the order changes.
	slots    [MaxSenders]senderRecords
	order    [MaxSenders]uint8
	nSenders int

	State             byte
	heartbeatSequence byte
	nextHeartbeat     Time
	deferred          [MaxDeferred]deferred
	nDeferred         int
	arrivals          uint32
	running           pendingTable
	answers           pendingTable
	results           pendingTable
	told              [maxPending]key
	nTold             int
	buf               [7]byte
	Stats             DeviceStats
}

// Init sets the device up and runs Reset at now.
func (d *Device) Init(cfg DeviceConfig, app Application, out FrameSender, r Rand, now Time) {
	clamp := func(v *int, max int) {
		if *v < 1 {
			*v = 1
		}
		if *v > max {
			*v = max
		}
	}
	clamp(&cfg.DedupWindow, MaxDedupWindow)
	clamp(&cfg.MaxSenders, MaxSenders)
	clamp(&cfg.MaxDeferred, MaxDeferred)
	clamp(&cfg.MaxOpen, MaxOpen)
	*d = Device{} // a literal with fields would build a 2.8 KB temporary on the heap (TinyGo)
	d.cfg, d.app, d.out, d.rand = cfg, app, out, r
	d.Reset(now)
}

// SetLog sends every change of a record to l (call it after Init and the
// Restores; nil: RAM only).
func (d *Device) SetLog(l RecordLog) { d.log = l }

// Restore puts back one record read from the log, in the order written (a
// later write of the same record updates it); call Reset after the last one.
func (d *Device) Restore(sender byte, id uint16, result byte, open bool) {
	if s := d.findSender(sender); s >= 0 {
		sr := &d.slots[s]
		for i := 0; i < sr.n; i++ {
			if sr.recs[i].id == id {
				sr.recs[i].result, sr.recs[i].open = result, open
				d.trim(sr)
				return
			}
		}
	}
	d.remember(sender, id, result, 0, open)
}

// RestoreTouch puts back a Touch read from the log.
func (d *Device) RestoreTouch(sender byte) {
	if s := d.findSender(sender); s >= 0 {
		d.touch(s)
	}
}

// EachRecord lists every record: senders from the one heard from longest
// ago, each one's records oldest first, so Restore in this order rebuilds
// them (a log compacts with it). false when v stopped it.
func (d *Device) EachRecord(v RecordVisitor) bool {
	for s := 0; s < d.nSenders; s++ {
		sr := &d.slots[d.order[s]]
		for _, r := range sr.recs[:sr.n] {
			if !v.Record(sr.sender, r.id, r.result, r.open) {
				return false
			}
		}
	}
	return true
}

// Reset returns to the power-on state; the records are kept (a reboot,
// spec 4.1). Records read back count as written now (no RTC); an action the
// reboot cut short ends in INTERRUPTED and never runs again; every open
// record's RESULT goes again.
func (d *Device) Reset(now Time) {
	d.State = StateSleep
	if d.cfg.Awake {
		d.State = StateReady
	}
	d.heartbeatSequence = 0
	d.nextHeartbeat = Never
	if d.cfg.HeartbeatInterval < Never {
		first := d.cfg.HeartbeatInterval
		if first < 200*Millisecond {
			first = 200 * Millisecond
		}
		d.nextHeartbeat = after(now, d.rand.Uniform(200*Millisecond, first))
	}
	d.nDeferred = 0
	d.running.n, d.answers.n, d.results.n, d.nTold = 0, 0, 0, 0
	for s := 0; s < d.nSenders; s++ {
		sr := &d.slots[d.order[s]]
		for i := 0; i < sr.n; i++ {
			r := &sr.recs[i]
			if r.result == ResultAccepted {
				r.result = ResultInterrupted
			}
			r.logged = now
			if r.open {
				d.results.set(pending{key: key{sr.sender, r.id}, at: after(now, d.rand.Uniform(0, d.cfg.ResultTimeout))})
			}
		}
	}
}

// NextDue is the earliest time Tick does anything.
func (d *Device) NextDue() Time {
	due := d.nextHeartbeat
	for i := 0; i < d.nDeferred; i++ {
		due = min(due, d.deferred[i].due)
	}
	for i := 0; i < d.running.n; i++ {
		due = min(due, d.running.items[i].at)
	}
	for i := 0; i < d.answers.n; i++ {
		due = min(due, d.answers.items[i].at)
	}
	for i := 0; i < d.results.n; i++ {
		due = min(due, d.results.items[i].at)
	}
	return due
}

// OnFrame takes one plain frame (above the security layer, unrouted).
func (d *Device) OnFrame(frame []byte, now Time) {
	if len(frame) == 0 || fixedLen(frame[0]) != len(frame) {
		d.Stats.Ignored++
		return
	}
	switch frame[0] {
	case KindWake, KindRequest, KindReply:
	default:
		d.Stats.Ignored++
		return
	}
	if frame[2] != d.cfg.ID {
		return
	}
	if frame[0] == KindReply {
		d.onAck(frame) // at once: it only stops a repeat
		return
	}
	if d.State == StateSleep && d.cfg.SleepDelayMax > 0 {
		d.defer_(frame, now)
		return
	}
	d.handle(frame, now)
}

func (d *Device) onAck(frame []byte) {
	k := key{frame[1], MessageID(frame)}
	if d.results.pop(k) {
		d.Stats.ResultsAcked++
		if d.close(k) {
			d.relog(k)
		}
	}
}

// defer_ queues a frame received in SLEEP; a copy of a waiting frame adds
// nothing, a full queue drops it (the controller retries).
func (d *Device) defer_(frame []byte, now Time) {
	for i := 0; i < d.nDeferred; i++ {
		e := &d.deferred[i]
		if string(e.frame[:e.n]) == string(frame) {
			d.Stats.DeferredDropped++
			return
		}
	}
	if d.nDeferred >= d.cfg.MaxDeferred {
		d.Stats.DeferredDropped++
		return
	}
	e := &d.deferred[d.nDeferred]
	e.due = after(now, d.rand.Uniform(d.cfg.SleepDelayMin, d.cfg.SleepDelayMax))
	e.arrival = d.arrivals
	e.n = copy(e.frame[:], frame)
	d.arrivals++
	d.nDeferred++
}

// Tick advances time: deferred frames, finished actions, RESULTs, heartbeats.
func (d *Device) Tick(now Time) {
	for {
		best := -1
		for i := 0; i < d.nDeferred; i++ {
			e := &d.deferred[i]
			if e.due <= now && (best < 0 || e.due < d.deferred[best].due ||
				e.due == d.deferred[best].due && e.arrival < d.deferred[best].arrival) {
				best = i
			}
		}
		if best < 0 {
			break
		}
		var frame [7]byte
		n := copy(frame[:], d.deferred[best].frame[:d.deferred[best].n])
		copy(d.deferred[best:], d.deferred[best+1:d.nDeferred])
		d.nDeferred--
		d.handle(frame[:n], now)
	}

	for i := 0; i < d.answers.n; {
		a := d.answers.items[i]
		if now < a.at {
			i++
			continue
		}
		// Over within the budget: the answer carries the result, no RESULT.
		d.answers.remove(i)
		a.result = d.deadline(a.result)
		d.setResult(a.key, a.result)
		if d.untell(a.key) { // a retry heard ACCEPTED meanwhile: report it as well
			d.results.set(pending{key: a.key, at: now})
		} else {
			d.close(a.key)
		}
		d.relog(a.key) // before the answer: a reboot must not turn it into ERROR
		d.reply(a.frame[:], a.result)
	}

	for i := 0; i < d.running.n; {
		r := d.running.items[i]
		if now < r.at {
			i++
			continue
		}
		d.running.remove(i)
		d.setResult(r.key, d.deadline(r.result))
		d.relog(r.key)
		d.results.set(pending{key: r.key, at: now})
	}

	for i := 0; i < d.results.n; {
		out := &d.results.items[i]
		if now < out.at {
			i++
			continue
		}
		if out.sent > d.cfg.ResultRetries {
			// The record keeps the result (still open): the controller asks for it.
			d.results.remove(i)
			d.Stats.ResultsAbandoned++
			continue
		}
		k, sent := out.key, out.sent
		if !d.sendResult(k, now) {
			d.results.remove(i) // the record is gone: nothing to say
			continue
		}
		out.at = after(now, scale(d.rand, d.cfg.ResultTimeout<<sent, 75, 125))
		out.sent = sent + 1
		i++
	}

	if now >= d.nextHeartbeat {
		d.sendHeartbeat()
		d.nextHeartbeat = after(now, scale(d.rand, d.cfg.HeartbeatInterval, 85, 115))
	}
}

func (d *Device) handle(frame []byte, now Time) {
	switch frame[0] {
	case KindWake:
		// Idempotent, not recorded: a late WAKE retry must not push a REQUEST's record out.
		d.State = StateReady
		d.reply(frame, ResultOK)
	case KindRequest:
		d.handleRequest(frame, now)
	}
}

func (d *Device) handleRequest(frame []byte, now Time) {
	sender, id := frame[1], MessageID(frame)
	// Any REQUEST (a retry, a query too) makes its sender the one heard from
	// last: past MaxSenders the one heard from longest ago goes (spec 4.1, Q23).
	if s := d.findSender(sender); s >= 0 && d.touch(s) && d.log != nil && !d.log.Touch(sender) {
		d.Stats.LogFailed++ // the order after a reboot may be older: nothing else changes
	}
	if frame[5] == QueryAction {
		d.handleQuery(frame, now)
		return
	}
	if previous, ok := d.recentResult(sender, id, now); ok {
		// Replay the original result: a lost ERROR must not turn into OK.
		k := key{sender, id}
		if previous == ResultAccepted && d.answers.find(k) >= 0 {
			d.tell(k) // it heard ACCEPTED: a RESULT will follow
		} else if previous != ResultAccepted {
			// The final result goes out: the record closes, as for a query
			// (spec 4.1). Left open, the controller would take the id as free
			// while the device still keeps it (STATUS P4).
			d.answered(k)
		}
		d.reply(frame, previous)
		return
	}
	duration := d.app.Duration(frame[5], frame[6])
	if duration > 0 {
		d.accept(frame, duration, now)
		return
	}
	// Logged ACCEPTED before it runs: a reboot in between makes it INTERRUPTED,
	// never a second run (spec 4.1).
	if !d.logged(sender, id, ResultAccepted, true) {
		return
	}
	d.remember(sender, id, ResultAccepted, now, true)
	result := d.app.Perform(Request{sender, id, frame[5], frame[6]})
	if result == ResultPending {
		result = ResultError // an instant action cannot complete later
	}
	k := key{sender, id}
	d.setResult(k, result)
	d.close(k)
	d.relog(k)
	d.reply(frame, result)
}

// accept logs an action that takes time ACCEPTED, then runs it. Within
// AnswerBudget the answer waits and carries the result; past it ACCEPTED
// goes at once and a RESULT follows.
func (d *Device) accept(frame []byte, duration, now Time) {
	sender, id := frame[1], MessageID(frame)
	if d.openCount(sender) >= d.cfg.MaxOpen {
		// Every open slot is taken: refused, and final.
		if !d.logged(sender, id, ResultNotRun, false) {
			return
		}
		d.Stats.RefusedBusy++
		d.remember(sender, id, ResultNotRun, now, false)
		d.reply(frame, ResultNotRun)
		return
	}
	if !d.logged(sender, id, ResultAccepted, true) {
		return
	}
	d.remember(sender, id, ResultAccepted, now, true) // logged before it runs
	k := key{sender, id}
	p := pending{key: k, at: after(now, duration)}
	if duration <= d.cfg.AnswerBudget {
		copy(p.frame[:], frame)
		p.result = d.app.Perform(Request{sender, id, frame[5], frame[6]})
		d.answers.set(p)
		return
	}
	d.Stats.Accepted++
	p.result = d.app.Perform(Request{sender, id, frame[5], frame[6]})
	d.running.set(p)
	d.reply(frame, ResultAccepted)
}

// Complete reports the result (ResultOK or ResultError) of an action that
// returned ResultPending; the answer or RESULT goes at the next Tick at or
// after now. false when the action is not waiting any more (deadline
// passed, a reboot cut it, completed already): nothing changes.
func (d *Device) Complete(sender byte, id uint16, result byte, now Time) bool {
	if result != ResultOK && result != ResultError {
		return false
	}
	k := key{sender, id}
	for _, t := range [2]*pendingTable{&d.answers, &d.running} {
		if i := t.find(k); i >= 0 && t.items[i].result == ResultPending {
			t.items[i].result, t.items[i].at = result, now
			return true
		}
	}
	d.Stats.LateCompletions++
	return false
}

// deadline is the result at an action's end: still pending, it missed its deadline.
func (d *Device) deadline(result byte) byte {
	if result == ResultPending {
		d.Stats.DeadlineMissed++
		return ResultError
	}
	return result
}

func (d *Device) handleQuery(frame []byte, now Time) {
	sender := frame[1]
	asked := uint16(frame[4])<<8 | uint16(frame[6])
	k := key{sender, asked}
	result, ok := d.recentResult(sender, asked, now)
	switch {
	case !ok:
		// Never seen: say so and make it final, a late copy must not run.
		result = ResultNotRun
		if !d.logged(sender, asked, result, false) {
			return
		}
		d.remember(sender, asked, result, now, false)
	case result != ResultAccepted:
		d.answered(k)
	case d.answers.find(k) >= 0:
		// It met a short action mid-run and heard ACCEPTED: a RESULT follows (review A6).
		d.tell(k)
	}
	d.reply(frame, result)
}

// answered: a final result went out from k's record, so the controller has
// it: no RESULT to send any more, the record closes.
func (d *Device) answered(k key) {
	if d.results.pop(k) {
		d.Stats.ResultsAcked++
	}
	if d.close(k) {
		d.relog(k)
	}
}

func (d *Device) tell(k key) {
	for i := 0; i < d.nTold; i++ {
		if d.told[i] == k {
			return
		}
	}
	if d.nTold < len(d.told) {
		d.told[d.nTold] = k
		d.nTold++
	}
}

func (d *Device) untell(k key) bool {
	for i := 0; i < d.nTold; i++ {
		if d.told[i] == k {
			copy(d.told[i:], d.told[i+1:d.nTold])
			d.nTold--
			return true
		}
	}
	return false
}

// findSender returns the sender's slot, or -1.
func (d *Device) findSender(sender byte) int {
	for i := 0; i < d.nSenders; i++ {
		if d.slots[i].sender == sender {
			return i
		}
	}
	return -1
}

func (d *Device) openCount(sender byte) int {
	s := d.findSender(sender)
	if s < 0 {
		return 0
	}
	n := 0
	for _, r := range d.slots[s].recs[:d.slots[s].n] {
		if r.open {
			n++
		}
	}
	return n
}

// recentResult looks a request up; records past DedupTTL leave first.
func (d *Device) recentResult(sender byte, id uint16, now Time) (byte, bool) {
	s := d.findSender(sender)
	if s < 0 {
		return 0, false
	}
	sr := &d.slots[s]
	if d.cfg.DedupTTL < Never {
		drop := 0
		for drop < sr.n && now-sr.recs[drop].logged > d.cfg.DedupTTL {
			drop++
		}
		copy(sr.recs[:], sr.recs[drop:sr.n])
		sr.n -= drop
	}
	for _, r := range sr.recs[:sr.n] {
		if r.id == id {
			return r.result, true
		}
	}
	return 0, false
}

// remember appends a record; the sender becomes the most recent one, and a
// new sender past MaxSenders pushes out the least recently used one.
func (d *Device) remember(sender byte, id uint16, result byte, now Time, open bool) {
	slot := d.findSender(sender)
	if slot >= 0 {
		d.touch(slot)
	} else {
		if d.nSenders >= d.cfg.MaxSenders {
			slot = int(d.order[0])
			copy(d.order[:], d.order[1:d.nSenders])
			d.nSenders--
		} else {
			slot = d.nSenders
		}
		d.slots[slot] = senderRecords{sender: sender}
		d.order[d.nSenders] = uint8(slot)
		d.nSenders++
	}
	sr := &d.slots[slot]
	if sr.n < len(sr.recs) {
		sr.recs[sr.n] = record{logged: now, id: id, result: result, open: open}
		sr.n++
	}
	d.trim(sr)
}

// touch makes a slot the most recently used; true when the order changed.
func (d *Device) touch(slot int) bool {
	for i := 0; i < d.nSenders; i++ {
		if int(d.order[i]) == slot {
			copy(d.order[i:], d.order[i+1:d.nSenders])
			d.order[d.nSenders-1] = uint8(slot)
			return i < d.nSenders-1
		}
	}
	return false
}

// logged writes a record's state to the log; true without a log.
func (d *Device) logged(sender byte, id uint16, result byte, open bool) bool {
	if d.log == nil || d.log.Write(sender, id, result, open) {
		return true
	}
	d.Stats.LogFailed++
	return false
}

// relog writes the state a record has now, after a change in RAM. A failed
// write leaves the older state for a reboot to find: an ACCEPTED one reads
// back as INTERRUPTED, an open one sends its RESULT again.
func (d *Device) relog(k key) {
	s := d.findSender(k.sender)
	if s < 0 {
		return
	}
	for _, r := range d.slots[s].recs[:d.slots[s].n] {
		if r.id == k.id {
			d.logged(k.sender, k.id, r.result, r.open)
			return
		}
	}
}

func (d *Device) setResult(k key, result byte) {
	s := d.findSender(k.sender)
	if s < 0 {
		return
	}
	sr := &d.slots[s]
	for i := 0; i < sr.n; i++ {
		if sr.recs[i].id == k.id {
			sr.recs[i].result = result
		}
	}
}

// close ends an open record that has its result; true when it did.
func (d *Device) close(k key) bool {
	s := d.findSender(k.sender)
	if s < 0 {
		return false
	}
	sr := &d.slots[s]
	closed := false
	for i := 0; i < sr.n; i++ {
		r := &sr.recs[i]
		if r.id == k.id && r.open && r.result != ResultAccepted {
			r.open, closed = false, true
		}
	}
	if sr.n > 0 {
		d.trim(sr)
	}
	return closed
}

// trim keeps DedupWindow closed records per sender, pushing the oldest out;
// open ones come on top and are never pushed out (review A2).
func (d *Device) trim(sr *senderRecords) {
	for {
		closed := 0
		for _, r := range sr.recs[:sr.n] {
			if !r.open {
				closed++
			}
		}
		if closed <= d.cfg.DedupWindow {
			return
		}
		for i := 0; i < sr.n; i++ {
			if !sr.recs[i].open {
				copy(sr.recs[i:], sr.recs[i+1:sr.n])
				sr.n--
				break
			}
		}
	}
}

func (d *Device) sendHeartbeat() {
	b := d.buf[:5]
	b[0], b[1], b[2], b[3], b[4] = KindHeartbeat, d.cfg.ID, d.cfg.Controller, d.heartbeatSequence, d.State
	d.heartbeatSequence++
	d.out.Send(b)
}

// sendResult sends the RESULT of a record; false when the record is gone
// or not over.
func (d *Device) sendResult(k key, now Time) bool {
	result, ok := d.recentResult(k.sender, k.id, now)
	if !ok || result == ResultAccepted {
		return false
	}
	d.Stats.ResultsSent++
	b := d.buf[:6]
	b[0], b[1], b[2], b[3], b[4], b[5] = KindResult, d.cfg.ID, k.sender, byte(k.id), byte(k.id>>8), result
	d.out.Send(b)
	return true
}

// reply answers a WAKE or REQUEST: SEQ and EPOCH echoed.
func (d *Device) reply(frame []byte, result byte) {
	b := d.buf[:6]
	b[0], b[1], b[2], b[3], b[4], b[5] = KindReply, d.cfg.ID, frame[1], frame[3], frame[4], result
	d.out.Send(b)
}
