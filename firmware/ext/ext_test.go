package ext_test

import (
	"testing"

	"cairnmesh/firmware/ext"
	"cairnmesh/firmware/node"
	"cairnmesh/firmware/node/nodetest"
	"cairnmesh/firmware/proto"
	"cairnmesh/firmware/radio"
)

const ms = proto.Millisecond

// value is a Source the test sets.
type value struct{ v int32 }

func (s *value) Sample() int32 { return s.v }

// stream is a ByteStream fed by the test.
type stream struct{ data []byte }

func (s *stream) Buffered() int { return len(s.data) }
func (s *stream) ReadByte() (byte, error) {
	b := s.data[0]
	s.data = s.data[1:]
	return b, nil
}

// pin is an Output.
type pin struct{ on bool }

func (p *pin) Set(on bool) { p.on = on }

func TestEdgeDebounce(t *testing.T) {
	e := &ext.Edge{Rising: true, Debounce: 30 * ms}
	steps := []struct {
		at   proto.Time
		v    int32
		fire bool
	}{
		{0, 1, false},   // baseline high: no edge
		{10, 0, false},  // falling, not asked for
		{50, 0, false},  // stable low now
		{60, 1, false},  // rising starts
		{70, 0, false},  // a bounce: back low
		{80, 1, false},  // rising again, from 80
		{100, 1, false}, // 20 ms
		{110, 1, true},  // 30 ms held: fires
		{150, 1, false}, // once
	}
	for _, s := range steps {
		if got := e.Detect(s.v, s.at*ms); got != s.fire {
			t.Errorf("at %d ms sample %d: fired %v", s.at, s.v, got)
		}
	}
}

func TestThresholdHysteresis(t *testing.T) {
	th := &ext.Threshold{Level: 100, Hysteresis: 10}
	want := map[int32]bool{90: false, 100: true, 120: false, 95: false, 89: false, 101: true}
	for _, v := range []int32{90, 100, 120, 95, 89, 101} {
		if got := th.Detect(v, 0); got != want[v] {
			t.Errorf("sample %d: fired %v", v, got)
		}
	}
	low := &ext.Threshold{Level: 1000, Hysteresis: 50, Below: true}
	if !low.Detect(900, 0) || low.Detect(1040, 0) || !(!low.Detect(1060, 0) && low.Detect(990, 0)) {
		t.Error("Below threshold")
	}
}

func TestChange(t *testing.T) {
	c := &ext.Change{Delta: 16}
	got := []bool{}
	for _, v := range []int32{2000, 2010, 1990, 2016, 2020, 1999} {
		got = append(got, c.Detect(v, 0))
	}
	want := []bool{true, false, false, true, false, true}
	for i := range want {
		if got[i] != want[i] {
			t.Fatalf("got %v, want %v", got, want)
		}
	}
}

// An alarm input: a threshold crossing sends a signal outside the protocol
// and notifies the controller; the cooldown stops a chattering input.
func TestWatchFiresSignalAndNotify(t *testing.T) {
	in := &value{v: 10}
	emit := &ext.EmitSignal{Signal: radio.Signal{Kind: radio.Carrier, PowerDBm: radio.MaxPower, Duration: 200 * ms}}
	notify := &ext.NotifyEvent{ID: 7}
	w := &ext.Watch{
		Source:   in,
		Detect:   &ext.Threshold{Level: 100, Hysteresis: 20},
		Trigger:  ext.Triggers{emit, notify},
		Period:   50 * ms,
		Cooldown: 1 * proto.Second,
		Claims:   []node.Resource{node.ADC},
	}
	bench := nodetest.New(nodetest.Options{}, w)
	if !bench.Started {
		t.Fatalf("start: %+v", bench.Stack.Fault)
	}
	bench.Advance(200*ms, 10*ms)
	in.v = 0x7F0 // past the level
	bench.Advance(100*ms, 10*ms)
	if w.Fired != 1 || emit.Sent != 1 || len(bench.Emitter.Signals) != 1 {
		t.Fatalf("fired %d, sent %d", w.Fired, emit.Sent)
	}
	if notify.Sent != 1 || bench.Stack.Events.Pending() != 1 {
		t.Errorf("notify sent %d, pending %d", notify.Sent, bench.Stack.Events.Pending())
	}

	// Chatter: down and up again within the cooldown.
	in.v = 0
	bench.Advance(100*ms, 10*ms)
	in.v = 0x7F0
	bench.Advance(100*ms, 10*ms)
	if w.Fired != 1 || w.Suppressed != 1 {
		t.Errorf("chatter: fired %d, suppressed %d", w.Fired, w.Suppressed)
	}
	// After the cooldown it fires again.
	in.v = 0
	bench.Advance(1*proto.Second, 10*ms)
	in.v = 0x7F0
	bench.Advance(100*ms, 10*ms)
	if w.Fired != 2 {
		t.Errorf("after the cooldown: fired %d", w.Fired)
	}
	// The event went out once its delay was over; raised again before the
	// controller acknowledged it, it is merged into the one queued.
	if ev := nodetest.Events(bench.Sent()); len(ev) != 1 || ev[0][5] != 7 {
		t.Errorf("events on air %x", ev)
	}
	if notify.Sent != 2 || bench.Stack.Events.Pending() != 1 || bench.Stack.Events.Stats.Merged != 1 {
		t.Errorf("notify sent %d, pending %d", notify.Sent, bench.Stack.Events.Pending())
	}
}

// The controller arms and disarms a watch with an action.
func TestWatchArmAction(t *testing.T) {
	in := &value{}
	runs := 0
	w := &ext.Watch{
		Source: in, Detect: &ext.Edge{Rising: true}, Period: 20 * ms,
		Trigger:    ext.TriggerFunc(func(*node.Host, int32) { runs++ }),
		Controlled: true, ArmAction: 0x40, Disarmed: true,
	}
	bench := nodetest.New(nodetest.Options{}, w)
	in.v = 1
	bench.Advance(100*ms, 10*ms)
	if runs != 0 || bench.Stack.NextDue() != proto.Never {
		t.Fatal("a disarmed watch ran")
	}
	seq := bench.Request(0x40, 1)
	if nodetest.Answer(bench.Sent(), seq) != proto.ResultOK {
		t.Fatal("arm not answered OK")
	}
	in.v = 0
	bench.Advance(50*ms, 10*ms)
	in.v = 1
	bench.Advance(50*ms, 10*ms)
	if runs != 1 {
		t.Fatalf("armed: %d runs", runs)
	}
	bench.Request(0x40, 0)
	in.v = 0
	bench.Advance(50*ms, 10*ms)
	in.v = 1
	bench.Advance(50*ms, 10*ms)
	if runs != 1 {
		t.Errorf("disarmed again: %d runs", runs)
	}
}

// A latch set by an interrupt is seen at the next poll, not the next period.
func TestLatchIsSeenAtOnce(t *testing.T) {
	var latch ext.Latch
	var at []proto.Time
	var counts []int32
	w := &ext.Watch{
		Source: &latch, Detect: ext.Nonzero{}, Period: 10 * proto.Second,
		Trigger: ext.TriggerFunc(func(h *node.Host, n int32) {
			at = append(at, h.Now())
			counts = append(counts, n)
		}),
	}
	bench := nodetest.New(nodetest.Options{}, w)
	bench.Advance(100*ms, 10*ms) // the first sample, at 0
	latch.Set()
	latch.Set()
	if bench.Stack.NextDue() != 0 {
		t.Error("a pending latch should make the node due at once")
	}
	bench.Advance(10*ms, 10*ms)
	if len(at) != 1 || at[0] != 110*ms || counts[0] != 2 {
		t.Errorf("fired at %v with %v", at, counts)
	}
}

// Interrupts while a watch is disarmed do not fire it when it is armed again.
func TestLatchWhileDisarmed(t *testing.T) {
	var latch ext.Latch
	runs := 0
	w := &ext.Watch{
		Source: &latch, Detect: ext.Nonzero{}, Period: 1 * proto.Second,
		Trigger:    ext.TriggerFunc(func(*node.Host, int32) { runs++ }),
		Controlled: true, ArmAction: 0x10, Disarmed: true,
	}
	bench := nodetest.New(nodetest.Options{}, w)
	latch.Set()
	bench.Advance(50*ms, 10*ms)
	bench.Request(0x10, 1)
	bench.Advance(50*ms, 10*ms)
	if runs != 0 {
		t.Fatalf("a stale interrupt fired %d times", runs)
	}
	latch.Set()
	bench.Advance(20*ms, 10*ms)
	if runs != 1 {
		t.Errorf("a new interrupt fired %d times", runs)
	}
}

func TestStreamWatch(t *testing.T) {
	in := &stream{}
	var matches []int32
	w := &ext.StreamWatch{
		Stream: in, Pattern: []byte("aab"), MaxPerTick: 4,
		Trigger: ext.TriggerFunc(func(_ *node.Host, n int32) { matches = append(matches, n) }),
		Claims:  []node.Resource{node.UART1},
	}
	bench := nodetest.New(nodetest.Options{}, w)
	in.data = []byte("xaaab--aabaab")
	bench.Stack.Poll(0) // 4 bytes per tick: the rest waits
	if w.Bytes != 4 {
		t.Errorf("read %d bytes in one tick", w.Bytes)
	}
	bench.Advance(50*ms, 10*ms)
	if w.Matches != 3 || len(matches) != 3 || in.Buffered() != 0 {
		t.Errorf("matches %d (%v), left %d", w.Matches, matches, in.Buffered())
	}
}

// The controller pulses an output: on, ACCEPTED for a long pulse, off when
// it is over, then the RESULT.
func TestSwitchPulse(t *testing.T) {
	out := &pin{}
	sw := &ext.Switch{Out: out, Code: 2, Claim: node.Pin(5), PulseUnit: 100 * ms}
	level := &pin{}
	plain := &ext.Switch{Out: level, Code: 1, Claim: node.Pin(6)}
	bench := nodetest.New(nodetest.Options{}, sw, plain)
	if !bench.Started {
		t.Fatalf("start: %+v", bench.Stack.Fault)
	}
	seq := bench.Request(1, 1)
	if !level.on || nodetest.Answer(bench.Sent(), seq) != proto.ResultOK {
		t.Fatal("switch on")
	}
	seq = bench.Request(2, 20) // 2 s: past the answer budget
	if !out.on || nodetest.Answer(bench.Sent(), seq) != proto.ResultAccepted {
		t.Fatal("pulse start")
	}
	bench.Advance(1900*ms, 10*ms)
	if !out.on {
		t.Fatal("pulse ended early")
	}
	bench.Advance(200*ms, 10*ms)
	if out.on {
		t.Fatal("pulse did not end")
	}
	var result []byte
	for _, f := range bench.Sent() {
		if f[0] == proto.KindResult {
			result = f
		}
	}
	if result == nil || result[3] != seq || result[5] != proto.ResultOK {
		t.Errorf("RESULT %v", result)
	}
	// Two switches on one pin: a boot fault, not a fight in the field.
	clash := nodetest.New(nodetest.Options{}, &ext.Switch{Out: out, Code: 1, Claim: node.Pin(5)},
		&ext.Switch{Out: out, Code: 2, Claim: node.Pin(5)})
	if clash.Started || clash.Stack.Fault.Reason != node.ResourceTaken {
		t.Errorf("clash: %+v", clash.Stack.Fault)
	}
}
