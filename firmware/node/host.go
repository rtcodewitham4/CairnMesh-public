package node

import (
	"cairnmesh/firmware/proto"
	"cairnmesh/firmware/radio"
)

// Host is a module's view of its node. Each module has its own (the Stack
// knows which module asks).
type Host struct {
	s     *Stack
	index int8
}

// Now is the time of the current Start, Tick or Poll.
func (h *Host) Now() proto.Time { return h.s.now }

// ID is the node's address.
func (h *Host) ID() byte { return h.s.device.ID }

// Rand is the node's random source (the true RNG on the board).
func (h *Host) Rand() proto.Rand { return h.s.rand }

// Handle makes a REQUEST with ACTION = code run a; only during Start.
// false (and the boot stops) when another module handles code already,
// when code is the protocol's (proto.QueryAction), or the table is full.
func (h *Host) Handle(code byte, a Action) bool {
	s := h.s
	t := &s.actions
	switch {
	case s.started:
		return false
	case code == proto.QueryAction:
		s.fail(int(h.index), ActionReserved, uint16(code))
		return false
	case t.find(code) != nil:
		s.fail(int(h.index), ActionTaken, uint16(code))
		return false
	case t.n >= MaxActions:
		s.fail(int(h.index), TooManyActions, uint16(code))
		return false
	}
	t.entries[t.n] = actionEntry{action: a, code: code, owner: h.index}
	t.n++
	return true
}

// Claim takes a pin or peripheral for this module; only during Start.
// false (and the boot stops) when another module holds it: two modules
// driving one pin is a build mistake, found at boot, not in the field.
func (h *Host) Claim(r Resource) bool {
	s := h.s
	if s.started {
		return false
	}
	for i := 0; i < s.nClaims; i++ {
		if s.claims[i].resource == r {
			if s.claims[i].owner == h.index {
				return true
			}
			s.fail(int(h.index), ResourceTaken, uint16(r))
			return false
		}
	}
	if s.nClaims >= MaxClaims {
		s.fail(int(h.index), TooManyClaims, uint16(r))
		return false
	}
	s.claims[s.nClaims] = claim{resource: r, owner: h.index}
	s.nClaims++
	return true
}

// CanEmit reports whether the radio can send signals outside the protocol.
func (h *Host) CanEmit() bool { return h.s.emitter != nil }

// Emit sends a signal outside the protocol now. false when the radio has
// no emitter, another signal still holds it, or it refused the signal.
// While the signal lasts the node sends no protocol frame (the protocol
// retries); keep signals short next to the controller's retry timeout, or
// commands to this node fail meanwhile.
func (h *Host) Emit(sig *radio.Signal) bool {
	s := h.s
	if s.emitter == nil || s.signaling(s.now) || !s.emitter.Emit(sig, s.now) {
		s.Stats.SignalsRefused++
		return false
	}
	s.signalOn = true
	s.Stats.Signals++
	return true
}

// Signaling reports whether a signal still holds the radio.
func (h *Host) Signaling() bool { return h.s.signaling(h.s.now) }

// Notify raises a module event for the controller (an EVENT frame, wire
// format v4, spec 19): eventID is one the gateway defined, no reading goes
// with it. Queued until acknowledged; an id already queued is merged; false
// when the queue (proto.MaxEvents) is full.
func (h *Host) Notify(eventID byte) bool {
	s := h.s
	if !s.Events.Notify(eventID, s.now) {
		s.Stats.NotifyDropped++
		return false
	}
	s.Stats.Notified++
	return true
}

// Ticket names the REQUEST a pending action will complete.
type Ticket struct {
	sender byte
	id     uint16
	valid  bool
}

// Ticket is the request being performed: call it inside Action.Perform,
// keep it, return proto.ResultPending, and Complete it later. Outside a
// REQUEST (Host.Run) the ticket is not valid and Complete refuses it.
func (h *Host) Ticket() Ticket { return h.s.actions.current }

// Complete gives the result (proto.ResultOK or proto.ResultError) of the
// pending action t: the answer or RESULT goes at the next Poll, without
// waiting for the deadline. false when the action is not waiting any more
// (its deadline passed and it was answered ERROR, a reboot cut it, it was
// completed already) or t is not valid: nothing changes.
func (h *Host) Complete(t Ticket, result byte) bool {
	if !t.valid {
		return false
	}
	return h.s.Dev.Complete(t.sender, t.id, result, h.s.now)
}

// Run performs a registered action locally, as a REQUEST would but without
// the protocol (no record, no answer): a trigger may run another module's
// action. ok is false when no module handles code.
func (h *Host) Run(code, value byte) (result byte, ok bool) {
	e := h.s.actions.find(code)
	if e == nil {
		return proto.ResultError, false
	}
	return e.action.Perform(value), true
}
