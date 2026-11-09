package proto

import "testing"

// The device's record log (spec 4.1) when writes fail and power is cut.

type countApp struct {
	runs     map[uint16]int
	duration Time
}

func (a *countApp) Duration(byte, byte) Time { return a.duration }

func (a *countApp) Perform(r Request) byte {
	a.runs[r.ID]++
	return ResultOK
}

type frameLog struct{ frames [][]byte }

func (f *frameLog) Send(plain []byte) { f.frames = append(f.frames, append([]byte(nil), plain...)) }

func (f *frameLog) take() [][]byte {
	out := f.frames
	f.frames = nil
	return out
}

const logTestID = 5

func request(sender, seq, epoch, action byte) []byte {
	return []byte{KindRequest, sender, logTestID, seq, epoch, action, 1}
}

func query(sender, seq, epoch byte) []byte {
	return []byte{KindRequest, sender, logTestID, 99, epoch, QueryAction, seq}
}

type logBench struct {
	d   Device
	log memLog
	app countApp
	out frameLog
	cfg DeviceConfig
	now Time
}

func newLogBench(duration Time) *logBench {
	b := &logBench{app: countApp{runs: map[uint16]int{}, duration: duration}}
	b.cfg = DefaultDeviceConfig(logTestID)
	b.cfg.Awake, b.cfg.HeartbeatInterval = true, Never
	b.log.boot(&b.d, b.cfg, &b.app, &b.out, 0)
	return b
}

func (b *logBench) rx(frame []byte) [][]byte {
	b.d.OnFrame(frame, b.now)
	b.d.Tick(b.now)
	return b.out.take()
}

// run advances to t, firing every timer on the way.
func (b *logBench) run(t Time) {
	for due := b.d.NextDue(); due <= t; due = b.d.NextDue() {
		b.now = due
		b.d.Tick(due)
	}
	b.now = t
}

func (b *logBench) powerCut() {
	b.log.failFrom = 0
	b.log.boot(&b.d, b.cfg, &b.app, &b.out, b.now)
}

func answerOf(t *testing.T, frames [][]byte) byte {
	t.Helper()
	for _, f := range frames {
		if f[0] == KindReply {
			return f[5]
		}
	}
	t.Fatalf("no answer in %x", frames)
	return 0
}

func TestLogFailedRequestNeitherRunsNorAnswers(t *testing.T) {
	b := newLogBench(0)
	b.log.failFrom = 1
	if got := b.rx(request(1, 7, 3, 1)); len(got) != 0 || b.app.runs[3<<8|7] != 0 {
		t.Fatalf("a request not logged: answered %x, ran %d times", got, b.app.runs[3<<8|7])
	}
	if b.d.Stats.LogFailed != 1 {
		t.Fatalf("LogFailed %d", b.d.Stats.LogFailed)
	}
	b.log.failFrom = 0
	if r := answerOf(t, b.rx(request(1, 7, 3, 1))); r != ResultOK || b.app.runs[3<<8|7] != 1 {
		t.Fatalf("the retry: %d, ran %d times", r, b.app.runs[3<<8|7])
	}
}

func TestLogFailedSlowRequestAndQuery(t *testing.T) {
	b := newLogBench(5 * Second) // past the answer budget: ACCEPTED, then a RESULT
	b.log.failFrom = 1
	if got := b.rx(request(1, 7, 3, 1)); len(got) != 0 || len(b.app.runs) != 0 {
		t.Fatalf("a slow request not logged: answered %x, ran %v", got, b.app.runs)
	}
	if got := b.rx(query(1, 8, 3)); len(got) != 0 {
		t.Fatalf("a query's NOT_RUN not logged must not be promised: %x", got)
	}
	b.log.failFrom = 0
	if r := answerOf(t, b.rx(query(1, 8, 3))); r != ResultNotRun {
		t.Fatalf("query: %d", r)
	}
	b.powerCut()
	if r := answerOf(t, b.rx(request(1, 8, 3, 1))); r != ResultNotRun || len(b.app.runs) != 0 {
		t.Fatalf("NOT_RUN must hold through a power cut: %d, ran %v", r, b.app.runs)
	}
}

// The final result's write is lost: the record reads back ACCEPTED, so
// INTERRUPTED (spec 4.1; v5), and its RESULT goes out after the boot.
func TestPowerCutBeforeTheResultIsLogged(t *testing.T) {
	b := newLogBench(0)
	b.log.failFrom = 2 // ACCEPTED is logged, the result is not
	if r := answerOf(t, b.rx(request(1, 7, 3, 1))); r != ResultOK {
		t.Fatalf("answer %d", r)
	}
	b.powerCut()
	b.run(b.cfg.ResultTimeout)
	var result []byte
	for _, f := range b.out.take() {
		if f[0] == KindResult {
			result = f
		}
	}
	if result == nil || result[5] != ResultInterrupted {
		t.Fatalf("after the boot: RESULT %x, want INTERRUPTED", result)
	}
	if r := answerOf(t, b.rx(request(1, 7, 3, 1))); r != ResultInterrupted || b.app.runs[3<<8|7] != 1 {
		t.Fatalf("a late copy: %d, ran %d times", r, b.app.runs[3<<8|7])
	}
}

// A query for an interrupted record answers 0x04 and closes it; the
// original request arriving later still replays 0x04 and does not run.
func TestQueryAnswersAnInterruptedRecord(t *testing.T) {
	b := newLogBench(5 * Second)
	b.rx(request(1, 7, 3, 1))
	b.run(2 * Second)
	b.out.take()
	b.powerCut()
	b.run(b.cfg.ResultTimeout)
	b.out.take()

	if r := answerOf(t, b.rx(query(1, 7, 3))); r != ResultInterrupted {
		t.Fatalf("query after the boot: %d, want INTERRUPTED", r)
	}
	if r := answerOf(t, b.rx(request(1, 7, 3, 1))); r != ResultInterrupted || b.app.runs[3<<8|7] != 1 {
		t.Fatalf("late copy: %d, ran %d times", r, b.app.runs[3<<8|7])
	}
}

// A late copy after a power cut is answered from the log and not run
// (spec 4.1's reason for the log).
func TestLateCopyAfterPowerCut(t *testing.T) {
	for _, duration := range []Time{0, 300 * Millisecond, 5 * Second} {
		b := newLogBench(duration)
		b.rx(request(1, 7, 3, 1))
		b.run(2 * duration)
		b.out.take()
		b.powerCut()
		if r := answerOf(t, b.rx(request(1, 7, 3, 1))); r != ResultOK || b.app.runs[3<<8|7] != 1 {
			t.Fatalf("duration %d: late copy %d, ran %d times", duration, r, b.app.runs[3<<8|7])
		}
	}
}

// The sender order is logged too: a query from sender 1 makes sender 2 the
// one to go when a third appears, after a power cut as well (spec 4.1, Q23).
func TestSenderOrderThroughPowerCut(t *testing.T) {
	b := newLogBench(0)
	b.rx(request(1, 7, 3, 1))
	b.rx(request(2, 7, 3, 1))
	b.rx(query(1, 7, 3)) // sender 1 heard from last, nothing new recorded
	b.powerCut()
	b.rx(request(9, 7, 3, 1)) // pushes out sender 2
	b.powerCut()
	if r := answerOf(t, b.rx(request(1, 7, 3, 1))); r != ResultOK || b.app.runs[3<<8|7] != 3 {
		t.Fatalf("sender 1's record lost: %d, runs %d", r, b.app.runs[3<<8|7])
	}
	b.rx(request(2, 7, 3, 1))
	if b.app.runs[3<<8|7] != 4 {
		t.Fatalf("sender 2 should have gone: runs %d", b.app.runs[3<<8|7])
	}
}
