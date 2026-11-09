// Package benchactions contains the node actions used only by bench tools.
// Production firmware must not import it.
package benchactions

import (
	"cairnmesh/firmware/node"
	"cairnmesh/firmware/proto"
)

// Actions registers the bench's 0x02-0x05 demonstration actions.
type Actions struct {
	late later
}

type slow struct{}

func (slow) Duration(value byte) proto.Time { return proto.Time(value) * 100 * proto.Millisecond }
func (slow) Perform(byte) byte              { return proto.ResultOK }

type later struct {
	h      *node.Host
	ticket node.Ticket
	at     proto.Time
}

func (a *Actions) Start(h *node.Host) bool {
	a.late.h, a.late.at = h, proto.Never
	return h.Handle(0x02, slow{}) &&
		h.Handle(0x03, node.Func(func(byte) byte { return proto.ResultError })) &&
		h.Handle(0x04, node.Func(func(byte) byte { return proto.ResultOK })) &&
		h.Handle(0x05, &a.late)
}

func (a *Actions) NextDue() proto.Time { return a.late.at }

func (a *Actions) Tick(proto.Time) {
	a.late.at = proto.Never
	a.late.h.Complete(a.late.ticket, proto.ResultOK)
}

func (l *later) Duration(byte) proto.Time { return 5 * proto.Second }

func (l *later) Perform(value byte) byte {
	if l.at != proto.Never {
		return proto.ResultError
	}
	l.ticket = l.h.Ticket()
	l.at = l.h.Now() + proto.Time(value)*100*proto.Millisecond
	return proto.ResultPending
}
