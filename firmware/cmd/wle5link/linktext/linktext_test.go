package linktext

import (
	"bytes"
	"testing"
)

func TestFrameIsRadiolinkTestFrame(t *testing.T) {
	// cmd/radiolink: copy(msg[:], "CAIRNMESH-BENCH-"), msg[16] = seq, 17 bytes.
	var f [FrameLen]byte
	Frame(&f, 7)
	want := append([]byte("CAIRNMESH-BENCH-"), 7)
	if !bytes.Equal(f[:], want) {
		t.Fatalf("Frame = %q, want %q", f[:], want)
	}
	if !IsTestFrame(f[:]) || !IsTestFrame(want) {
		t.Fatal("own frame not recognised")
	}
	if IsTestFrame(f[:16]) || IsTestFrame(append([]byte("CAIRNMESH-BENCH_"), 0)) {
		t.Fatal("wrong frame recognised")
	}
}

func TestAppendRx(t *testing.T) {
	p := append([]byte("CAIRNMESH-BENCH-"), 9)
	got := string(AppendRx(nil, len(p), -87, -3, p))
	// 16 bytes = 32 hex digits; the 17th byte (the sequence) is not shown.
	want := "wle5link: rx n=17 rssi=-87 snr=-3 hex=434149524e4d4553482d42454e43482d"
	if got != want {
		t.Fatalf("got %q, want %q", got, want)
	}
}

func TestAppendRxShortAndEmpty(t *testing.T) {
	if got := string(AppendRx(nil, 2, 5, 12, []byte{0x00, 0xAB})); got != "wle5link: rx n=2 rssi=5 snr=12 hex=00ab" {
		t.Fatalf("got %q", got)
	}
	if got := string(AppendRx(nil, 0, -120, 0, nil)); got != "wle5link: rx n=0 rssi=-120 snr=0 hex=" {
		t.Fatalf("got %q", got)
	}
}

func TestAppendRxLongPayloadShowsSixteenBytes(t *testing.T) {
	p := make([]byte, 255)
	for i := range p {
		p[i] = 0xFF
	}
	got := string(AppendRx(nil, len(p), -100, -20, p))
	want := "wle5link: rx n=255 rssi=-100 snr=-20 hex=" + string(bytes.Repeat([]byte("ff"), 16))
	if got != want {
		t.Fatalf("got %q", got)
	}
}

func TestAppendTxAndSum(t *testing.T) {
	if got := string(AppendTxOK(nil)); got != "wle5link: tx ok" {
		t.Fatalf("got %q", got)
	}
	if got := string(AppendTxFail(nil, "timeout")); got != "wle5link: tx timeout" {
		t.Fatalf("got %q", got)
	}
	if got := string(AppendSum(nil, 12, 3, 40)); got != "wle5link: sum rx=12 crc_err=3 tx=40" {
		t.Fatalf("got %q", got)
	}
	if got := string(AppendSum(nil, 4294967295, 0, 0)); got != "wle5link: sum rx=4294967295 crc_err=0 tx=0" {
		t.Fatalf("got %q", got)
	}
}

func TestAppendKeepsPrefixBytes(t *testing.T) {
	b := AppendTxOK([]byte("x"))
	if string(b) != "xwle5link: tx ok" {
		t.Fatalf("got %q", b)
	}
}

func TestParsePeriod(t *testing.T) {
	for _, c := range []struct {
		in   string
		want int
		ok   bool
	}{
		{"", DefaultPeriodSec, true},
		{"1", 1, true},
		{"30", 30, true},
		{"3600", 3600, true},
		{"3601", 0, false},
		{"0", 0, false},
		{"00", 0, false},
		{"-5", 0, false},
		{"+5", 0, false},
		{"5s", 0, false},
		{"1.5", 0, false},
		{" 5", 0, false},
		{"99999999999999999999", 0, false},
	} {
		got, ok := ParsePeriod(c.in)
		if got != c.want || ok != c.ok {
			t.Errorf("ParsePeriod(%q) = %d, %v; want %d, %v", c.in, got, ok, c.want, c.ok)
		}
	}
}

func TestSchedule(t *testing.T) {
	s := Schedule{PeriodUs: 5_000_000}
	s.Start(1_000_000, 2_000_000) // first due at 3 s
	if s.Due(2_999_999) || !s.Due(3_000_000) {
		t.Fatal("first due time wrong")
	}
	s.Sent(3_000_100) // started a little late: the grid stays at 8 s
	if s.Due(7_999_999) || !s.Due(8_000_000) {
		t.Fatal("period grid drifted")
	}
	// The loop stalled for 20 s: one frame, then a full period, not a burst.
	s.Sent(28_000_000)
	if s.Due(32_999_999) || !s.Due(33_000_000) {
		t.Fatalf("after a stall, next = %d", s.next)
	}
}

func TestTracker(t *testing.T) {
	var tr Tracker
	if tr.Waiting() || tr.Update(5, 0) != Pending {
		t.Fatal("idle tracker")
	}
	tr.Start(5, 1)
	if !tr.Waiting() || tr.Update(5, 1) != Pending {
		t.Fatal("still on air")
	}
	if tr.Update(6, 1) != Done || tr.Waiting() {
		t.Fatal("TxDone not seen")
	}
	if tr.Update(7, 1) != Pending {
		t.Fatal("reported twice")
	}
	tr.Start(7, 1)
	if tr.Update(7, 2) != Timeout || tr.Waiting() {
		t.Fatal("timeout not seen")
	}
}
