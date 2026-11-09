package mesh

import "cairnmesh/firmware/proto"

// Path diagnostics (spec 11.8): every node keeps a short log of the routed
// messages it saw (what it heard, from whom, how often it sent, how it
// ended). Nothing of it goes on air until a TRACE asks about a message:
// the target answers with its own record, and every relay on the way back
// appends its record to the TRACE_REPLY before passing it on.

const (
	// MaxRouteLog routed messages kept (spec 9).
	MaxRouteLog = 8
	// MaxTraces traces passing through, and their replies to pass on.
	MaxTraces = 2
	// recordBytes is one node's record in a TRACE_REPLY; it grows the reply
	// by this less our place in the route.
	recordBytes = 4
	traceGrowth = recordBytes - 1
)

// Trace record outcomes (frames.TRACE_*).
const (
	traceUnseen = iota
	traceDone
	traceGaveUp
	traceOther
)

type hopState uint8

const (
	statePending hopState = iota
	stateSent
	stateAcked
	stateAnswered
	stateDelivered
	stateGaveUp
	stateSkipped
)

// logKey names a routed message: KIND (flags off), SENDER, TARGET, SEQ, EPOCH.
type logKey [5]byte

func logKeyOf(plain []byte) logKey {
	return logKey{kindOf(plain), plain[1], plain[2], plain[3], plain[4]}
}

// hopRecord is what this node saw of one routed message (mesh.HopRecord).
type hopRecord struct {
	key       logKey
	rt        route
	index     int
	prevRSSI  RSSI // the copy from the node just before us: this hop's link
	prevSet   bool
	sends     uint8
	state     hopState
	blindLast bool
}

type traceQuery struct {
	sender byte
	id     uint16
	seq    byte
	epoch  byte
	target byte
}

type traceKey struct {
	gateway byte
	id      uint16
}

type traceOut struct {
	key      traceKey
	plain    [proto.MaxFrame]byte
	n        int
	records  int
	due      proto.Time
	expires  proto.Time
	blind    bool
	raw      [proto.MaxFrame]byte
	rawN     int
	sends    uint8
	acked    bool
	echo     bool
	passOnce bool
}

var zeroTraceOut traceOut

// logSeen notes a routed message seen at our place index (a retry with a
// fresh FCNT starts its record over, at the end) and returns its key.
func (r *Relay) logSeen(plain []byte, rt route, index int, rssi RSSI, tx byte, hasTx bool) logKey {
	key := logKeyOf(plain)
	at := -1
	for i := 0; i < r.nLog; i++ {
		if r.routeLog[i].key == key {
			at = i
		}
	}
	switch {
	case at >= 0:
		copy(r.routeLog[at:], r.routeLog[at+1:r.nLog])
		r.nLog--
	case r.nLog == MaxRouteLog:
		copy(r.routeLog[:], r.routeLog[1:])
		r.nLog--
	}
	rec := &r.routeLog[r.nLog]
	r.nLog++
	*rec = hopRecord{key: key, rt: rt, index: index}
	kind := key[0]
	rec.blindLast = inbound(kind) && index == rt.n-2 && !(r.cfg.LastHopAck && echoed(kind))
	if hasTx {
		r.logPrev(rec, tx, rssi)
	}
	return key
}

func (r *Relay) logPrev(rec *hopRecord, tx byte, rssi RSSI) {
	if rssi == NoRSSI || rec.prevSet || rec.index == 0 {
		return
	}
	if tx == rec.rt.nodes[rec.index-1] {
		rec.prevRSSI, rec.prevSet = rssi, true
	}
}

// logEvent records a send (waits: an acknowledgement is awaited) or an
// outcome of the message under key.
func (r *Relay) logEvent(key logKey, has bool, event hopState, waits bool) {
	if !has {
		return
	}
	rec := r.findLog(key)
	if rec == nil {
		return
	}
	if event == stateSent {
		rec.sends++
		if !waits && rec.state == statePending {
			rec.state = stateSent
		}
		return
	}
	if rec.state == statePending || rec.state == stateSent {
		rec.state = event
	}
}

func (r *Relay) findLog(key logKey) *hopRecord {
	for i := 0; i < r.nLog; i++ {
		if r.routeLog[i].key == key {
			return &r.routeLog[i]
		}
	}
	return nil
}

// noteTrace remembers a TRACE passing by; false (BadRoute) when malformed.
func (r *Relay) noteTrace(plain []byte) bool {
	cut, _, ok := proto.SplitRoute(plain)
	if !ok || cut != 7 {
		r.Stats.BadRoute++
		return false
	}
	q := traceQuery{sender: plain[1], id: proto.MessageID(plain), seq: plain[5], epoch: plain[6], target: plain[2]}
	for i := 0; i < r.nTraces; i++ {
		if r.traces[i].sender == q.sender && r.traces[i].id == q.id {
			copy(r.traces[i:], r.traces[i+1:r.nTraces])
			r.nTraces--
			break
		}
	}
	if r.nTraces == MaxTraces {
		copy(r.traces[:], r.traces[1:])
		r.nTraces--
	}
	r.traces[r.nTraces] = q
	r.nTraces++
	return true
}

func (r *Relay) findTrace(k traceKey) *traceQuery {
	for i := 0; i < r.nTraces; i++ {
		if r.traces[i].sender == k.gateway && r.traces[i].id == k.id {
			return &r.traces[i]
		}
	}
	return nil
}

// recordFor appends this node's 4-byte record for the message a trace asks
// about (mesh.node_report): the outward message and its reply.
func (r *Relay) recordFor(k traceKey, dst []byte) []byte {
	var out, back *hopRecord
	if q := r.findTrace(k); q != nil {
		for _, kind := range [...]byte{proto.KindRequest, proto.KindWake, proto.KindKeepalive} {
			if out = r.findLog(logKey{kind, k.gateway, q.target, q.seq, q.epoch}); out != nil {
				break
			}
		}
		back = r.findLog(logKey{proto.KindReply, q.target, k.gateway, q.seq, q.epoch})
	}
	rssiOut, snrOut, rssiBack := byte(0), byte(0x80), byte(0)
	flags := byte(traceUnseen<<4 | traceUnseen)
	if out != nil {
		if out.prevSet {
			rssiOut = dbmByte(out.prevRSSI)
			snr := min(127, max(-127, roundHalfEven(4*int64(out.prevRSSI-r.cfg.NoiseFloor), 1024)))
			snrOut = byte(int8(snr))
		}
		flags = min(3, out.repeats())<<6 | traceState(out)<<4
	}
	if back != nil {
		if back.prevSet {
			rssiBack = dbmByte(back.prevRSSI)
		}
		flags |= min(3, back.repeats())<<2 | traceState(back)
	}
	return append(dst, rssiOut, snrOut, rssiBack, flags)
}

// dbmByte is a level as the record's byte: -dBm, 1 .. 255.
func dbmByte(l RSSI) byte {
	return byte(-min(-1, max(-255, roundHalfEven(int64(l), 1024))))
}

// repeats: sends beyond the first that loss made necessary (a last hop into
// the gateway goes twice by design).
func (h *hopRecord) repeats() byte {
	planned := uint8(1)
	if h.blindLast {
		planned = 2
	}
	if h.sends <= planned {
		return 0
	}
	return h.sends - planned
}

func traceState(h *hopRecord) byte {
	switch h.state {
	case stateAcked, stateAnswered, stateDelivered, stateSent:
		return traceDone
	case stateGaveUp:
		return traceGaveUp
	}
	return traceOther
}

// answerTrace: we are the trace's target; the answer starts with our record
// and goes back along the TRACE's relays, reversed.
func (r *Relay) answerTrace(plain []byte, rt route, now proto.Time) {
	k := traceKey{plain[1], proto.MessageID(plain)}
	if r.findTraceOut(k) != nil {
		return // a repeat of the trace: our answer is on its way
	}
	a := append(r.scratch[:0], proto.KindTraceReply, r.cfg.Node, plain[1], plain[3], plain[4])
	a = r.recordFor(k, a)
	r.Stats.TracesAnswered++
	var back [proto.MaxRelays]byte
	n := 0
	for i := rt.n - 2; i >= 1; i-- {
		back[n] = rt.nodes[i]
		n++
	}
	r.queueTrace(k, a, 1, back[:n], now)
}

// onTraceReply: a routed TRACE_REPLY heard for the first time.
func (r *Relay) onTraceReply(plain []byte, now proto.Time) {
	cut, rest, ok := proto.SplitRoute(plain)
	if !ok || cut < 5+recordBytes || (cut-5)%recordBytes != 0 {
		r.Stats.BadRoute++
		return
	}
	records := (cut - 5) / recordBytes
	k := traceKey{plain[2], proto.MessageID(plain)}
	if mine := r.findTraceOut(k); mine != nil {
		if records > mine.records {
			mine.acked = true // the next node has it, with its record added
		} else if records < mine.records && mine.acked && mine.rawN > 0 {
			// The node before us sent its copy again: it missed ours.
			mine.echo = true
			mine.due = now + r.rand.Uniform(0, r.cfg.RouteJitter)
		}
	}
	if q := r.findTrace(k); q != nil {
		// The trace's answer: every hop of the TRACE is done.
		r.onReply(requestID{plain[2], q.target, plain[3], plain[4]})
	}
	if !r.cfg.Forward || len(rest) == 0 || rest[0] != r.cfg.Node || r.findTraceOut(k) != nil {
		return
	}
	a := append(r.scratch[:0], proto.KindTraceReply, r.cfg.Node, plain[2], plain[3], plain[4])
	a = append(a, plain[5:cut]...)
	a = r.recordFor(k, a)
	r.Stats.TracesRelayed++
	var tail [proto.MaxRelays]byte
	n := copy(tail[:], rest[1:])
	r.queueTrace(k, a, records+1, tail[:n], now)
}

func (r *Relay) findTraceOut(k traceKey) *traceOut {
	for i := 0; i < r.nTraceOut; i++ {
		if r.traceOut[i].key == k {
			return &r.traceOut[i]
		}
	}
	return nil
}

// queueTrace: a TRACE_REPLY to pass on along rest (empty: into the
// gateway, sent twice, blind), confirmed like a probe by the next copy.
func (r *Relay) queueTrace(k traceKey, reply []byte, records int, rest []byte, now proto.Time) {
	if r.nTraceOut == MaxTraces {
		victim := 0
		for i := 1; i < r.nTraceOut; i++ {
			if r.traceOut[i].expires < r.traceOut[victim].expires {
				victim = i
			}
		}
		copy(r.traceOut[victim:], r.traceOut[victim+1:r.nTraceOut])
		r.nTraceOut--
	}
	t := &r.traceOut[r.nTraceOut]
	r.nTraceOut++
	*t = zeroTraceOut
	t.key, t.records, t.blind = k, records, len(rest) == 0
	t.n = len(withRoute(t.plain[:0], reply, rest))
	t.due = now + r.rand.Uniform(0, r.cfg.RouteJitter)
	t.expires = now + r.cfg.ProbeTTL
}

// withRoute is frames.add_route into dst: ROUTED set, the relays, RINFO.
func withRoute(dst, plain, relays []byte) []byte {
	dst = append(dst, plain[0]|proto.FlagRouted)
	dst = append(dst, plain[1:]...)
	dst = append(dst, relays...)
	return append(dst, byte(len(relays)))
}

// tickTraces marks the replies to (re)send now; isBusy: listen before talk.
func (r *Relay) tickTraces(now, busy proto.Time, isBusy bool) {
	for i := 0; i < r.nTraceOut; {
		t := &r.traceOut[i]
		if now >= t.expires {
			copy(r.traceOut[i:], r.traceOut[i+1:r.nTraceOut])
			r.nTraceOut--
			continue
		}
		i++
		if now < t.due || t.acked && !t.echo {
			continue
		}
		if isBusy {
			t.due = busy + r.rand.Uniform(0, r.cfg.RouteJitter)
			continue
		}
		if t.echo {
			t.echo, t.passOnce = false, true
			continue
		}
		limit := r.cfg.RouteRetries
		if t.blind {
			limit = 1
		}
		if t.sends > limit {
			t.acked = true // give up; kept so a late repeat makes no second answer
			r.Stats.RouteDropped++
			continue
		}
		t.sends++
		length := t.n + sealedExtra
		if t.blind {
			t.due = now + r.repeatGap(length)
		} else {
			t.due = now + r.routeHopTimeout(length+traceGrowth, 0, false) + r.rand.Uniform(0, r.airtime(length))
		}
		t.passOnce = true
	}
}

// passTraces sends the marked replies, sealed once as our own frame.
func (r *Relay) passTraces() {
	for i := 0; i < r.nTraceOut; i++ {
		t := &r.traceOut[i]
		if !t.passOnce {
			continue
		}
		t.passOnce = false
		if t.rawN == 0 {
			if r.Sealer == nil {
				r.Stats.Unsealed++
				continue
			}
			n, ok := r.Sealer.SealFrame(t.raw[:], t.plain[:t.n])
			if !ok {
				r.Stats.Unsealed++
				continue
			}
			t.rawN = n
			fcnt := uint32(t.raw[4]) | uint32(t.raw[5])<<8 | uint32(t.raw[6])<<16 | uint32(t.raw[7])<<24
			r.windows[r.cfg.Node].Accept(fcnt)
		}
		r.radio.Put(t.raw[:t.rawN], 8)
	}
}

func (r *Relay) tracesDue() proto.Time {
	due := proto.Never
	for i := 0; i < r.nTraceOut; i++ {
		t := &r.traceOut[i]
		due = min(due, t.expires)
		if !(t.acked && !t.echo) {
			due = min(due, t.due)
		}
	}
	return due
}
