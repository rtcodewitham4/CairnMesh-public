package wiring

import (
	"bytes"
	"testing"

	"cairnmesh/firmware/board/wle5/adapt"
	"cairnmesh/firmware/board/wle5/safety"
	"cairnmesh/firmware/node"
	"cairnmesh/firmware/node/nodetest"
	"cairnmesh/firmware/proto"
	"cairnmesh/firmware/radio"
)

func TestParsePower(t *testing.T) {
	for _, c := range []struct {
		in   string
		want int8
		err  error
	}{
		{"", 0, nil}, // the default
		{"0", 0, nil}, {"-9", -9, nil}, {"+3", 3, nil}, {"10", 10, nil}, {"22", 22, nil},
		{"23", 0, ErrPowerRange}, {"-10", 0, ErrPowerRange}, {"1000", 0, ErrPowerRange}, {"99999999999999", 0, ErrPowerRange},
		{"-", 0, ErrPowerNotNumber}, {"+", 0, ErrPowerNotNumber}, {"1.5", 0, ErrPowerNotNumber}, {"abc", 0, ErrPowerNotNumber},
		{" 3", 0, ErrPowerNotNumber}, {"3 ", 0, ErrPowerNotNumber}, {"0x5", 0, ErrPowerNotNumber},
	} {
		got, err := ParsePower(c.in)
		if err != c.err || (err == nil && got != c.want) {
			t.Errorf("ParsePower(%q) = %d, %v; want %d, %v", c.in, got, err, c.want, c.err)
		}
	}
}

// The default is below the board's default cap, so the cap never lowers it; a
// request above the cap is lowered by the board layer (safety.ClampPower is
// what Radio.PowerCap runs), and without the tag wle5_allow_high_power nothing
// above 10 dBm gets through.
func TestDefaultPowerAndTheBoardCap(t *testing.T) {
	if DefaultPowerDBm != 0 {
		t.Fatalf("default %d dBm: the E77 link tests run at 0 dBm", DefaultPowerDBm)
	}
	if got := safety.ClampPower(DefaultPowerDBm, false); got != DefaultPowerDBm {
		t.Errorf("the default is capped to %d", got)
	}
	for _, c := range []struct {
		asked string
		eff   int8
	}{{"", 0}, {"-9", -9}, {"10", 10}, {"11", 10}, {"22", 10}} {
		req, err := ParsePower(c.asked)
		if err != nil {
			t.Fatal(err)
		}
		if got := safety.ClampPower(req, false); got != c.eff {
			t.Errorf("WLE5_TX_DBM=%q: effective %d, want %d", c.asked, got, c.eff)
		}
	}
	if got := safety.ClampPower(22, true); got != 22 {
		t.Errorf("with wle5_allow_high_power 22 gives %d", got)
	}
}

func TestPowerAndTxText(t *testing.T) {
	for dbm, want := range map[int8]string{0: "0 dBm", -9: "-9 dBm", 10: "10 dBm", 22: "22 dBm", 7: "7 dBm"} {
		if got := PowerText(dbm); got != want {
			t.Errorf("PowerText(%d) = %q, want %q", dbm, got, want)
		}
	}
	for _, c := range []struct {
		allowed        bool
		eff, requested int8
		want           string
	}{
		{true, 0, 0, "tx ON 0 dBm"},
		{false, 0, 0, "tx OFF (no WLE5_ANT_OK=1) 0 dBm configured"},
		{true, 10, 22, "tx ON 10 dBm (requested 22 dBm, capped by the board)"},
		{false, -9, -9, "tx OFF (no WLE5_ANT_OK=1) -9 dBm configured"},
	} {
		if got := TxText(c.allowed, c.eff, c.requested); got != c.want {
			t.Errorf("TxText = %q, want %q", got, c.want)
		}
	}
}

// The firmware's own pieces, as main has them (large: globals).
var (
	wired     Mesh
	gate      adapt.GatedAir
	gatedEmit adapt.GatedEmitter
)

// wiredNode is cmd/wle5node's node on the fake radio: built by Build, the one
// function main calls, with the gate open or closed. wrap, when given, puts a
// radio of the test's own between the fake air and the gate.
func wiredNode(allow bool, wrap func(*nodetest.Air) radio.Air, modules ...node.Module) *nodetest.Bench {
	return wiredNodeWith(nodetest.Options{}, allow, wrap, modules...)
}

// wiredNodeWith is wiredNode with the bench options under the test's control
// (for example a non-default heartbeat interval before the mesh is applied).
func wiredNodeWith(o nodetest.Options, allow bool, wrap func(*nodetest.Air) radio.Air, modules ...node.Module) *nodetest.Bench {
	configure := o.Configure
	o.Configure = func(c *node.Config) {
		if configure != nil {
			configure(c)
		}
		var air radio.Air = c.Air
		if wrap != nil {
			air = wrap(c.Air.(*nodetest.Air))
		}
		*c = Build(*c, air, c.Emitter, allow, &gate, &gatedEmit, &wired)
	}
	return nodetest.New(o, modules...)
}

func TestApplyMakesAMultiHopNode(t *testing.T) {
	cfg := node.Config{Device: proto.DefaultDeviceConfig(5)}
	var m Mesh
	m.Apply(&cfg)
	if cfg.Relay != &m.Relay || cfg.Mesh != &m.Config || cfg.Watch != &m.Watch || cfg.Discovery != &m.Discovery {
		t.Fatal("Apply left a layer out")
	}
	if cfg.Wake != nil {
		t.Error("the wake layer needs a duty-cycled receive the driver does not have yet")
	}
	if m.Config.Node != 5 || !m.Config.Forward || m.Config.BaseSF != BaseSF || m.Config.BandwidthHz != BandwidthHz {
		t.Errorf("relay config %+v", m.Config)
	}
	// The relay's airtime function is the driver's, for the node's air parameters.
	if m.Config.Airtime(BaseSF, 30) == 0 {
		t.Error("no airtime")
	}
}

// kinds opens the frames the node sent (sealed, network layer) and returns
// their plain forms.
func opened(t *testing.T, b *nodetest.Bench) [][]byte {
	t.Helper()
	var out [][]byte
	var buf [proto.MaxFrame]byte
	for _, raw := range b.Raw() {
		n, _, ok := proto.OpenNetwork(b.Stack.Sec.Network(), buf[:], raw)
		if !ok {
			t.Fatalf("frame on air does not open: %x", raw)
		}
		out = append(out, append([]byte(nil), buf[:n]...))
	}
	return out
}

func count(frames [][]byte, kind byte) int {
	n := 0
	for _, f := range frames {
		if f[0]&proto.KindMask == kind {
			n++
		}
	}
	return n
}

const ctrl = proto.DefaultControllerID

type counter struct {
	h    *node.Host
	runs int
}

func (c *counter) Start(h *node.Host) bool {
	return h.Handle(1, node.Func(func(byte) byte { c.runs++; return proto.ResultOK }))
}
func (c *counter) NextDue() proto.Time { return proto.Never }
func (c *counter) Tick(proto.Time)     {}

// The relay: a flood for another node is repeated once, verbatim but for TX.
func TestWiredNodeRelaysAFloodForAnotherNode(t *testing.T) {
	b := wiredNode(true, nil)
	if !b.Started {
		t.Fatalf("start: %+v", b.Stack.Fault)
	}
	b.Inject([]byte{proto.KindWake, ctrl, 0x09, 7, 0}, ctrl)
	b.Advance(2*proto.Second, 10*proto.Millisecond)
	out := b.Raw()
	var relayed int
	for _, raw := range out {
		if proto.Transmitter(raw) == nodetest.NodeID {
			relayed++
		}
	}
	if relayed != 1 {
		t.Fatalf("%d frames with TX = us among %d, want the one relayed copy", relayed, len(out))
	}
}

// The device is still reachable: a flooded REQUEST runs and is answered.
func TestWiredNodeStillAnswersARequest(t *testing.T) {
	c := &counter{}
	b := wiredNode(true, nil, c)
	seq := b.Request(1, 0)
	if got := nodetest.Answer(b.Sent(), seq); got != proto.ResultOK || c.runs != 1 {
		t.Fatalf("answered %d, ran %d times", got, c.runs)
	}
}

// The route endpoint (spec 17): a routed KEEPALIVE for this node is answered
// along the route back.
func TestWiredNodeIsARouteEndpoint(t *testing.T) {
	b := wiredNode(true, nil)
	b.Inject([]byte{proto.KindKeepalive | proto.FlagRouted, ctrl, nodetest.NodeID, 9, 0, 20, 0, 0x07, 1}, 0x07)
	out := opened(t, b)
	want := []byte{proto.KindReply | proto.FlagRouted, nodetest.NodeID, ctrl, 9, 0, proto.ResultOK, 0x07, 1}
	if len(out) != 1 || !bytes.Equal(out[0], want) {
		t.Fatalf("answer %x, want %x", out, want)
	}
}

// HELLO (spec 8) and JOIN (spec 12.3) come by themselves, no request needed.
func TestWiredNodeSaysHelloAndJoins(t *testing.T) {
	b := wiredNode(true, nil)
	var hellos, joins int
	for i := 0; i < 130; i++ { // 130 s
		b.Advance(proto.Second, 100*proto.Millisecond)
		out := opened(t, b)
		hellos += count(out, proto.KindHello)
		joins += count(out, proto.KindJoin)
	}
	if hellos < 1 || joins < 1 {
		t.Fatalf("in 130 s: %d HELLOs, %d JOINs", hellos, joins)
	}
}

// A multi-hop node replaces the single-hop HEARTBEAT with HELLO (spec 3 and
// 18). Keep the device's normal 3 s heartbeat interval before Build so this
// fails if Mesh.Apply stops switching it off.
func TestWiredNodeHasNoHeartbeat(t *testing.T) {
	b := wiredNodeWith(nodetest.Options{Heartbeat: 3 * proto.Second}, true, nil)
	var heartbeats, hellos int
	for i := 0; i < 130; i++ {
		b.Advance(proto.Second, 100*proto.Millisecond)
		out := opened(t, b)
		heartbeats += count(out, proto.KindHeartbeat)
		hellos += count(out, proto.KindHello)
	}
	if heartbeats != 0 {
		t.Fatalf("multi-hop node sent %d HEARTBEATs", heartbeats)
	}
	if hellos == 0 {
		t.Fatal("no HELLO: the test did not exercise the assembled multi-hop node")
	}
}

// A HELLO from a neighbour reaches the neighbour table (the layer is wired to
// what the relay hears).
func TestWiredNodeHearsANeighbour(t *testing.T) {
	b := wiredNode(true, nil)
	b.Inject([]byte{proto.KindHello, 0x09, proto.BroadcastID, 1, 0}, ctrl)
	if got := b.Stack.Watch.Neighbours(); got != 1 {
		t.Fatalf("neighbours after a HELLO: %d", got)
	}
}

// The transmit interlock holds with every layer on: behind a closed gate (no
// WLE5_ANT_OK=1) nothing the relay, HELLO or JOIN want to send reaches the
// radio, and the gate counts the refusals.
func TestClosedGateKeepsTheWholeMeshSilent(t *testing.T) {
	sig := &signaller{}
	b := wiredNode(false, nil, sig)
	b.Inject([]byte{proto.KindWake, ctrl, 0x09, 7, 0}, ctrl) // would be relayed
	b.Request(4, 0)                                          // would be answered
	b.Request(2, 0)                                          // would send a signal outside the protocol
	for i := 0; i < 130; i++ {                               // HELLO and JOIN are due
		b.Advance(proto.Second, 100*proto.Millisecond)
	}
	if out := b.Raw(); len(out) != 0 {
		t.Fatalf("%d frames reached the air behind a closed gate", len(out))
	}
	if len(b.Emitter.Signals) != 0 {
		t.Fatalf("%d signals reached the radio behind a closed gate", len(b.Emitter.Signals))
	}
	if gate.Refused == 0 || gatedEmit.Refused == 0 {
		t.Fatalf("refused: air %d, signals %d: the layers never tried to send", gate.Refused, gatedEmit.Refused)
	}
}

// The same node behind an open gate does send (the test above is not vacuous).
func TestOpenGateLetsTheMeshSpeak(t *testing.T) {
	sig := &signaller{}
	b := wiredNode(true, nil, sig)
	b.Request(2, 0)
	for i := 0; i < 130; i++ {
		b.Advance(proto.Second, 100*proto.Millisecond)
	}
	if sent := len(b.Raw()); sent == 0 || gate.Refused != 0 {
		t.Fatalf("open gate: frames sent %d, refused %d", sent, gate.Refused)
	}
	if len(b.Emitter.Signals) != 1 || gatedEmit.Refused != 0 {
		t.Fatalf("open gate: %d signals, refused %d", len(b.Emitter.Signals), gatedEmit.Refused)
	}
}

// signaller is a module whose action 2 sends a carrier outside the protocol.
type signaller struct{ h *node.Host }

func (s *signaller) Start(h *node.Host) bool {
	s.h = h
	return h.Handle(2, node.Func(func(byte) byte {
		sig := radio.Signal{Kind: radio.Carrier, PowerDBm: 0, Duration: 100 * proto.Millisecond}
		if s.h.Emit(&sig) {
			return proto.ResultOK
		}
		return proto.ResultError
	}))
}
func (s *signaller) NextDue() proto.Time { return proto.Never }
func (s *signaller) Tick(proto.Time)     {}

// busyAir is a radio with channel activity detection, as the SX126x driver
// will have it: the channel is busy until `until`.
type busyAir struct {
	*nodetest.Air
	until proto.Time
}

func (a *busyAir) BusyUntil(now proto.Time) (proto.Time, bool) { return a.until, now < a.until }

// Listen before talk (spec 7) reaches the relay through the gate: with the
// channel busy for 5 s the flood is not relayed within the backoff, and goes
// out once the channel is clear. A gate that hides BusyUntil fails this.
func TestWiredNodeHoldsBackOnABusyChannel(t *testing.T) {
	busy := &busyAir{until: 5 * proto.Second}
	b := wiredNode(true, func(a *nodetest.Air) radio.Air { busy.Air = a; return busy })
	b.Inject([]byte{proto.KindWake, ctrl, 0x09, 7, 0}, ctrl)
	b.Advance(4*proto.Second, 10*proto.Millisecond)
	if out := b.Raw(); len(out) != 0 {
		t.Fatalf("%d frames sent while the channel was busy", len(out))
	}
	b.Advance(4*proto.Second, 10*proto.Millisecond)
	relayed := 0
	for _, raw := range b.Raw() {
		if proto.Transmitter(raw) == nodetest.NodeID {
			relayed++
		}
	}
	if relayed != 1 {
		t.Fatalf("%d relayed copies once the channel cleared, want 1", relayed)
	}
}

// What the library's defaults are, pinned: the node takes them as they are
// (Mesh.Apply), so a change in mesh / node defaults must show up here, and be
// checked against the Python defaults and the vectors, before it reaches a
// node. (Values read from mesh.DefaultConfig, node.DefaultWatchConfig and
// node.DefaultDiscoveryConfig on 2026-11-07.)
func TestMeshTakesTheLibraryDefaults(t *testing.T) {
	cfg := node.Config{Device: proto.DefaultDeviceConfig(5)}
	var m Mesh
	m.Apply(&cfg)
	r := m.Config
	for _, c := range []struct {
		name      string
		got, want int64
	}{
		{"BackoffLow", int64(r.BackoffLow), int64(50 * proto.Millisecond)},
		{"BackoffHigh", int64(r.BackoffHigh), int64(1000 * proto.Millisecond)},
		{"SuppressAfter", int64(r.SuppressAfter), 3},
		{"HopRetries", int64(r.HopRetries), 1},
		{"HopTimeout", int64(r.HopTimeout), int64(1500 * proto.Millisecond)},
		{"RouteRetries", int64(r.RouteRetries), 3},
		{"RouteJitter", int64(r.RouteJitter), int64(10 * proto.Millisecond)},
		{"RouteSlack", int64(r.RouteSlack), int64(20 * proto.Millisecond)},
		{"ProbeTTL", int64(r.ProbeTTL), int64(30 * proto.Second)},
		{"WakePreamble", int64(r.WakePreamble), 8},
		{"BaseSF", int64(r.BaseSF), 7},
		{"BandwidthHz", int64(r.BandwidthHz), 125000},
		{"Forward", b2i(r.Forward), 1},
		{"GuidedReplies", b2i(r.GuidedReplies), 1},
		{"ConfirmReplies", b2i(r.ConfirmReplies), 1},
		{"LastHopAck", b2i(r.LastHopAck), 1},
		{"Watch.HelloInterval", int64(m.Watch.HelloInterval), int64(60 * proto.Second)},
		{"Watch.MissLimit", int64(m.Watch.MissLimit), 5},
		{"Watch.ReportRetries", int64(m.Watch.ReportRetries), 3},
		{"Watch.ReportTimeout", int64(m.Watch.ReportTimeout), int64(15 * proto.Second)},
		{"Join", b2i(m.Discovery.Join), 1},
		{"JoinSpread", int64(m.Discovery.JoinSpread), int64(30 * proto.Second)},
		{"JoinWindow", int64(m.Discovery.JoinWindow), 10},
		{"JoinBackoffMin", int64(m.Discovery.JoinBackoffMin), int64(30 * proto.Second)},
		{"JoinBackoffMax", int64(m.Discovery.JoinBackoffMax), int64(300 * proto.Second)},
		{"JoinByScan", b2i(m.Discovery.JoinByScan), 1},
		{"IslandBackoff", int64(m.Discovery.IslandBackoff), int64(3600 * proto.Second)},
	} {
		if c.got != c.want {
			t.Errorf("%s = %d, want %d", c.name, c.got, c.want)
		}
	}
	if m.Watch != node.DefaultWatchConfig() || m.Discovery != node.DefaultDiscoveryConfig() {
		t.Error("Apply changed a Watch / Discovery default")
	}
}

func b2i(b bool) int64 {
	if b {
		return 1
	}
	return 0
}
