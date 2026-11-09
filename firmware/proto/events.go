package proto

// Module events: the EVENT frame's sender (wire format v4, spec section 19;
// uplink.EventReporter). A module raises an event id the gateway defined;
// the node queues it (one entry per id, MaxEvents at most), sends it while
// awake and again until the controller's network-only REPLY acknowledges
// it, never giving up. A dormant node only queues (its loop does not tick
// this); Resume sends what waited within the wake spread.

// MaxEvents is the queue (spec 18 max_events).
const MaxEvents = 4

// EventConfig holds the timings (spec 18); DefaultEventConfig has the defaults.
type EventConfig struct {
	DelayMin, DelayMax   Time // raised while awake: sent this long after
	SpreadMin, SpreadMax Time // queued when the node wakes: sent this long after
	Timeout, BackoffMax  Time // unanswered: again after Timeout, doubling up to BackoffMax (±25 %)
}

// DefaultEventConfig is uplink.EventReporter's defaults.
func DefaultEventConfig() EventConfig {
	return EventConfig{
		DelayMin: 0, DelayMax: 2 * Second,
		SpreadMin: 1 * Second, SpreadMax: 30 * Second,
		Timeout: 15 * Second, BackoffMax: 600 * Second,
	}
}

// EventStats counts what Events did.
type EventStats struct {
	Raised, Merged, Refused, Sent, Acked uint32
}

type queuedEvent struct {
	id, seq byte
	sent    uint8
	due     Time
}

// Events is one node's queue of module events.
type Events struct {
	cfg              EventConfig
	node, controller byte
	// epoch names this run of the node in its message ids (as its REPORTs').
	epoch, seq byte
	rand       Rand
	q          [MaxEvents]queuedEvent // in the order raised
	n          int
	frame      [6]byte
	Stats      EventStats
}

// Init sets the node, the controller, this run's EPOCH and where the
// sequence counter starts (both drawn at boot on the board).
func (e *Events) Init(cfg EventConfig, node, controller, epoch, seq byte, r Rand) {
	*e = Events{cfg: cfg, node: node, controller: controller, epoch: epoch, seq: seq, rand: r}
}

// NextSequence is the node's next own message SEQUENCE, shared by its
// EVENTs and REPORTs (node.Watch): a REPLY acknowledging one names no other.
func (e *Events) NextSequence() byte {
	e.seq++
	return e.seq
}

// Pending reports how many events wait for an acknowledgement.
func (e *Events) Pending() int { return e.n }

// Notify queues the event id; false when the queue is full. An id already
// queued is merged. Callable at any time, a dormant node included.
func (e *Events) Notify(id byte, now Time) bool {
	for i := 0; i < e.n; i++ {
		if e.q[i].id == id {
			e.Stats.Merged++
			return true
		}
	}
	if e.n >= MaxEvents {
		e.Stats.Refused++
		return false
	}
	e.q[e.n] = queuedEvent{id: id, seq: e.NextSequence(), due: after(now, e.rand.Uniform(e.cfg.DelayMin, e.cfg.DelayMax))}
	e.n++
	e.Stats.Raised++
	return true
}

// Resume: the node woke; everything queued goes out within the spread, at
// the fast pace again.
func (e *Events) Resume(now Time) {
	for i := 0; i < e.n; i++ {
		e.q[i].due = after(now, e.rand.Uniform(e.cfg.SpreadMin, e.cfg.SpreadMax))
		e.q[i].sent = 0
	}
}

// Forget empties the queue (a reboot: it was in RAM).
func (e *Events) Forget() { e.n = 0 }

// NextDue is the earliest send.
func (e *Events) NextDue() Time {
	due := Never
	for i := 0; i < e.n; i++ {
		due = min(due, e.q[i].due)
	}
	return due
}

// Tick sends what is due.
func (e *Events) Tick(now Time, out FrameSender) {
	for i := 0; i < e.n; i++ {
		ev := &e.q[i]
		if now < ev.due {
			continue
		}
		e.frame = [6]byte{KindEvent, e.node, e.controller, ev.seq, e.epoch, ev.id}
		out.Send(e.frame[:])
		e.Stats.Sent++
		backoff := e.cfg.BackoffMax
		if ev.sent < 30 && e.cfg.Timeout<<ev.sent < backoff {
			backoff = e.cfg.Timeout << ev.sent
		}
		if ev.sent < 255 {
			ev.sent++
		}
		ev.due = after(now, scale(e.rand, backoff, 75, 125))
	}
}

// OnAck takes a REPLY that came without the application layer (a RESULT's
// acknowledgement carries it): the controller acknowledging an event sent.
func (e *Events) OnAck(plain []byte) {
	if len(plain) != 6 || plain[0] != KindReply || plain[1] != e.controller || plain[2] != e.node || plain[4] != e.epoch {
		return
	}
	for i := 0; i < e.n; i++ {
		if e.q[i].sent > 0 && e.q[i].seq == plain[3] {
			copy(e.q[i:e.n], e.q[i+1:e.n])
			e.n--
			e.Stats.Acked++
			return
		}
	}
}
