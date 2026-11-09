//go:build cairn_testfilter

package node

import (
	"cairnmesh/firmware/proto"
	"cairnmesh/firmware/radio"
	"cairnmesh/firmware/rxfilter"
)

// TEST BUILD ONLY (tag cairn_testfilter, make WLE5_TESTFILTER=1): a receive
// filter that drops frames by the transmitter (TX) so a bench of nodes in one
// room behaves like a chosen topology. See package rxfilter.

// rxHook is the filter's state in Stack.
type rxHook struct {
	f       rxfilter.Filter
	scratch [proto.MaxFrame]byte
}

// RxFilter is the stack's test filter, for the console command.
func (s *Stack) RxFilter() *rxfilter.Filter { return &s.rx.f }

// rxDrop reports whether the filter drops this frame. It runs after the
// frame's MIC verified (proto.OpenNetwork: pure, it moves no window) and before
// anything else in the stack sees the frame; a frame that fails the MIC is not
// judged (the stack rejects it as always). With the default matrix (pass all)
// it returns at once without touching the frame.
func (s *Stack) rxDrop(raw []byte) bool {
	f := &s.rx.f
	if !f.Active() {
		return false
	}
	if len(raw) < proto.Overhead || raw[0]&proto.FlagSecure == 0 {
		return false // not a sealed frame: left to the stack
	}
	if _, _, ok := proto.OpenNetwork(s.Sec.Network(), s.rx.scratch[:], raw); !ok {
		f.Unauthentic++
		return false
	}
	tx := proto.Transmitter(raw)
	if f.Allows(tx) {
		f.Passed++
		return false
	}
	d := rxfilter.Drop{
		Sender: raw[1], TX: tx, Kind: raw[0] &^ proto.FlagSecure,
		Fcnt: uint32(raw[4]) | uint32(raw[5])<<8 | uint32(raw[6])<<16 | uint32(raw[7])<<24,
	}
	copy(d.MIC[:], raw[len(raw)-proto.MICLen:])
	if l, ok := s.air.(radio.Levels); ok {
		d.RSSI, d.HasRSSI = l.PacketRSSI()
	}
	f.Record(d)
	return true
}
