package node_test

import (
	"testing"

	"cairnmesh/firmware/journal"
	"cairnmesh/firmware/node"
	"cairnmesh/firmware/node/nodetest"
	"cairnmesh/firmware/proto"
	"cairnmesh/firmware/radio"
)

// probe is a module that records how the Stack drives it.
type probe struct {
	code    byte
	claims  []node.Resource
	fail    bool
	every   proto.Time
	h       *node.Host
	values  []byte
	ticks   int
	next    proto.Time
	signal  radio.Signal
	emitted []bool
}

func (p *probe) Start(h *node.Host) bool {
	p.h = h
	for _, r := range p.claims {
		if !h.Claim(r) {
			return false
		}
	}
	if !h.Handle(p.code, node.Func(func(v byte) byte {
		p.values = append(p.values, v)
		if v == 0xEE {
			return proto.ResultError
		}
		return proto.ResultOK
	})) {
		return false
	}
	p.next = proto.Never
	if p.every > 0 {
		p.next = p.every
	}
	return !p.fail
}

func (p *probe) NextDue() proto.Time { return p.next }

func (p *probe) Tick(now proto.Time) {
	p.ticks++
	p.next = now + p.every
	if p.signal.Kind != 0 {
		p.emitted = append(p.emitted, p.h.Emit(&p.signal))
	}
}

func TestActionsReachTheirModule(t *testing.T) {
	a, b := &probe{code: 1}, &probe{code: 7}
	bench := nodetest.New(nodetest.Options{}, a, b)
	if !bench.Started {
		t.Fatalf("start failed: %+v", bench.Stack.Fault)
	}
	s1 := bench.Request(1, 5)
	s2 := bench.Request(7, 0xEE)
	s3 := bench.Request(9, 0) // nobody handles 9
	sent := bench.Sent()
	if got := nodetest.Answer(sent, s1); got != proto.ResultOK {
		t.Errorf("action 1 answered %d", got)
	}
	if got := nodetest.Answer(sent, s2); got != proto.ResultError {
		t.Errorf("action 7 answered %d", got)
	}
	if got := nodetest.Answer(sent, s3); got != proto.ResultError {
		t.Errorf("unknown action answered %d", got)
	}
	if len(a.values) != 1 || a.values[0] != 5 || len(b.values) != 1 || b.values[0] != 0xEE {
		t.Errorf("values: %v %v", a.values, b.values)
	}
	if bench.Stack.Stats.UnknownAction != 1 {
		t.Errorf("UnknownAction = %d", bench.Stack.Stats.UnknownAction)
	}
}

func TestRetryRunsOnce(t *testing.T) {
	a := &probe{code: 1}
	bench := nodetest.New(nodetest.Options{}, a)
	// The same REQUEST again (a retry): sealed anew, so a new FCNT, same message ID.
	seq := bench.Request(1, 3)
	frame := []byte{proto.KindRequest, proto.DefaultControllerID, nodetest.NodeID, seq, 0, 1, 3}
	var ctrl proto.Secure
	var c nodetest.Cipher
	ctrl.Init(&c, &nodetest.NetworkKey, 1000, nil)
	ctrl.App = true
	ctrl.Keys.Add(nodetest.NodeID, 0, &nodetest.AppKey)
	var buf [proto.MaxFrame]byte
	n, _ := ctrl.SealFrame(buf[:], frame)
	bench.Air.In = append(bench.Air.In, buf[:n])
	bench.Stack.Poll(0)
	if len(a.values) != 1 {
		t.Fatalf("the action ran %d times", len(a.values))
	}
	if got := nodetest.Answer(bench.Sent(), seq); got != proto.ResultOK {
		t.Errorf("the retry was answered %d", got)
	}
}

func TestBootFaults(t *testing.T) {
	cases := []struct {
		name    string
		modules []node.Module
		want    node.Fault
	}{
		{"same action", []node.Module{&probe{code: 1}, &probe{code: 1}},
			node.Fault{Module: 1, Reason: node.ActionTaken, Detail: 1}},
		{"query action", []node.Module{&probe{code: proto.QueryAction}},
			node.Fault{Module: 0, Reason: node.ActionReserved, Detail: proto.QueryAction}},
		{"same pin", []node.Module{&probe{code: 1, claims: []node.Resource{node.Pin(5)}},
			&probe{code: 2, claims: []node.Resource{node.ADC, node.Pin(5)}}},
			node.Fault{Module: 1, Reason: node.ResourceTaken, Detail: 5}},
		{"module refuses", []node.Module{&probe{code: 1}, &probe{code: 2, fail: true}},
			node.Fault{Module: 1, Reason: node.ModuleFailed}},
	}
	for _, c := range cases {
		bench := nodetest.New(nodetest.Options{}, c.modules...)
		if bench.Started || bench.Stack.Fault != c.want {
			t.Errorf("%s: started %v, fault %+v, want %+v", c.name, bench.Started, bench.Stack.Fault, c.want)
		}
		// A node that did not start ignores the air.
		bench.Request(1, 1)
		if len(bench.Air.Out) != 0 {
			t.Errorf("%s: a node that failed to start answered", c.name)
		}
	}
	ok := nodetest.New(nodetest.Options{}, &probe{code: 1, claims: []node.Resource{node.Pin(5), node.Pin(5)}})
	if !ok.Started {
		t.Errorf("claiming a pin twice from one module: %+v", ok.Stack.Fault)
	}
}

func TestModuleTimers(t *testing.T) {
	p := &probe{code: 1, every: 250 * proto.Millisecond}
	bench := nodetest.New(nodetest.Options{}, p)
	if due := bench.Stack.NextDue(); due != 250*proto.Millisecond {
		t.Errorf("NextDue = %d", due)
	}
	bench.Advance(1*proto.Second, 10*proto.Millisecond)
	if p.ticks != 4 {
		t.Errorf("%d ticks in 1 s at 250 ms", p.ticks)
	}
}

func TestSignalHoldsTheRadio(t *testing.T) {
	p := &probe{code: 1, every: 1 * proto.Second,
		signal: radio.Signal{Kind: radio.Carrier, PowerDBm: radio.MaxPower, Duration: 1500 * proto.Millisecond}}
	bench := nodetest.New(nodetest.Options{Heartbeat: 200 * proto.Millisecond}, p)
	bench.Advance(1*proto.Second, 10*proto.Millisecond) // first signal at 1 s, holds until 2.5 s
	bench.Sent()
	held := bench.Stack.Stats.HeldBySignal
	bench.Advance(1*proto.Second, 10*proto.Millisecond) // 2 s: the second one is refused
	if len(bench.Sent()) != 0 {
		t.Error("a frame went out while the signal held the radio")
	}
	if bench.Stack.Stats.HeldBySignal == held {
		t.Error("no heartbeat was held back")
	}
	if len(p.emitted) != 2 || !p.emitted[0] || p.emitted[1] {
		t.Errorf("emitted %v, want [true false]", p.emitted)
	}
	bench.Advance(600*proto.Millisecond, 10*proto.Millisecond) // 2.6 s, past 2.5 s: frames again
	if len(bench.Sent()) == 0 {
		t.Error("no frame after the signal ended")
	}
	if len(bench.Emitter.Signals) != 1 {
		t.Errorf("%d signals reached the radio", len(bench.Emitter.Signals))
	}
	// Commands still run while a signal holds the radio; only the answer waits for a retry.
	bench.Advance(500*proto.Millisecond, 10*proto.Millisecond) // 3.1 s: a new signal since 3 s
	bench.Sent()
	seq := bench.Request(1, 9)
	if p.values[len(p.values)-1] != 9 || nodetest.Answer(bench.Sent(), seq) != -1 {
		t.Error("during a signal: the action should run and its answer be held")
	}
}

func TestNoEmitter(t *testing.T) {
	p := &probe{code: 1}
	bench := nodetest.New(nodetest.Options{NoEmitter: true}, p)
	if p.h.CanEmit() || p.h.Emit(&radio.Signal{Kind: radio.Carrier}) {
		t.Error("emit without an emitter")
	}
	if s := bench.Stack.Stats; s.SignalsRefused != 1 {
		t.Errorf("stats %+v", s)
	}
	if res, ok := p.h.Run(1, 4); !ok || res != proto.ResultOK || p.values[0] != 4 {
		t.Error("Run did not reach the action")
	}
	if _, ok := p.h.Run(2, 0); ok {
		t.Error("Run of an unknown action")
	}
}

// later is an action whose result is known only later: it completes VALUE ×
// 100 ms after it starts, with a 5 s deadline.
type later struct {
	h      *node.Host
	ticket node.Ticket
	at     proto.Time
	done   []bool
}

func (l *later) Start(h *node.Host) bool {
	l.h, l.at = h, proto.Never
	return h.Handle(5, l)
}
func (l *later) Duration(byte) proto.Time { return 5 * proto.Second }
func (l *later) Perform(value byte) byte {
	l.ticket = l.h.Ticket()
	l.at = l.h.Now() + proto.Time(value)*100*proto.Millisecond
	return proto.ResultPending
}
func (l *later) NextDue() proto.Time { return l.at }
func (l *later) Tick(proto.Time) {
	l.at = proto.Never
	l.done = append(l.done, l.h.Complete(l.ticket, proto.ResultOK))
}

func resultOf(frames [][]byte, seq byte) int {
	for _, f := range frames {
		if f[0] == proto.KindResult && f[3] == seq {
			return int(f[5])
		}
	}
	return -1
}

func TestPendingActionCompletesThroughTheHost(t *testing.T) {
	l := &later{}
	bench := nodetest.New(nodetest.Options{}, l)
	seq := bench.Request(5, 20) // done after 2 s, deadline 5 s
	if nodetest.Answer(bench.Sent(), seq) != proto.ResultAccepted {
		t.Fatal("not ACCEPTED")
	}
	bench.Advance(1990*proto.Millisecond, 10*proto.Millisecond)
	if len(bench.Sent()) != 0 {
		t.Fatal("RESULT before the action completed")
	}
	bench.Advance(20*proto.Millisecond, 10*proto.Millisecond)
	if resultOf(bench.Sent(), seq) != proto.ResultOK {
		t.Fatal("no RESULT OK right after the completion")
	}

	seq = bench.Request(5, 80) // would complete after 8 s: past the deadline
	bench.Sent()
	bench.Advance(5*proto.Second, 10*proto.Millisecond)
	if resultOf(bench.Sent(), seq) != proto.ResultError {
		t.Fatal("no RESULT ERROR at the deadline")
	}
	bench.Advance(4*proto.Second, 10*proto.Millisecond)
	if len(l.done) != 2 || !l.done[0] || l.done[1] {
		t.Errorf("completions taken: %v, want [true false]", l.done)
	}
	if bench.Stack.Dev.Stats.DeadlineMissed != 1 || bench.Stack.Dev.Stats.LateCompletions != 1 {
		t.Errorf("device stats %+v", bench.Stack.Dev.Stats)
	}
	// Run locally: no request, so no ticket to complete.
	l.h.Run(5, 1)
	if l.h.Complete(l.ticket, proto.ResultOK) {
		t.Error("completed a local run")
	}
}

// Module events (spec 19): queued one per id, sent after the delay, again
// until the controller's network-only REPLY; a full queue refuses.
func TestNotifySendsEventsUntilAcknowledged(t *testing.T) {
	p := &probe{code: 1}
	bench := nodetest.New(nodetest.Options{}, p)
	for _, id := range []byte{7, 7, 8, 9, 10} {
		if !p.h.Notify(id) {
			t.Fatalf("notify %d refused", id)
		}
	}
	if p.h.Notify(11) || bench.Stack.Stats.NotifyDropped != 1 || bench.Stack.Events.Stats.Merged != 1 {
		t.Fatalf("full queue: stats %+v %+v", bench.Stack.Stats, bench.Stack.Events.Stats)
	}
	bench.Advance(1*proto.Second, 10*proto.Millisecond) // Midpoint: the delay is 1 s
	sent := nodetest.Events(bench.Sent())
	if len(sent) != 4 || sent[0][5] != 7 || sent[0][2] != proto.DefaultControllerID {
		t.Fatalf("events %x", sent)
	}
	first := sent[0]
	bench.Acknowledge(first[3], first[4])
	if bench.Stack.Events.Pending() != 3 || bench.Stack.Events.Stats.Acked != 1 {
		t.Fatalf("after the ack: pending %d", bench.Stack.Events.Pending())
	}
	bench.Advance(15*proto.Second, 100*proto.Millisecond) // the first retry (15 s, Midpoint)
	again := nodetest.Events(bench.Sent())
	if len(again) != 3 || again[0][5] != 8 || again[0][3] != sent[1][3] {
		t.Fatalf("retries %x", again)
	}
	if !p.h.Notify(11) {
		t.Error("a slot freed by the ack")
	}
}

// memFlash is a flash in RAM for the journals.
type memFlash map[int]*[journal.PageSize]byte

func (f memFlash) page(p int) *[journal.PageSize]byte {
	if f[p] == nil {
		f[p] = new([journal.PageSize]byte)
		for i := range f[p] {
			f[p][i] = 0xFF
		}
	}
	return f[p]
}

func (f memFlash) Erase(p int) bool {
	for i := range f.page(p) {
		f[p][i] = 0xFF
	}
	return true
}

func (f memFlash) Program(p, offset int, dw *[8]byte) bool {
	copy(f.page(p)[offset:], dw[:])
	return true
}

func (f memFlash) Read(p, offset int, dw *[8]byte) bool {
	copy(dw[:], f.page(p)[offset:offset+8])
	return true
}

// After a power cut the node still knows what it ran (spec 4.1) and what
// it heard (spec 5): a late copy is answered, not run; a recorded frame
// played back is refused.
func TestPowerCutKeepsRecordsAndMarks(t *testing.T) {
	flash := memFlash{}
	a := &probe{code: 1}
	bench := nodetest.New(nodetest.Options{
		Records: journal.NewRecords(journal.Where{Flash: flash, First: 0, Pages: 1}),
		Marks:   journal.NewMarks(journal.Where{Flash: flash, First: 2, Pages: 2}),
	}, a)
	seq := bench.Request(1, 3)
	recorded := bench.LastSealed()
	bench.Sent()
	bench.PowerCut()
	bench.Air.In = append(bench.Air.In, recorded)
	bench.Stack.Poll(bench.Now)
	if bench.Stack.Sec.Stats.Replayed != 1 || len(bench.Air.Out) != 0 {
		t.Fatalf("a frame from before the cut played back: replayed %d, sent %d", bench.Stack.Sec.Stats.Replayed, len(bench.Air.Out))
	}
	bench.Retry(seq, 1, 3)
	if got := nodetest.Answer(bench.Sent(), seq); got != proto.ResultOK || len(a.values) != 1 {
		t.Fatalf("late copy after the cut: answered %d, ran %d times", got, len(a.values))
	}
}

// Without stores (as before) a power cut forgets both.
func TestPowerCutWithoutStores(t *testing.T) {
	a := &probe{code: 1}
	bench := nodetest.New(nodetest.Options{}, a)
	seq := bench.Request(1, 3)
	bench.Sent()
	bench.PowerCut()
	bench.Retry(seq, 1, 3)
	if len(a.values) != 2 {
		t.Fatalf("ran %d times", len(a.values))
	}
}

// countMarks counts the marks written.
type countMarks struct{ calls uint32 }

func (m *countMarks) Load(*proto.Secure) proto.MarkStore { return m }

func (m *countMarks) Mark(byte, uint32) bool {
	m.calls++
	return true
}

// The replay marks go to flash at most once per MarkInterval (spec 5), the
// first at once; one waiting makes NextDue come back for it.
func TestMarksWrittenOncePerInterval(t *testing.T) {
	marks := &countMarks{}
	bench := nodetest.New(nodetest.Options{Marks: marks}, &probe{code: 1})
	bench.Request(1, 1)
	if marks.calls != 1 {
		t.Fatalf("first mark: %d writes", marks.calls)
	}
	bench.Now = 300 * proto.Millisecond
	bench.Request(1, 2)
	if marks.calls != 1 {
		t.Fatalf("within the interval: %d writes", marks.calls)
	}
	if due := bench.Stack.NextDue(); due != proto.MarkInterval {
		t.Fatalf("NextDue %d, want %d", due, proto.MarkInterval)
	}
	bench.Advance(proto.MarkInterval-bench.Now, 100*proto.Millisecond)
	if marks.calls != 2 {
		t.Fatalf("after the interval: %d writes", marks.calls)
	}
	bench.Stack.Flush(bench.Now)
	if marks.calls != 2 || bench.Stack.NextDue() != proto.Never {
		t.Fatalf("nothing left to write: %d writes, due %d", marks.calls, bench.Stack.NextDue())
	}
}

// countFlash counts the erases of a memFlash.
type countFlash struct {
	memFlash
	erases int
}

func (f *countFlash) Erase(p int) bool { f.erases++; return f.memFlash.Erase(p) }

// The journals erase their next bank ahead, one page per Poll and only
// while the air is quiet; a compaction then erases nothing itself.
func TestFlashErasedAheadOnlyWhileQuiet(t *testing.T) {
	flash := &countFlash{memFlash: memFlash{}}
	records := journal.NewRecords(journal.Where{Flash: flash, First: 0, Pages: 2})
	bench := nodetest.New(nodetest.Options{
		Records: records,
		Marks:   journal.NewMarks(journal.Where{Flash: flash, First: 4, Pages: 1}),
	}, &probe{code: 1})
	bench.Air.Busy = true
	for records.J.Stats.Compactions < 2 {
		bench.Request(1, 1)
	}
	if !records.Unprepared() {
		t.Fatal("pages erased while the air was busy")
	}

	bench.Air.Busy = false
	for i := 0; records.Unprepared(); i++ {
		if bench.Stack.NextDue() > bench.Now || i > 10 {
			t.Fatalf("pass %d: pages left to erase, NextDue %d", i, bench.Stack.NextDue())
		}
		bench.Stack.Poll(bench.Now)
	}

	bench.Air.Busy = true
	before, compactions := flash.erases, records.J.Stats.Compactions
	for records.J.Stats.Compactions == compactions {
		bench.Request(1, 1)
	}
	if flash.erases != before {
		t.Fatalf("the compaction erased %d pages itself", flash.erases-before)
	}
}
