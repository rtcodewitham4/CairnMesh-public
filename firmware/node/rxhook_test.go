//go:build cairn_testfilter

package node_test

import (
	"fmt"
	"strings"
	"testing"

	"cairnmesh/firmware/console"
	"cairnmesh/firmware/node"
	"cairnmesh/firmware/node/nodetest"
	"cairnmesh/firmware/proto"
	"cairnmesh/firmware/rxfilter"
)

// The receive test filter in the stack (tag cairn_testfilter): where it sits
// (after the MIC, before any protocol state), what it keys on (TX), what it
// leaves alone. The default matrix is held to "no filter" by
// TestRxFilterDefaultBehavesLikeNoFilter (rxself_test.go).

type port struct {
	in  []byte
	out strings.Builder
}

func (p *port) Connected() bool { return true }
func (p *port) Buffered() int   { return len(p.in) }
func (p *port) ReadByte() (byte, error) {
	b := p.in[0]
	p.in = p.in[1:]
	return b, nil
}
func (p *port) Write(b []byte) int       { p.out.Write(b); return len(b) }
func (p *port) WriteString(s string) int { p.out.WriteString(s); return len(s) }
func (p *port) Writable() int            { return 1 << 20 }

// rxf runs one console line against the stack's filter, as the firmware's
// command does, and returns what it printed.
type shell struct {
	c *console.Console
	p *port
}

func newShell(s *node.Stack) *shell {
	sh := &shell{p: &port{}}
	sh.c = &console.Console{}
	sh.c.Init(sh.p)
	sh.c.Handle("rxf", "", func(c *console.Console, args [][]byte) {
		rxfilter.Command(c, args, nodetest.NodeID, s.Started(), s.RxFilter())
	})
	sh.c.Poll(0)
	sh.c.Poll(console.GreetDelay)
	return sh
}

func (sh *shell) run(line string) string {
	sh.p.out.Reset()
	sh.p.in = []byte(line + "\r")
	sh.c.Poll(console.GreetDelay + 1)
	out := sh.p.out.String()
	if i := strings.Index(out, "\r\n"); i >= 0 {
		out = out[i+2:]
	}
	return strings.TrimSuffix(out, "> ")
}

func TestRxFilterIsOffByDefault(t *testing.T) {
	b := nodetest.New(nodetest.Options{}, &probe{code: 1})
	if b.Stack.RxFilter().Active() {
		t.Fatal("a new stack filters")
	}
}

func TestRxFilterDropsByTXAfterTheMICAndBeforeAnyState(t *testing.T) {
	a := &probe{code: 1}
	b := nodetest.New(nodetest.Options{}, a)
	sh := newShell(&b.Stack)
	// Only the controller is heard (a line: controller - this node).
	if got := sh.run("rxf topo 1-66"); !strings.Contains(got, "mode topo") || !strings.Contains(got, "rxf hear 66") {
		t.Fatalf("topo: %q", got)
	}
	// The controller's request, but this copy was transmitted by node 7.
	raw := b.Inject([]byte{proto.KindRequest, ctrl, nodetest.NodeID, 9, 0, 1, 5}, 7)
	f := b.Stack.RxFilter()
	if f.Dropped != 1 || len(a.values) != 0 || len(b.Raw()) != 0 {
		t.Fatalf("dropped %d, ran %d, sent %d", f.Dropped, len(a.values), len(b.Air.Out))
	}
	// Nothing in the protocol saw it: not counted, no replay window moved.
	if st := b.Stack.Stats; st.Received != 0 || st.Rejected != 0 {
		t.Fatalf("stack stats moved: %+v", st)
	}
	if ss := b.Stack.Sec.Stats; ss.Opened != 0 || ss.Replayed != 0 || ss.AuthFailed != 0 {
		t.Fatalf("secure stats moved: %+v", ss)
	}
	if _, used := b.Stack.Sec.Window(ctrl).Last(); used {
		t.Fatal("the replay window moved for a frame the filter dropped")
	}
	// The log names it by (sender, fcnt) and its own MIC, no timestamp.
	fcnt := uint32(raw[4]) | uint32(raw[5])<<8 | uint32(raw[6])<<16 | uint32(raw[7])<<24
	want := fmt.Sprintf("d 1 sender %d tx 7 kind %02x fcnt %d rssi ? mic %x\r\n", ctrl, raw[0]&^proto.FlagSecure, fcnt, raw[len(raw)-proto.MICLen:])
	if got := sh.run("rxf log"); !strings.HasPrefix(got, want) {
		t.Fatalf("log %q, want prefix %q", got, want)
	}
	// Because the window did not move, the very same frame is still new once
	// the filter lets it through.
	sh.run("rxf all")
	b.Air.In = append(b.Air.In, raw)
	b.Stack.Poll(b.Now)
	if len(a.values) != 1 || a.values[0] != 5 {
		t.Fatalf("the frame that was dropped did not run once the filter was off: %v", a.values)
	}
}

func TestRxFilterLetsAHeardTransmitterThrough(t *testing.T) {
	a := &probe{code: 1}
	b := nodetest.New(nodetest.Options{}, a)
	sh := newShell(&b.Stack)
	sh.run("rxf topo 1-66")
	seq := b.Request(1, 3) // transmitted by the controller itself
	if got := nodetest.Answer(b.Sent(), seq); got != proto.ResultOK || len(a.values) != 1 {
		t.Fatalf("answered %d, ran %d", got, len(a.values))
	}
	if f := b.Stack.RxFilter(); f.Passed != 1 || f.Dropped != 0 {
		t.Fatalf("passed %d dropped %d", f.Passed, f.Dropped)
	}
}

func TestRxFilterLeavesAFailedMICToTheStack(t *testing.T) {
	a := &probe{code: 1}
	b := nodetest.New(nodetest.Options{}, a)
	sh := newShell(&b.Stack)
	sh.run("rxf topo 1-66")
	plain := []byte{proto.KindRequest, ctrl, nodetest.NodeID, 9, 0, 1, 5}
	raw := b.Inject(plain, 7)
	b.Stack.RxFilter().Record(rxfilter.Drop{}) // one known entry, to see that no more come
	before := b.Stack.RxFilter().Dropped
	bad := append([]byte(nil), raw...)
	bad[len(bad)-1] ^= 0x55 // a TX that is not heard, and a MIC that does not verify
	b.Air.In = append(b.Air.In, bad)
	b.Stack.Poll(b.Now)
	f := b.Stack.RxFilter()
	if f.Dropped != before || f.Unauthentic != 1 || b.Stack.Sec.Stats.AuthFailed != 1 || b.Stack.Stats.Rejected != 1 {
		t.Fatalf("dropped %d (was %d), unauth %d, secure %+v", f.Dropped, before, f.Unauthentic, b.Stack.Sec.Stats)
	}
}

func TestRxFilterKeepsADroppedFrameOutOfTheRelay(t *testing.T) {
	a := &probe{code: 1}
	b := nodetest.New(nodetest.Options{Mesh: true}, a)
	sh := newShell(&b.Stack)
	sh.run("rxf topo 1-66") // node 7 is not heard
	wake := b.Inject([]byte{proto.KindWake, ctrl, 0x09, 7, 0}, 0x07)
	b.Advance(2*proto.Second, 10*proto.Millisecond)
	if out := b.Raw(); len(out) != 0 {
		t.Fatalf("the relay forwarded a frame the filter dropped (%d frames)", len(out))
	}
	// The relay recorded nothing for it: the same frame is still a first copy.
	sh.run("rxf all")
	b.Air.In = append(b.Air.In, wake)
	b.Stack.Poll(b.Now)
	b.Advance(2*proto.Second, 10*proto.Millisecond)
	out := b.Raw()
	if len(out) != 1 || proto.Transmitter(out[0]) != nodetest.NodeID {
		t.Fatalf("after the filter was off the frame was not relayed as a first copy: %d frames", len(out))
	}
}

func TestRxFilterAsymmetricNeedsItsSwitchAndWorksOneWay(t *testing.T) {
	a := &probe{code: 1}
	b := nodetest.New(nodetest.Options{}, a)
	sh := newShell(&b.Stack)
	if got := sh.run("rxf asym nolink 7"); !strings.Contains(got, "asym is off") || b.Stack.RxFilter().Active() {
		t.Fatalf("asym without its switch: %q", got)
	}
	sh.run("rxf asym-ok yes")
	if got := sh.run("rxf asym nolink 7"); !strings.Contains(got, "NONPHYSICAL name nolink") {
		t.Fatalf("asym: %q", got)
	}
	// The controller is not in the row: this node no longer hears it, though
	// nothing says the controller does not hear this node (one way).
	b.Request(1, 3)
	if f := b.Stack.RxFilter(); f.Dropped != 1 || len(a.values) != 0 {
		t.Fatalf("dropped %d, ran %d", f.Dropped, len(a.values))
	}
}

func TestRxFilterRecordsTheFramesRSSI(t *testing.T) {
	air := &levelAir{rssi: -97}
	var s node.Stack
	s.Init(node.Config{
		Device: proto.DefaultDeviceConfig(nodetest.NodeID), NetworkKey: &nodetest.NetworkKey,
		Cipher: &nodetest.Cipher{}, Rand: proto.Midpoint{}, Air: air,
	})
	if !s.Start(0) {
		t.Fatal("start")
	}
	var peer proto.Secure
	peer.Init(&nodetest.Cipher{}, &nodetest.NetworkKey, 0, nil)
	var raw [proto.MaxFrame]byte
	n, _ := peer.SealFrame(raw[:], []byte{proto.KindHeartbeat, ctrl, nodetest.NodeID, 1, 0})
	sh := newShell(&s)
	sh.run("rxf asym-ok yes")
	sh.run("rxf asym x 7")
	air.In = append(air.In, append([]byte(nil), raw[:n]...))
	s.Poll(0)
	if got := sh.run("rxf log"); !strings.Contains(got, " rssi -97 ") {
		t.Fatalf("log: %q", got)
	}
}

type levelAir struct {
	nodetest.Air
	rssi int16
}

func (l *levelAir) PacketRSSI() (int16, bool) { return l.rssi, true }
