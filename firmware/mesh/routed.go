package mesh

import "cairnmesh/firmware/proto"

// Routed frames (spec 11.2): only the relays a frame lists pass it on, and
// only copies from a node before them in L = SENDER, R1 .. RN, TARGET. TX
// tells the copies apart: a copy from a node after us acknowledges our hop;
// a copy from before us again means that node missed ours.

// routeHopTimeout is how long a routed hop waits for its acknowledgement
// before repeating (mesh.route_hop_timeout): the next hop's copy is back
// after two airtimes, but the hop after that transmits during the third;
// three hops or fewer from a target that answers, wait for the answer.
func (r *Relay) routeHopTimeout(length, remaining int, answered bool) proto.Time {
	air, jitter := r.airtime(length), r.cfg.RouteJitter
	if answered && remaining <= 3 {
		return 2*proto.Time(remaining)*(air+jitter) + r.cfg.RouteAnswer + r.cfg.RouteSlack
	}
	return 3*air + 2*jitter + r.cfg.RouteSlack
}

// repeatGap: right behind the first copy (the gateway holds its next frame
// on this route for one airtime plus jitter).
func (r *Relay) repeatGap(length int) proto.Time {
	return r.airtime(length) + r.rand.Uniform(0, r.cfg.RouteJitter)
}

// routeWait before passing on a copy heard from heardFrom: a jitter from the
// node just before us; from further back (a skipped hop) one more slot per
// relay in between, so the relay whose turn it is goes first.
func (r *Relay) routeWait(length, index, heardFrom int) proto.Time {
	slot := r.airtime(length) + r.cfg.RouteJitter + r.cfg.RouteSlack
	skipped := max(0, index-heardFrom-1)
	return r.rand.Uniform(0, r.cfg.RouteJitter) + proto.Time(skipped)*slot
}

// routedFlight queues raw (sent already, or after wait) at our place index in rt.
// routedFlight queues raw and logs the message (heard at rssi from tx, if hasTx).
func (r *Relay) routedFlight(raw, plain []byte, rt route, index int, fcnt uint32, now proto.Time, sent uint8, wait proto.Time, rssi RSSI, tx byte, hasTx bool) {
	f := r.makeRoutedFlight(raw, plain, rt, index, fcnt, now, sent, wait)
	f.logKey, f.hasLog = r.logSeen(plain, rt, index, rssi, tx, hasTx), true
	if sent > 0 {
		r.logEvent(f.logKey, true, stateSent, f.confirm)
	}
}

func (r *Relay) makeRoutedFlight(raw, plain []byte, rt route, index int, fcnt uint32, now proto.Time, sent uint8, wait proto.Time) *flight {
	kind := kindOf(plain)
	in := inbound(kind)
	f := r.push(plain[1], fcnt, raw)
	f.id, _ = requestOf(plain)
	f.routed, f.rt, f.index, f.sent = true, rt, index, sent
	// The answer to a wake probe: the relays on its way back may have gone
	// dormant again meanwhile, so every hop gets the long preamble.
	lead := proto.Time(0)
	if r.answersWakeProbe(plain) {
		f.preamble = r.cfg.WakePreamble
		lead = r.wakeLead()
	}
	if in && index == rt.n-2 && !(r.cfg.LastHopAck && echoed(kind)) {
		// Into the gateway, which relays nothing: no acknowledgement will
		// come, so it goes twice instead of waiting for one.
		f.blind = true
		if sent > 0 {
			f.due = now + r.repeatGap(f.n) + lead
		} else {
			f.due = now + wait
		}
		return f
	}
	remaining := rt.n - 1 - index
	length := f.n
	if kind == proto.KindTrace {
		length += 4 * remaining // its answer comes back longer, a record per node
	}
	timeout := r.routeHopTimeout(length, remaining, !in) + lead
	f.retries, f.hasRetries = r.cfg.RouteRetries, true
	if index == 0 && kind == proto.KindReply && r.cfg.Forward {
		// The target's answer: the last relay repeats its request until it
		// hears this, so answer each repeat instead of repeating on a timer
		// of our own (both on timers collide in step), while it can repeat.
		f.onDemand = true
		f.lingerSpan = proto.Time(r.cfg.RouteRetries+1) * (timeout + r.cfg.RouteAnswer + r.cfg.RouteJitter)
		f.linger = now + f.lingerSpan
		if sent > 0 {
			f.due = f.linger
		} else {
			f.due = now + wait
		}
		return f
	}
	if !in {
		f.request, f.hasRequest = requestOf(plain)
	}
	f.confirm, f.timeout, f.hasTimeout, f.sentAt = true, timeout, true, now
	if sent > 0 {
		f.due = now + timeout
	} else {
		f.due = now + wait
	}
	return f
}

// routeRelay: a fresh routed frame.
func (r *Relay) routeRelay(plain []byte, origin byte, fcnt uint32, tx byte, rssi RSSI, now proto.Time) {
	rt, ok := routeOf(plain)
	if !ok {
		r.Stats.BadRoute++
		return
	}
	kind := kindOf(plain)
	// A good MIC does not make a frame well formed: each field read is checked.
	if kind == proto.KindTrace && !r.noteTrace(plain) {
		return
	}
	if kind == proto.KindKeepalive && !r.noteKeepalive(plain, &rt) {
		return
	}
	if r.cfg.Node == rt.nodes[rt.n-1] {
		// We are the target: it goes up. Heard past the last relay, our
		// answer waits for that relay's copy (or its slot) first.
		r.logEvent(r.logSeen(plain, rt, rt.n-1, rssi, tx, true), true, stateDelivered, true)
		if r.cfg.LastHopAck && echoed(kind) {
			r.echo(plain, fcnt)
		}
		if kind == proto.KindTrace {
			r.answerTrace(plain, rt, now)
			return
		}
		if from := rt.index(tx); from >= 0 && !inbound(kind) {
			if skipped := rt.n - 2 - from; skipped > 0 {
				slot := r.airtime(len(plain)+sealedExtra) + r.cfg.RouteJitter + r.cfg.RouteSlack
				id, _ := requestOf(plain)
				r.setHold(id, now+proto.Time(skipped)*slot)
			}
		}
		return
	}
	index := rt.index(r.cfg.Node)
	if !r.cfg.Forward || index <= 0 || index >= rt.n-1 {
		r.Stats.NotOnRoute++
		return
	}
	from := rt.index(tx)
	if from < 0 || tx == r.cfg.Node {
		return
	}
	if from > index {
		// Someone after us already has it (a skipped hop): nothing to add.
		r.noteDone(origin, fcnt, from)
		r.Stats.Overtaken++
		r.logEvent(r.logSeen(plain, rt, index, rssi, tx, true), true, stateSkipped, true)
		return
	}
	if r.nFlights >= r.cfg.MaxPending {
		r.Stats.Overflow++
		return
	}
	n := proto.SealNetwork(r.ccm, r.scratch[:], plain, fcnt, r.cfg.Node)
	r.routedFlight(r.scratch[:n], plain, rt, index, fcnt, now, 0, r.routeWait(n, index, from), rssi, tx, true)
	r.followAnswer(origin, fcnt, plain)
}

// onRoutedCopy: a routed frame heard again.
func (r *Relay) onRoutedCopy(plain []byte, origin byte, fcnt uint32, tx byte, rssi RSSI, now proto.Time) {
	r.Stats.Duplicates++
	rt, ok := routeOf(plain)
	if !ok {
		return
	}
	index, from := rt.index(r.cfg.Node), rt.index(tx)
	if index < 0 || from < 0 {
		return
	}
	if rec := r.findLog(logKeyOf(plain)); rec != nil {
		r.logPrev(rec, tx, rssi) // the hop's own link, if this copy is from it
	}
	i := r.find(origin, fcnt)
	if from > index {
		r.noteDone(origin, fcnt, from)
		if i >= 0 {
			f := &r.flights[i]
			if f.sent > 0 {
				r.Stats.HopAcked++
				r.logEvent(f.logKey, f.hasLog, stateAcked, true)
			} else {
				r.Stats.Overtaken++
				r.logEvent(f.logKey, f.hasLog, stateSkipped, true)
			}
			r.remove(i)
		}
		return
	}
	kind := kindOf(plain)
	if index == rt.n-1 && r.cfg.LastHopAck && echoed(kind) {
		// The gateway: the node before us repeated, so it missed our echo.
		if from < index {
			r.echo(plain, fcnt)
		}
		return
	}
	if index == rt.n-1 {
		// The target. The last relay's copy releases a held answer; that
		// relay sending it again means it missed our answer, which goes
		// again at once (a different frame: nobody takes it for a retry).
		if from == index-1 {
			id, _ := requestOf(plain)
			release := now + r.rand.Uniform(0, r.cfg.RouteJitter)
			r.updateHold(id, release)
			for j := 0; j < r.nFlights; j++ {
				f := &r.flights[j]
				if f.routed && f.index == 0 && (f.sent == 0 || f.onDemand) && f.id == id {
					f.due = min(f.due, release)
					f.demanded = f.sent > 0
					if f.onDemand {
						// The relay is still repeating: answer it for as long as it can.
						f.linger = max(f.linger, release+f.lingerSpan)
					}
				}
			}
		}
		return
	}
	if from == index || !r.cfg.Forward {
		return // our own copy echoed
	}
	wait := r.routeWait(len(plain)+sealedExtra, index, from)
	if i >= 0 {
		f := &r.flights[i]
		if f.sent == 0 {
			// A closer node's copy: our turn may come sooner than planned.
			f.due = min(f.due, now+wait)
		} else if f.confirm && r.ackOverdue(f, now) {
			// We missed our own acknowledgement and repeat anyway: now, right
			// behind the node that just repeated, which takes ours for its
			// acknowledgement. Inside the normal wait our timer repeats if it
			// must: answering as well would set off a cascade of repeats.
			f.due = min(f.due, now+r.rand.Uniform(0, r.cfg.RouteJitter))
		}
		return
	}
	d := r.findDone(origin, fcnt)
	if d == nil || d.answered || r.nFlights >= r.cfg.MaxPending {
		return
	}
	if d.furthest >= rt.n {
		// Another copy of the same (SENDER, FCNT) with a shorter route: a
		// node that sealed two frames under one counter.
		r.Stats.BadRoute++
		return
	}
	d.answered = true
	// The copy of the furthest node we know has it: an acknowledgement for
	// the node behind us that nobody further on takes for a retry.
	f := r.push(origin, fcnt, nil)
	f.n = proto.SealNetwork(r.ccm, f.raw[:], plain, fcnt, rt.nodes[d.furthest])
	f.due = now + wait
	f.routed, f.rt, f.index, f.answer = true, rt, index, true
	f.logKey, f.hasLog = logKeyOf(plain), true
}

// echo: we are the gateway; the inbound frame goes back once (TX = us), its
// last hop's acknowledgement, at once (before the frame goes up).
func (r *Relay) echo(plain []byte, fcnt uint32) {
	r.Stats.Echoed++
	n := proto.SealNetwork(r.ccm, r.scratch[:], plain, fcnt, r.cfg.Node)
	r.radio.Put(r.scratch[:n], 8)
}

func (r *Relay) ackOverdue(f *flight, now proto.Time) bool {
	if f.rt.n-1-f.index <= 3 && f.hasRequest {
		return false // near the target the answer is the acknowledgement
	}
	air := r.airtime(f.n)
	return now >= f.sentAt+2*air+2*r.cfg.RouteJitter+r.cfg.RouteSlack
}

// routeBroken: a routed hop stayed unacknowledged.
func (r *Relay) routeBroken(f *flight, now proto.Time) {
	n, _, ok := proto.OpenNetwork(r.ccm, r.plain[:], f.raw[:f.n])
	if !ok {
		return
	}
	plain := r.plain[:n]
	broken := f.rt.nodes[f.index+1]
	r.logEvent(f.logKey, f.hasLog, stateGaveUp, true)
	if f.index == 0 {
		r.Stats.RouteDropped++
		if r.Failed != nil {
			r.Failed.RouteFailed(plain, broken)
		}
		return
	}
	if inbound(kindOf(plain)) {
		r.Stats.RouteDropped++ // the gateway's end-to-end timer covers it
		return
	}
	if r.Sealer == nil {
		r.Stats.Unsealed++
		return
	}
	// ROUTE_ERROR back to the sender, along the relays before us, reversed.
	e := r.scratch[:0]
	e = append(e, proto.KindRouteError|proto.FlagRouted, r.cfg.Node, f.rt.nodes[0], plain[3], plain[4], broken)
	for j := f.index - 1; j >= 1; j-- {
		e = append(e, f.rt.nodes[j])
	}
	e = append(e, byte(f.index-1))
	r.Stats.RouteErrors++
	var out [proto.MaxFrame]byte
	m, ok := r.Sealer.SealFrame(out[:], e)
	if !ok {
		r.Stats.Unsealed++
		return
	}
	r.Send(out[:m], now)
}

func (r *Relay) noteDone(origin byte, fcnt uint32, furthest int) {
	if d := r.findDone(origin, fcnt); d != nil {
		d.furthest = max(d.furthest, furthest)
		return
	}
	if r.nDone == MaxDone {
		copy(r.done[:], r.done[1:])
		r.nDone--
	}
	r.done[r.nDone] = done{origin: origin, fcnt: fcnt, furthest: furthest}
	r.nDone++
}

func (r *Relay) findDone(origin byte, fcnt uint32) *done {
	for i := 0; i < r.nDone; i++ {
		if r.done[i].origin == origin && r.done[i].fcnt == fcnt {
			return &r.done[i]
		}
	}
	return nil
}

// setHold holds the answer to id until then (oldest dropped past MaxHolds).
func (r *Relay) setHold(id requestID, until proto.Time) {
	if r.updateHold(id, until) {
		return
	}
	if r.nHolds == MaxHolds {
		copy(r.holds[:], r.holds[1:])
		r.nHolds--
	}
	r.holds[r.nHolds] = hold{id, until}
	r.nHolds++
}

func (r *Relay) updateHold(id requestID, until proto.Time) bool {
	for i := 0; i < r.nHolds; i++ {
		if r.holds[i].id == id {
			r.holds[i].until = until
			return true
		}
	}
	return false
}

func (r *Relay) popHold(id requestID) (bool, proto.Time) {
	for i := 0; i < r.nHolds; i++ {
		if r.holds[i].id == id {
			until := r.holds[i].until
			copy(r.holds[i:], r.holds[i+1:r.nHolds])
			r.nHolds--
			return true, until
		}
	}
	return false, 0
}
