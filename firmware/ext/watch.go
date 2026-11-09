// Package ext holds ready-made modules (node.Module) and the parts they are
// built from, so that a special node is assembled rather than written:
//
//	Watch        sample an input (pin, ADC channel, interrupt latch), detect
//	             a condition, fire a trigger
//	StreamWatch  watch a serial interface for a byte pattern
//	Switch       a digital output the controller sets through an action
//
// Inputs come in through small interfaces (Source, ByteStream, Output), so
// the same module runs on the board (package board adapts the hardware) and
// in host tests (fakes). Triggers say what a firing does: send a signal
// outside the protocol, notify the controller, run an action, or anything
// written as a TriggerFunc. Pure Go, no allocation after Start.
package ext

import (
	"cairnmesh/firmware/node"
	"cairnmesh/firmware/proto"
)

// Source is an input a Watch samples: a pin level (0 or 1), an ADC reading,
// a count of interrupts.
type Source interface {
	Sample() int32
}

// Pending is implemented by a Source fed by an interrupt (Latch): while it
// reports true the Watch samples at once instead of waiting for its period.
type Pending interface {
	Pending() bool
}

// Trigger is what a firing does; sample is the reading that fired it.
type Trigger interface {
	Fire(h *node.Host, sample int32)
}

// Watch is a module that samples Source every Period, feeds Detect, and
// fires Trigger when the condition begins, at most once per Cooldown.
// Fill it in before Stack.Add; it must not be copied afterwards.
type Watch struct {
	Source  Source
	Detect  Detector
	Trigger Trigger
	Period  proto.Time
	// Cooldown: a firing within this time of the last one is suppressed
	// (counted, not fired), so a chattering input cannot flood the air.
	Cooldown proto.Time
	// Claims: the pins and peripherals Source uses.
	Claims []node.Resource
	// Controlled registers ArmAction: VALUE 0 disarms the watch, any other
	// value arms it (and restarts detection). Disarmed: it starts disarmed.
	Controlled bool
	ArmAction  byte
	Disarmed   bool

	// Last is the latest sample; Fired and Suppressed count firings.
	Last              int32
	Fired, Suppressed uint32

	h        *node.Host
	next     proto.Time
	lastFire proto.Time
	fired    bool
}

func (w *Watch) Start(h *node.Host) bool {
	w.h = h
	for _, r := range w.Claims {
		if !h.Claim(r) {
			return false
		}
	}
	if w.Controlled && !h.Handle(w.ArmAction, node.Func(w.arm)) {
		return false
	}
	if w.Period <= 0 {
		w.Period = 100 * proto.Millisecond
	}
	w.next = h.Now()
	return w.Source != nil && w.Detect != nil && w.Trigger != nil
}

func (w *Watch) arm(value byte) byte {
	w.Disarmed = value == 0
	if !w.Disarmed {
		// Events that came while disarmed are not news: drop them.
		if p, ok := w.Source.(Pending); ok && p.Pending() {
			w.Source.Sample()
		}
		w.Detect.Reset()
		w.next = w.h.Now()
	}
	return proto.ResultOK
}

func (w *Watch) NextDue() proto.Time {
	if w.Disarmed {
		return proto.Never
	}
	if p, ok := w.Source.(Pending); ok && p.Pending() {
		return 0
	}
	return w.next
}

func (w *Watch) Tick(now proto.Time) {
	w.Last = w.Source.Sample()
	w.next = now + w.Period
	if !w.Detect.Detect(w.Last, now) {
		return
	}
	if w.fired && now-w.lastFire < w.Cooldown {
		w.Suppressed++
		return
	}
	w.fired, w.lastFire = true, now
	w.Fired++
	w.Trigger.Fire(w.h, w.Last)
}
