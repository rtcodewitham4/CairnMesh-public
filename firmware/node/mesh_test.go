package node_test

import (
	"bytes"
	"testing"

	"cairnmesh/firmware/journal"
	"cairnmesh/firmware/node/nodetest"
	"cairnmesh/firmware/proto"
)

// A multi-hop node (spec 17): radio, relay, security layer, route
// endpoint, device. The relay itself is held to its vectors (mesh); these
// check the assembly.

const ctrl = proto.DefaultControllerID

func open(t *testing.T, b *nodetest.Bench, raw []byte) (plain []byte, fcnt uint32) {
	t.Helper()
	var buf [proto.MaxFrame]byte
	n, fcnt, ok := proto.OpenNetwork(b.Stack.Sec.Network(), buf[:], raw)
	if !ok {
		t.Fatalf("frame on air does not open: %x", raw)
	}
	return append([]byte(nil), buf[:n]...), fcnt
}

func TestMeshFloodedRequestRunsAndIsAnswered(t *testing.T) {
	a := &probe{code: 1}
	b := nodetest.New(nodetest.Options{Mesh: true}, a)
	seq := b.Request(1, 3)
	first := append([]byte(nil), b.Air.Out[0]...)
	if got := nodetest.Answer(b.Sent(), seq); got != proto.ResultOK || len(a.values) != 1 {
		t.Fatalf("answered %d, ran %d times", got, len(a.values))
	}
	// Nobody passed the answer on: the relay repeats it once, verbatim (the
	// same FCNT: the controller takes it for the copy it is, spec 7).
	b.Advance(2*proto.Second, 100*proto.Millisecond)
	if out := b.Raw(); len(out) != 1 || !bytes.Equal(out[0], first) || len(a.values) != 1 {
		t.Fatalf("repeat: %d frames, ran %d times", len(out), len(a.values))
	}
}

func TestMeshRelaysAFrameForAnotherNode(t *testing.T) {
	a := &probe{code: 1}
	b := nodetest.New(nodetest.Options{Mesh: true}, a)
	wake := b.Inject([]byte{proto.KindWake, ctrl, 0x09, 7, 0}, ctrl)
	if len(b.Raw()) != 0 {
		t.Fatal("relayed before its backoff")
	}
	b.Advance(2*proto.Second, 10*proto.Millisecond)
	out := b.Raw()
	if len(out) != 1 || proto.Transmitter(out[0]) != nodetest.NodeID {
		t.Fatalf("want one copy with TX = us, got %d frames", len(out))
	}
	got, fcnt := open(t, b, out[0])
	want, wantFcnt := open(t, b, wake)
	if !bytes.Equal(got, want) || fcnt != wantFcnt || len(a.values) != 0 {
		t.Fatalf("relayed %x (fcnt %d), heard %x (fcnt %d); ran %d", got, fcnt, want, wantFcnt, len(a.values))
	}
}

func TestMeshRoutedRequestAnsweredAlongTheRouteBack(t *testing.T) {
	a := &probe{code: 1}
	b := nodetest.New(nodetest.Options{Mesh: true}, a)
	// L = controller, 0x07, us; the copy comes from relay 0x07.
	b.Inject([]byte{proto.KindRequest | proto.FlagRouted, ctrl, nodetest.NodeID, 5, 0, 1, 3, 0x07, 1}, 0x07)
	out := b.Raw()
	if len(out) != 1 || len(a.values) != 1 {
		t.Fatalf("%d frames sent, ran %d times", len(out), len(a.values))
	}
	plain, _ := open(t, b, out[0])
	if plain[0] != proto.KindReply|proto.FlagRouted|proto.FlagApp || !bytes.HasSuffix(plain, []byte{0x07, 1}) {
		t.Fatalf("answer %x: want a routed REPLY back through 0x07", plain)
	}
}

func TestMeshKeepaliveAnsweredByTheEndpoint(t *testing.T) {
	a := &probe{code: 1}
	b := nodetest.New(nodetest.Options{Mesh: true}, a)
	b.Inject([]byte{proto.KindKeepalive | proto.FlagRouted, ctrl, nodetest.NodeID, 9, 0, 20, 0, 0x07, 1}, 0x07)
	out := b.Raw()
	if len(out) != 1 {
		t.Fatalf("%d frames sent", len(out))
	}
	plain, _ := open(t, b, out[0])
	want := []byte{proto.KindReply | proto.FlagRouted, nodetest.NodeID, ctrl, 9, 0, proto.ResultOK, 0x07, 1}
	if !bytes.Equal(plain, want) || b.Stack.Dev.Stats.Ignored != 0 {
		t.Fatalf("answer %x, want %x", plain, want)
	}
}

// recordMarks keeps the last mark stored per sender.
type recordMarks map[byte]uint32

func (m recordMarks) Load(*proto.Secure) proto.MarkStore { return m }
func (m recordMarks) Mark(sender byte, last uint32) bool { m[sender] = last; return true }

// Spec 5: the controller's persisted mark is the highest FCNT heard from
// it, relayed or for this node; after a power cut it seeds the relay too.
func TestMeshPersistsTheControllersHighestHeard(t *testing.T) {
	marks := recordMarks{}
	a := &probe{code: 1}
	b := nodetest.New(nodetest.Options{Mesh: true, Marks: marks}, a)
	b.Request(1, 3)
	wake := b.Inject([]byte{proto.KindWake, ctrl, 0x09, 7, 0}, ctrl)
	_, fcnt := open(t, b, wake)
	b.Stack.Flush(b.Now)
	if marks[ctrl] != fcnt {
		t.Fatalf("persisted %d, want %d (the relayed WAKE's)", marks[ctrl], fcnt)
	}

	flash := memFlash{}
	b = nodetest.New(nodetest.Options{Mesh: true, Marks: journal.NewMarks(journal.Where{Flash: flash, First: 2, Pages: 1})}, a)
	wake = b.Inject([]byte{proto.KindWake, ctrl, 0x09, 7, 0}, ctrl)
	b.Stack.Flush(b.Now)
	b.Advance(2*proto.Second, 10*proto.Millisecond)
	b.Raw()
	b.PowerCut()
	b.Air.In = append(b.Air.In, wake) // still circulating somewhere
	b.Advance(2*proto.Second, 10*proto.Millisecond)
	if out := b.Raw(); len(out) != 0 {
		t.Fatalf("the controller's old flood was relayed again after the cut: %d frames", len(out))
	}
}
