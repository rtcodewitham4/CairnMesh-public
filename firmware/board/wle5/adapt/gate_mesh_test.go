package adapt

import (
	"testing"

	"cairnmesh/firmware/mesh"
	"cairnmesh/firmware/proto"
)

// meshAir is a radio with channel activity detection and retuning.
type meshAir struct {
	fakeAir
	until         proto.Time
	channel, sf   uint8
	tuned, probed int
}

func (a *meshAir) BusyUntil(now proto.Time) (proto.Time, bool) {
	a.probed++
	return a.until, now < a.until
}

func (a *meshAir) SetRadio(channel, sf uint8) { a.channel, a.sf, a.tuned = channel, sf, a.tuned+1 }

// The stack finds listen-before-talk and retuning by type assertion (node
// meshPort, mesh.Relay); the gate must answer them for the radio under it.
func TestGatedAirForwardsBusyAndTuner(t *testing.T) {
	var _ mesh.Busy = &GatedAir{}
	var _ mesh.Tuner = &GatedAir{}
	for _, allow := range []bool{true, false} { // sensing and tuning transmit nothing
		air := &meshAir{until: 5 * proto.Second}
		g := &GatedAir{Air: air, Allow: allow}
		if until, ok := g.BusyUntil(proto.Second); !ok || until != 5*proto.Second {
			t.Errorf("allow %v: BusyUntil = %d, %v", allow, until, ok)
		}
		if _, ok := g.BusyUntil(6 * proto.Second); ok {
			t.Errorf("allow %v: busy after the channel cleared", allow)
		}
		g.SetRadio(3, 9)
		if air.channel != 3 || air.sf != 9 || air.tuned != 1 {
			t.Errorf("allow %v: tuned to %d / %d (%d times)", allow, air.channel, air.sf, air.tuned)
		}
		if len(air.sent) != 0 {
			t.Errorf("allow %v: tuning sent a packet", allow)
		}
	}
}

// A radio that has neither: nothing is made up. BusyUntil says "clear" (what
// the stack assumes of an air that is no mesh.Busy) and SetRadio does nothing.
func TestGatedAirWithoutBusyOrTunerSaysNothingMadeUp(t *testing.T) {
	g := &GatedAir{Air: &plainAir{}, Allow: true}
	if until, ok := g.BusyUntil(proto.Second); ok || until != 0 {
		t.Errorf("BusyUntil = %d, %v on a radio without it", until, ok)
	}
	g.SetRadio(3, 9) // must not panic
}
