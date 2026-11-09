package adapt

import (
	"cairnmesh/firmware/mesh"
	"cairnmesh/firmware/proto"
	"cairnmesh/firmware/radio"
)

// GatedAir is the node's radio.Air with the transmit interlock on it: with
// Allow false (no WLE5_ANT_OK=1 in the build: no confirmed antenna or 50 ohm
// load, board/wle5/safety) nothing is ever handed to the radio to send, the
// node only listens; the protocol's retries see "could not go out". Receive,
// the signal level and the quiet check pass through. It also forwards the
// optional radio.LongPreamble / Levels / Quiet of what it wraps, so the stack
// finds them on the gate (and asks the wrapped radio's Receiving and
// Transmitting when it has no Quiet of its own, as the SX1262 driver).
type GatedAir struct {
	Air   radio.Air
	Allow bool
	// Refused counts packets not sent because the gate is closed.
	Refused uint32
}

func (g *GatedAir) Send(packet []byte) bool {
	if !g.Allow {
		g.Refused++
		return false
	}
	return g.Air.Send(packet)
}

func (g *GatedAir) Receive() []byte { return g.Air.Receive() }

// SendPreamble is radio.LongPreamble; a radio without it sends with its own
// preamble.
func (g *GatedAir) SendPreamble(packet []byte, symbols uint16) bool {
	if !g.Allow {
		g.Refused++
		return false
	}
	if lp, ok := g.Air.(radio.LongPreamble); ok {
		return lp.SendPreamble(packet, symbols)
	}
	return g.Air.Send(packet)
}

// PacketRSSI is radio.Levels.
func (g *GatedAir) PacketRSSI() (int16, bool) {
	if l, ok := g.Air.(radio.Levels); ok {
		return l.PacketRSSI()
	}
	return 0, false
}

// BusyUntil is mesh.Busy: the wrapped radio's channel activity, so listen
// before talk reaches the stack through the gate. A radio without it gives
// "clear" (ok false), exactly what the stack assumes of an air that is not a
// mesh.Busy: nothing is made up.
func (g *GatedAir) BusyUntil(now proto.Time) (proto.Time, bool) {
	if b, ok := g.Air.(mesh.Busy); ok {
		return b.BusyUntil(now)
	}
	return 0, false
}

// SetRadio is mesh.Tuner: a channel / SF change passes to the wrapped radio
// (tuning transmits nothing, so the gate has no say). A radio that cannot
// retune ignores it, as the stack does for an air that is not a mesh.Tuner.
func (g *GatedAir) SetRadio(channel, sf uint8) {
	if t, ok := g.Air.(mesh.Tuner); ok {
		t.SetRadio(channel, sf)
	}
}

// Quiet is radio.Quiet: no packet coming in or going out. A radio that cannot
// tell is never taken for quiet.
func (g *GatedAir) Quiet() bool {
	if q, ok := g.Air.(radio.Quiet); ok {
		return q.Quiet()
	}
	if s, ok := g.Air.(interface {
		Receiving() bool
		Transmitting() bool
	}); ok {
		return !s.Receiving() && !s.Transmitting()
	}
	return false
}

// GatedEmitter is the radio.Emitter behind the same interlock: with Allow
// false no signal (carrier or packet outside the protocol) starts.
type GatedEmitter struct {
	E       radio.Emitter
	Allow   bool
	Refused uint32
}

func (g *GatedEmitter) Emit(s *radio.Signal, now proto.Time) bool {
	if !g.Allow || g.E == nil {
		g.Refused++
		return false
	}
	return g.E.Emit(s, now)
}

func (g *GatedEmitter) Busy(now proto.Time) bool {
	return g.Allow && g.E != nil && g.E.Busy(now)
}
