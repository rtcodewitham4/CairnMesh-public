package ext

import (
	"sync/atomic"

	"cairnmesh/firmware/node"
	"cairnmesh/firmware/proto"
)

// Latch is a Source fed by an interrupt: the handler calls Set, the Watch
// samples the number of Sets since its last sample (use Nonzero). Set is
// safe in interrupt context; the Watch samples at once while one is pending.
type Latch struct {
	count atomic.Uint32
	seen  uint32
}

// Set records one event; call it from the interrupt handler.
func (l *Latch) Set() { l.count.Add(1) }

func (l *Latch) Pending() bool { return l.count.Load() != l.seen }

func (l *Latch) Sample() int32 {
	now := l.count.Load()
	n := now - l.seen
	l.seen = now
	return int32(n)
}

// ByteStream is a serial interface read without blocking (machine.UART has
// these methods).
type ByteStream interface {
	Buffered() int
	ReadByte() (byte, error)
}

// MaxPattern is the longest pattern a StreamWatch looks for.
const MaxPattern = 32

// StreamWatch is a module that reads a serial interface and fires Trigger
// each time Pattern has come in (at most once per Cooldown). The bytes are
// consumed: the interface belongs to this module (claim it in Claims).
type StreamWatch struct {
	Stream  ByteStream
	Pattern []byte
	Trigger Trigger
	// Cooldown suppresses a match within this time of the last firing.
	Cooldown proto.Time
	Claims   []node.Resource
	// MaxPerTick bounds the bytes read in one Tick (default 64), so a
	// talkative line cannot hold the protocol up.
	MaxPerTick int

	// Bytes read, Matches seen, Fired; Suppressed by the cooldown.
	Bytes, Matches, Fired, Suppressed uint32

	h        *node.Host
	window   [MaxPattern]byte
	filled   int
	lastFire proto.Time
	fired    bool
}

func (w *StreamWatch) Start(h *node.Host) bool {
	w.h = h
	for _, r := range w.Claims {
		if !h.Claim(r) {
			return false
		}
	}
	if w.MaxPerTick <= 0 {
		w.MaxPerTick = 64
	}
	return w.Stream != nil && w.Trigger != nil && len(w.Pattern) > 0 && len(w.Pattern) <= MaxPattern
}

func (w *StreamWatch) NextDue() proto.Time {
	if w.Stream.Buffered() > 0 {
		return 0
	}
	return proto.Never
}

func (w *StreamWatch) Tick(now proto.Time) {
	m := len(w.Pattern)
	for i := 0; i < w.MaxPerTick && w.Stream.Buffered() > 0; i++ {
		b, err := w.Stream.ReadByte()
		if err != nil {
			return
		}
		w.Bytes++
		// The last m bytes, oldest first: a match may overlap the previous one.
		if w.filled < m {
			w.window[w.filled] = b
			w.filled++
		} else {
			copy(w.window[:m-1], w.window[1:m])
			w.window[m-1] = b
		}
		if w.filled < m || string(w.window[:m]) != string(w.Pattern) {
			continue
		}
		w.Matches++
		if w.fired && now-w.lastFire < w.Cooldown {
			w.Suppressed++
			continue
		}
		w.fired, w.lastFire = true, now
		w.Fired++
		w.Trigger.Fire(w.h, int32(w.Matches))
	}
}

// Output is a digital output (machine.Pin has Set).
type Output interface {
	Set(on bool)
}

// Switch is a module that lets the controller set an output: ACTION Code,
// VALUE 0 off, anything else on. Pulse instead makes VALUE a duration of
// VALUE × PulseUnit: on, then off when it is over (the request answers
// once the pulse ended, ACCEPTED first when it is long).
type Switch struct {
	Out   Output
	Code  byte
	Claim node.Resource
	// PulseUnit > 0 turns the switch into a pulse output.
	PulseUnit proto.Time
	// On is the current state.
	On bool

	h   *node.Host
	off proto.Time
}

func (s *Switch) Start(h *node.Host) bool {
	s.h = h
	s.off = proto.Never
	return s.Out != nil && h.Claim(s.Claim) && h.Handle(s.Code, s)
}

// Duration and Perform make the Switch its own node.Action.
func (s *Switch) Duration(value byte) proto.Time {
	if s.PulseUnit > 0 {
		return proto.Time(value) * s.PulseUnit
	}
	return 0
}

func (s *Switch) Perform(value byte) byte {
	if s.PulseUnit > 0 {
		s.set(value != 0)
		s.off = s.h.Now() + proto.Time(value)*s.PulseUnit
		return proto.ResultOK
	}
	s.set(value != 0)
	return proto.ResultOK
}

func (s *Switch) set(on bool) {
	s.On = on
	s.Out.Set(on)
}

func (s *Switch) NextDue() proto.Time { return s.off }

func (s *Switch) Tick(now proto.Time) {
	s.off = proto.Never
	s.set(false)
}
