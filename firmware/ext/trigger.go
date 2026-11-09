package ext

import (
	"cairnmesh/firmware/node"
	"cairnmesh/firmware/radio"
)

// EmitSignal sends a signal outside the protocol (node.Host.Emit).
type EmitSignal struct {
	Signal radio.Signal
	// Sent and Refused count the outcomes (refused: no emitter, the radio
	// busy with another signal, or settings it cannot do).
	Sent, Refused uint32
}

func (t *EmitSignal) Fire(h *node.Host, _ int32) {
	if h.Emit(&t.Signal) {
		t.Sent++
	} else {
		t.Refused++
	}
}

// NotifyEvent tells the controller that event ID (one the gateway defined)
// happened (node.Host.Notify, spec 19); no reading goes with it.
type NotifyEvent struct {
	ID byte
	// Sent and Refused count the outcomes (refused: the event queue is full).
	Sent, Refused uint32
}

func (t *NotifyEvent) Fire(h *node.Host, _ int32) {
	if h.Notify(t.ID) {
		t.Sent++
	} else {
		t.Refused++
	}
}

// RunAction performs a registered action locally (node.Host.Run): a watch
// can drive another module's output.
type RunAction struct {
	Code, Value byte
}

func (t *RunAction) Fire(h *node.Host, _ int32) { h.Run(t.Code, t.Value) }

// TriggerFunc is a trigger written in place. Build it at boot (a func value
// in an interface is allocated once).
type TriggerFunc func(h *node.Host, sample int32)

func (f TriggerFunc) Fire(h *node.Host, sample int32) { f(h, sample) }

// Triggers fires each of its triggers in order.
type Triggers []Trigger

func (ts Triggers) Fire(h *node.Host, sample int32) {
	for _, t := range ts {
		t.Fire(h, sample)
	}
}
