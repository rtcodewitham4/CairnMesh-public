// Package nodetest runs a node.Stack on the host for tests: a fake air
// with a controller on the other side (a proto.Secure with the same keys:
// it seals REQUESTs and opens what the node sends) and a fake emitter;
// module events reach the controller as EVENT frames (Acknowledge, Events). Host only (crypto/aes); never import it from firmware.
package nodetest

import (
	"cairnmesh/firmware/mesh"
	"cairnmesh/firmware/radio/sx126x"
	"crypto/aes"
	"crypto/cipher"

	"cairnmesh/firmware/node"
	"cairnmesh/firmware/proto"
	"cairnmesh/firmware/radio"
)

// The test keys of tests/vectors (vectors.NETWORK_KEY, vectors.APP_KEY).
var (
	NetworkKey = [16]byte{'0', '1', '2', '3', '4', '5', '6', '7', '8', '9', 'a', 'b', 'c', 'd', 'e', 'f'}
	AppKey     = [16]byte{0x10, 0x11, 0x12, 0x13, 0x14, 0x15, 0x16, 0x17, 0x18, 0x19, 0x1a, 0x1b, 0x1c, 0x1d, 0x1e, 0x1f}
)

// NodeID is the node's address on the bench.
const NodeID = 0x01

// Cipher is crypto/aes as a proto.Cipher.
type Cipher struct{ blocks map[[16]byte]cipher.Block }

func (c *Cipher) Encrypt(key *[16]byte, dst, src *[16]byte) {
	if c.blocks == nil {
		c.blocks = map[[16]byte]cipher.Block{}
	}
	b, ok := c.blocks[*key]
	if !ok {
		b, _ = aes.NewCipher(key[:])
		c.blocks[*key] = b
	}
	b.Encrypt(dst[:], src[:])
}

// Air queues packets: In for the node to receive, Out what it sent. It is
// quiet (radio.Quiet) when nothing waits in In and Busy is false.
type Air struct {
	In, Out [][]byte
	Refuse  bool
	Busy    bool
}

func (a *Air) Quiet() bool { return !a.Busy && len(a.In) == 0 }

func (a *Air) Send(p []byte) bool {
	if a.Refuse {
		return false
	}
	a.Out = append(a.Out, append([]byte(nil), p...))
	return true
}

func (a *Air) Receive() []byte {
	if len(a.In) == 0 {
		return nil
	}
	p := a.In[0]
	a.In = a.In[1:]
	return p
}

// Emitter records signals; one holds the radio for its Duration.
type Emitter struct {
	Signals []radio.Signal
	Until   proto.Time
	Refuse  bool
}

func (e *Emitter) Emit(s *radio.Signal, now proto.Time) bool {
	if e.Refuse {
		return false
	}
	e.Signals = append(e.Signals, *s)
	e.Until = now + s.Duration
	return true
}

func (e *Emitter) Busy(now proto.Time) bool { return now < e.Until }

// Bench is a node and its controller.
type Bench struct {
	Stack   node.Stack
	Air     Air
	Emitter Emitter
	Now     proto.Time
	// Started is what Stack.Start returned.
	Started bool

	ctrl    proto.Secure
	cipher  Cipher
	seq     byte
	sealed  [proto.MaxFrame]byte
	last    []byte
	opts    Options
	modules []node.Module
	relay   mesh.Relay
	mesh    mesh.Config
}

// Options change the bench before Start.
type Options struct {
	// Heartbeat: the device's HEARTBEAT interval; 0: off.
	Heartbeat proto.Time
	NoEmitter bool
	// Records and Marks: the node's stores kept through PowerCut.
	Records node.RecordStore
	Marks   node.MarkStore
	// Mesh: a multi-hop node, every packet through its relay (spec 17).
	Mesh bool
	// Configure, when set, changes the node.Config just before Init (after
	// Mesh): a test of a firmware's own wiring puts it here.
	Configure func(*node.Config)
}

// New builds a node with the modules and starts it at time 0.
func New(o Options, modules ...node.Module) *Bench {
	b := &Bench{opts: o, modules: modules}
	b.boot(0)
	b.ctrl.Init(&b.cipher, &NetworkKey, 0, nil)
	b.ctrl.App = true
	b.ctrl.Keys.Add(NodeID, 0, &AppKey)
	return b
}

// PowerCut restarts the node at Now from what its stores kept (its frame
// counter goes on from where it was); the controller is untouched.
func (b *Bench) PowerCut() {
	last := b.Stack.Sec.Fcnt()
	b.Stack = node.Stack{}
	b.boot(last)
}

func (b *Bench) boot(lastFcnt uint32) {
	o := b.opts
	cfg := proto.DefaultDeviceConfig(NodeID)
	cfg.Awake = true
	cfg.HeartbeatInterval = proto.Never
	if o.Heartbeat > 0 {
		cfg.HeartbeatInterval = o.Heartbeat
	}
	c := node.Config{
		Device: cfg, NetworkKey: &NetworkKey, AppKey: &AppKey,
		Cipher: &b.cipher, Rand: proto.Midpoint{}, Air: &b.Air,
		LastFcnt: lastFcnt, Records: o.Records, Marks: o.Marks,
	}
	if !o.NoEmitter {
		c.Emitter = &b.Emitter
	}
	if o.Mesh {
		b.mesh = mesh.DefaultConfig(NodeID, func(sf uint8, n int) proto.Time { return sx126x.Airtime(sf, 125000, 5, 8, n) })
		c.Relay, c.Mesh = &b.relay, &b.mesh
	}
	if o.Configure != nil {
		o.Configure(&c)
	}
	b.Stack.Init(c)
	for _, m := range b.modules {
		b.Stack.Add(m)
	}
	b.Started = b.Stack.Start(b.Now)
}

// Request sends a REQUEST from the controller and polls the node; it
// returns the message ID's SEQ.
func (b *Bench) Request(action, value byte) byte {
	b.seq++
	b.Retry(b.seq, action, value)
	return b.seq
}

// Retry sends REQUEST seq again (sealed anew: a new FCNT) and polls the node.
func (b *Bench) Retry(seq, action, value byte) {
	plain := []byte{proto.KindRequest, proto.DefaultControllerID, NodeID, seq, 0, action, value}
	n, ok := b.ctrl.SealFrame(b.sealed[:], plain)
	if !ok {
		panic("controller could not seal")
	}
	b.last = append([]byte(nil), b.sealed[:n]...)
	b.Air.In = append(b.Air.In, b.last)
	b.Stack.Poll(b.Now)
}

// Inject seals plain as the controller (FCNT and application layer as
// usual), puts it on air as transmitted by tx and polls the node; it
// returns the sealed frame.
func (b *Bench) Inject(plain []byte, tx byte) []byte {
	n, ok := b.ctrl.SealFrame(b.sealed[:], plain)
	if !ok {
		panic("controller could not seal")
	}
	raw := append([]byte(nil), b.sealed[:n]...)
	if tx != proto.DefaultControllerID {
		var out [proto.MaxFrame]byte
		m, _ := proto.Reseal(b.ctrl.Network(), out[:], raw, tx)
		raw = append([]byte(nil), out[:m]...)
	}
	b.Air.In = append(b.Air.In, raw)
	b.Stack.Poll(b.Now)
	return raw
}

// Raw is what the node put on air since the last call, sealed.
func (b *Bench) Raw() [][]byte {
	out := b.Air.Out
	b.Air.Out = nil
	return out
}

// LastSealed is the frame the controller sent last, as it went on air.
func (b *Bench) LastSealed() []byte { return b.last }

// Advance moves the clock and polls the node at every step of step.
func (b *Bench) Advance(d, step proto.Time) {
	end := b.Now + d
	for b.Now < end {
		b.Now = min(b.Now+step, end)
		b.Stack.Poll(b.Now)
	}
}

// Sent opens what the node sent since the last call (plain frames).
func (b *Bench) Sent() [][]byte {
	var out [][]byte
	var buf [proto.MaxFrame]byte
	for _, p := range b.Air.Out {
		n, why := b.ctrl.OpenFrame(buf[:], p)
		if why != proto.Accepted {
			panic("controller rejected a frame from the node")
		}
		out = append(out, append([]byte(nil), buf[:n]...))
	}
	b.Air.Out = nil
	return out
}

// Acknowledge sends the controller's network-only REPLY to the node's own
// message seq / epoch (an EVENT's acknowledgement, spec 19) and polls it.
func (b *Bench) Acknowledge(seq, epoch byte) {
	plain := []byte{proto.KindReply, proto.DefaultControllerID, NodeID, seq, epoch, proto.ResultOK}
	n, ok := b.ctrl.SealFrame(b.sealed[:], plain)
	if !ok {
		panic("controller could not seal")
	}
	b.Air.In = append(b.Air.In, append([]byte(nil), b.sealed[:n]...))
	b.Stack.Poll(b.Now)
}

// Events are the EVENT frames among frames.
func Events(frames [][]byte) [][]byte {
	var out [][]byte
	for _, f := range frames {
		if f[0] == proto.KindEvent {
			out = append(out, f)
		}
	}
	return out
}

// Answer is the RESULT field of the REPLY to seq among frames, or -1.
func Answer(frames [][]byte, seq byte) int {
	for _, f := range frames {
		if f[0] == proto.KindReply && f[3] == seq {
			return int(f[5])
		}
	}
	return -1
}
