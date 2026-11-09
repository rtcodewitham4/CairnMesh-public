package gateway

import (
	"bytes"
	"testing"

	"cairnmesh/firmware/proto"
	"cairnmesh/firmware/radio/sx126x"
)

// fakeRadio transmits until the test ends it (tx = false), receives a
// packet while hearing.
type fakeRadio struct {
	tx, timedOut, refuse bool
	hearing              bool
	sick, broken         bool // Check fails; Recover fails too
	recovers             int
	sent                 [][]byte
	preambles            []uint16
	rx                   [][]byte
	info                 sx126x.PacketInfo
	tunes                [][2]uint32
}

func (f *fakeRadio) SendPreamble(p []byte, preamble uint16) bool {
	if f.tx || f.refuse {
		return false
	}
	f.tx = true
	f.sent = append(f.sent, append([]byte(nil), p...))
	f.preambles = append(f.preambles, preamble)
	return true
}

func (f *fakeRadio) Receive() []byte {
	if len(f.rx) == 0 {
		return nil
	}
	p := f.rx[0]
	f.rx = f.rx[1:]
	return p
}

func (f *fakeRadio) Packet() sx126x.PacketInfo { return f.info }
func (f *fakeRadio) Transmitting() bool        { return f.tx }
func (f *fakeRadio) TxTimedOut() bool          { return f.timedOut }
func (f *fakeRadio) Healthy() bool             { return !f.refuse }
func (f *fakeRadio) Receiving() bool           { return f.hearing }
func (f *fakeRadio) Check() bool               { return !f.sick }

// Recover ends a transmission and a reception; it fails while broken.
func (f *fakeRadio) Recover() bool {
	f.recovers++
	f.tx, f.hearing = false, false
	f.sick = f.broken
	return !f.broken
}

// Airtime: 1 ms a byte, the longest packet 255 ms; preamble symbols count
// 1 ms each past 8.
func (f *fakeRadio) Airtime(n int, preamble uint16) proto.Time {
	if preamble > 8 {
		n += int(preamble) - 8
	}
	return proto.Time(n) * proto.Millisecond
}
func (f *fakeRadio) Counts() sx126x.Stats {
	return sx126x.Stats{CRCErrors: 3, Faults: 0x12345, Resets: uint32(f.recovers)}
}

func (f *fakeRadio) Retune(freq uint32, sf uint8) bool {
	if f.tx {
		return false
	}
	f.tunes = append(f.tunes, [2]uint32{freq, uint32(sf)})
	return true
}

type rig struct {
	b   Bridge
	r   [2]*fakeRadio
	up  [][]byte
	now proto.Time
}

func newRig() *rig {
	g := &rig{r: [2]*fakeRadio{{}, {}}}
	g.b = Bridge{
		Radios: []Radio{g.r[0], g.r[1]},
		Up:     func(m []byte) { g.up = append(g.up, append([]byte(nil), m...)) },
		Frequency: func(ch byte) (uint32, bool) {
			if ch > 31 {
				return 0, false
			}
			return 433500000 + uint32(ch)*1000000, true
		},
		SF: 7,
	}
	g.b.Init()
	return g
}

// at moves the clock to ms and polls.
func (g *rig) at(ms int) {
	g.now = proto.Time(ms) * proto.Millisecond
	g.b.Poll(g.now)
}

func (g *rig) handle(m ...byte) { g.b.Handle(m, g.now) }

func (g *rig) take() []byte {
	if len(g.up) == 0 {
		return nil
	}
	m := g.up[0]
	g.up = g.up[1:]
	return m
}

func tx(radio, flags byte, preamble, answerMs uint16, frame ...byte) []byte {
	return append([]byte{MsgTx, radio, flags, byte(preamble >> 8), byte(preamble),
		byte(answerMs >> 8), byte(answerMs)}, frame...)
}

func txDone(radio, result byte, queued, air uint16) []byte {
	return []byte{MsgTxDone, radio, result, byte(queued >> 8), byte(queued), byte(air >> 8), byte(air)}
}

func TestTxAndTxDone(t *testing.T) {
	g := newRig()
	g.handle(tx(0, FlagUrgent, 1000, 0, 0xAA, 0xBB)...)
	g.at(0)
	if len(g.r[0].sent) != 1 || !bytes.Equal(g.r[0].sent[0], []byte{0xAA, 0xBB}) || g.r[0].preambles[0] != 1000 {
		t.Fatalf("sent %v %v", g.r[0].sent, g.r[0].preambles)
	}
	g.at(51)
	if g.take() != nil {
		t.Fatal("TX_DONE before the end")
	}
	g.r[0].tx = false
	g.at(52)
	if m := g.take(); !bytes.Equal(m, txDone(0, TxOK, 0, 52)) {
		t.Errorf("got % x", m)
	}
	g.r[0].timedOut = true
	g.handle(tx(0, 0, 0, 0, 1)...)
	g.at(60)
	g.r[0].tx = false
	g.at(70)
	if m := g.take(); m[2] != TxTimeout {
		t.Errorf("timeout: % x", m)
	}
}

// Rule 1: the second radio waits for the first to finish.
func TestNeverBothTransmit(t *testing.T) {
	g := newRig()
	g.handle(tx(0, FlagUrgent, 0, 0, 1)...)
	g.handle(tx(1, FlagUrgent, 0, 0, 2)...)
	g.at(0)
	if len(g.r[0].sent) != 1 || len(g.r[1].sent) != 0 {
		t.Fatal("both transmit")
	}
	g.at(30)
	if len(g.r[1].sent) != 0 {
		t.Fatal("radio 1 started during radio 0's transmission")
	}
	g.r[0].tx = false
	g.at(50)
	if len(g.r[1].sent) != 1 {
		t.Fatal("radio 1 did not start after radio 0")
	}
	g.r[1].tx = false
	g.at(100)
	g.take()
	if m := g.take(); !bytes.Equal(m, txDone(1, TxOK, 50, 50)) {
		t.Errorf("got % x", m)
	}
}

// Rule 2: radio 0 waits for an answer; radio 1 holds back frames not urgent.
func TestAnswerWindowDefersOnlyNotUrgent(t *testing.T) {
	g := newRig()
	g.handle(tx(0, FlagUrgent, 0, 500, 1)...)
	g.at(0)
	g.r[0].tx = false
	g.at(50) // window open until 550
	g.handle(tx(1, 0, 0, 0, 2)...)
	g.at(60)
	if len(g.r[1].sent) != 0 || g.b.Stats.Deferred != 1 {
		t.Fatalf("not deferred: %v %+v", g.r[1].sent, g.b.Stats)
	}
	g.handle(tx(0, 0, 0, 0, 3)...) // radio 0's own frame is not held back by its own window
	g.at(70)
	if len(g.r[0].sent) != 2 {
		t.Fatal("radio 0 held back by its own window")
	}
	g.r[0].tx = false
	g.at(120)
	g.at(549)
	if len(g.r[1].sent) != 0 {
		t.Fatal("radio 1 sent inside the window")
	}
	g.at(550)
	if len(g.r[1].sent) != 1 || g.b.Stats.Deferred != 1 {
		t.Fatalf("not sent at the window's end: %+v", g.b.Stats)
	}

	g = newRig()
	g.handle(tx(0, FlagUrgent, 0, 500, 1)...)
	g.at(0)
	g.r[0].tx = false
	g.at(50)
	g.handle(tx(1, FlagUrgent, 0, 0, 2)...)
	g.at(51)
	if len(g.r[1].sent) != 1 || g.b.Stats.Deferred != 0 {
		t.Error("urgent frame held back")
	}
}

// Each radio's requests keep their order, TUNE included; a TUNE of the
// radio that does not transmit runs at once.
func TestOrderPerRadio(t *testing.T) {
	g := newRig()
	g.handle(tx(0, FlagUrgent, 0, 0, 1)...)
	g.handle(MsgTune, 0, 1, 9)
	g.handle(tx(0, FlagUrgent, 0, 0, 2)...)
	g.handle(MsgTune, 1, 0, 8)
	g.at(0)
	if len(g.r[0].tunes) != 0 || len(g.r[1].tunes) != 1 {
		t.Fatalf("tunes %v %v: radio 0's must wait for its frame, radio 1's not", g.r[0].tunes, g.r[1].tunes)
	}
	g.r[0].tx = false
	g.at(50)
	if len(g.r[0].tunes) != 1 || g.r[0].tunes[0] != [2]uint32{434500000, 9} || len(g.r[0].sent) != 2 {
		t.Fatalf("tune %v sent %v", g.r[0].tunes, g.r[0].sent)
	}
	g.handle(MsgPing)
	if m := g.up[len(g.up)-1]; m[4] != 1 || m[5] != 1 || m[6] != 9 || m[18] != 1 || m[19] != 0 || m[20] != 8 {
		t.Errorf("status % x", m)
	}
}

func TestRefusals(t *testing.T) {
	g := newRig()
	g.handle(tx(5, 0, 0, 0, 1)...)
	g.handle(tx(0, 0, 0, 0)...)
	g.handle(MsgTx, 0)
	g.handle(0x55)
	g.b.Handle(nil, 0)
	g.handle(MsgTune, 0, 40, 7)
	g.handle(MsgTune, 0, 1, 13)
	g.handle(MsgTune, 9, 1, 7)
	g.at(0)
	if m := g.take(); !bytes.Equal(m, txDone(5, TxRefused, 0, 0)) {
		t.Errorf("bad radio: % x", m)
	}
	if m := g.take(); !bytes.Equal(m, txDone(0, TxRefused, 0, 0)) {
		t.Errorf("empty frame: % x", m)
	}
	if s := g.b.Stats; s.Malformed != 4 || s.TuneFailed != 2 || s.Refused != 2 {
		t.Errorf("stats %+v", s)
	}
	g.r[1].refuse = true
	g.handle(tx(1, 0, 0, 0, 1)...)
	g.at(10)
	if m := g.take(); !bytes.Equal(m, txDone(1, TxRefused, 10, 0)) { // queued at 0, tried at 10
		t.Errorf("radio refused: % x", m)
	}
}

func TestQueueFull(t *testing.T) {
	g := newRig()
	for i := 0; i < QueueLen; i++ {
		g.handle(tx(0, 0, 0, 0, byte(i))...)
	}
	g.handle(tx(1, 0, 0, 0, 9)...)
	if m := g.take(); !bytes.Equal(m, txDone(1, TxQueueFull, 0, 0)) {
		t.Errorf("got % x", m)
	}
	g.handle(MsgTune, 1, 0, 7)
	if g.b.Stats.QueueFull != 2 {
		t.Errorf("stats %+v", g.b.Stats)
	}
}

func TestRxMessage(t *testing.T) {
	g := newRig()
	g.r[1].rx = [][]byte{{1, 2, 3}}
	g.r[1].info = sx126x.PacketInfo{RSSI: -120, SNR: -7}
	g.at(0)
	if m := g.take(); !bytes.Equal(m, []byte{MsgRx, 1, 0xFF, 0x88, 0xF9, 1, 2, 3}) {
		t.Errorf("got % x", m)
	}
	if g.b.Stats.Received != 1 {
		t.Error("count")
	}
}

func TestStatus(t *testing.T) {
	g := newRig()
	g.handle(MsgPing)
	m := g.take()
	g.b.Stats.WatchdogBoot = 1
	g.r[1].recovers = 2
	g.handle(MsgPing)
	m = g.take()
	if len(m) != 4+2*14+1+10*4 || m[1] != Version || m[2] != 2 || m[3] != 14 || m[4] != 1 || m[6] != 7 ||
		m[8] != 0 || m[9] != 3 || m[14] != 0xFF || m[15] != 0xFF || // CRC errors 3, faults saturated
		m[16] != 0 || m[17] != 0 || m[30] != 0 || m[31] != 2 || m[32] != 10 || m[len(m)-1] != 1 {
		t.Errorf("status % x", m)
	}
}

// Rule 4: a radio receiving a packet holds its TX and TUNE until the end;
// BUSY tells the PC each change.
func TestReceivingHoldsTheRadio(t *testing.T) {
	g := newRig()
	g.r[1].hearing = true
	g.at(0)
	if m := g.take(); !bytes.Equal(m, []byte{MsgBusy, 1, 1}) {
		t.Fatalf("got % x", m)
	}
	g.handle(tx(1, FlagUrgent, 0, 0, 1)...)
	g.handle(MsgTune, 1, 1, 9)
	g.handle(tx(0, FlagUrgent, 0, 0, 2)...) // the other radio is free
	g.at(10)
	if len(g.r[1].sent) != 0 || len(g.r[1].tunes) != 0 || len(g.r[0].sent) != 1 || g.b.Stats.Held != 1 {
		t.Fatalf("radio 1 sent %v tunes %v, radio 0 sent %v, %+v", g.r[1].sent, g.r[1].tunes, g.r[0].sent, g.b.Stats)
	}
	g.r[0].tx = false
	g.at(40)
	g.take()
	if len(g.r[1].sent) != 0 || g.b.Stats.Held != 1 {
		t.Fatal("sent during the packet")
	}
	g.r[1].hearing = false
	g.r[1].rx = [][]byte{{7}}
	g.at(45)
	if m := g.take(); m[0] != MsgRx {
		t.Fatalf("the packet first: % x", m)
	}
	if m := g.take(); !bytes.Equal(m, []byte{MsgBusy, 1, 0}) {
		t.Fatalf("got % x", m)
	}
	if len(g.r[1].sent) != 1 {
		t.Fatal("not sent after the packet")
	}
	g.r[1].tx = false
	g.at(100)
	if len(g.r[1].tunes) != 1 {
		t.Fatal("TUNE after its frame")
	}

	// An end never seen: free after the longest packet.
	g = newRig()
	g.r[0].hearing = true
	g.handle(tx(0, FlagUrgent, 0, 0, 1)...)
	g.at(0)
	g.at(255)
	if len(g.r[0].sent) != 0 {
		t.Fatal("sent before the longest packet's end")
	}
	g.at(256)
	if len(g.r[0].sent) != 1 || !bytes.Equal(g.up[len(g.up)-1], []byte{MsgBusy, 0, 0}) {
		t.Fatalf("still held: %v % x", g.r[0].sent, g.up)
	}
}

// A transmission that does not end is ended as a timeout, the radio reset.
func TestStuckTransmission(t *testing.T) {
	g := newRig()
	g.handle(tx(0, FlagUrgent, 200, 0, 1, 2, 3)...) // 3 + 192 ms: deadline 2*195 + 100 + 1000
	g.at(0)
	g.handle(tx(1, FlagUrgent, 0, 0, 4)...)
	g.at(1489)
	if g.r[0].recovers != 0 || len(g.r[1].sent) != 0 {
		t.Fatal("ended before the deadline")
	}
	g.at(1490)
	if m := g.take(); !bytes.Equal(m, txDone(0, TxTimeout, 0, 1490)) {
		t.Fatalf("got % x", m)
	}
	if g.r[0].recovers != 1 || g.b.Stats.TxStuck != 1 || len(g.r[1].sent) != 1 {
		t.Fatalf("recovers %d %+v, radio 1 sent %v", g.r[0].recovers, g.b.Stats, g.r[1].sent)
	}
}

// A radio failing its check is reset; one that cannot be reset is tried
// again, the wait doubling to RetryMax.
func TestHealthCheck(t *testing.T) {
	g := newRig()
	g.at(0)
	if g.r[0].recovers != 0 {
		t.Fatal("a healthy radio reset")
	}
	g.r[0].sick = true
	g.at(999)
	if g.r[0].recovers != 0 {
		t.Fatal("checked before CheckEvery")
	}
	g.at(1000)
	if g.r[0].recovers != 1 || g.r[1].recovers != 0 {
		t.Fatal("not reset")
	}

	g.r[1].sick, g.r[1].broken = true, true
	var at []int
	for ms := 1001; ms <= 130000; ms++ {
		n := g.r[1].recovers
		g.at(ms)
		if g.r[1].recovers != n {
			at = append(at, ms)
		}
	}
	want := []int{2000, 3000, 5000, 9000, 17000, 33000, 65000, 97000, 129000}
	if len(at) != len(want) {
		t.Fatalf("tries at %v, want %v", at, want)
	}
	for i := range want {
		if at[i] != want[i] {
			t.Fatalf("tries at %v, want %v", at, want)
		}
	}
	g.r[1].broken = false
	g.at(161000)
	if g.r[1].recovers != len(want)+1 || g.r[1].sick {
		t.Fatal("not back")
	}
	g.at(162000)
	if g.r[1].recovers != len(want)+1 {
		t.Error("reset again after it came back")
	}
}

// A radio receiving is not checked; a reset ends a reception the PC was
// told of.
func TestHealthCheckWaitsAndEndsBusy(t *testing.T) {
	g := newRig()
	g.r[0].sick, g.r[0].hearing = true, true
	g.at(0)
	if g.r[0].recovers != 0 {
		t.Fatal("checked while receiving")
	}
	g.r[0].hearing = false
	g.at(1)
	if g.r[0].recovers != 1 {
		t.Fatal("not checked after the packet")
	}
	g = newRig()
	g.r[1].hearing = true
	g.at(0)
	g.take()
	g.b.recover(1, 0)
	if m := g.take(); !bytes.Equal(m, []byte{MsgBusy, 1, 0}) {
		t.Errorf("got % x", m)
	}
}
