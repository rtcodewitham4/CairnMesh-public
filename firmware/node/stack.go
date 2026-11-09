package node

import (
	"cairnmesh/firmware/mesh"
	"cairnmesh/firmware/proto"
	"cairnmesh/firmware/radio"
)

// Table sizes.
const (
	MaxModules = 8
	MaxActions = 32
	MaxClaims  = 32
)

// Config is what a node is built from. The keys must stay at a fixed
// address (the Stack keeps the pointers).
type Config struct {
	Device     proto.DeviceConfig
	NetworkKey *[16]byte
	// AppKey is the node's application key (version 0); nil turns the
	// application layer off.
	AppKey *[16]byte
	// AppKeyVersion is the KEYVER of AppKey (spec 15.4).
	AppKeyVersion byte
	// LastFcnt and Store: the stored FCNT bound and where reservations go
	// (spec section 5); Store nil: no persistence (tests).
	LastFcnt uint32
	Store    proto.CounterStore
	// Records: the device's duplicate records kept through a power cut
	// (spec 4.1), Marks: the replay marks (spec 5); nil: RAM only (tests).
	// The board's are journal.Records and journal.Marks (board.Journals).
	Records RecordStore
	Marks   MarkStore
	Cipher  proto.Cipher
	Rand    proto.Rand
	Air     radio.Air
	// Emitter: nil when the radio cannot send signals outside the protocol.
	Emitter radio.Emitter
	// Events: the module events' timings (spec 18); zero: the defaults.
	Events proto.EventConfig
	// Relay and Mesh make a multi-hop node (spec 17): every packet goes
	// through the relay, which hands frames for this node up; nil: single
	// hop, the air is the link (the serial bench). Relay is large: a global.
	Relay *mesh.Relay
	Mesh  *mesh.Config
	// The layers above a multi-hop node's relay (spec 17), each nil when
	// absent: Watch the HELLOs and neighbour table (spec 8), Wake the
	// dormant / awake state (14.2; the air then implements Sampler),
	// Discovery the JOIN and scan answers (12.3).
	Watch     *WatchConfig
	Wake      *WakeConfig
	Discovery *DiscoveryConfig
	// Epoch names this run in the node's message ids; FixedEpoch false:
	// drawn at Start (the board), true: Epoch (the vectors: the node's id).
	Epoch      byte
	FixedEpoch bool
}

// RecordStore reads the device's records back into it (after Init) and
// returns where new ones go.
type RecordStore interface {
	Load(d *proto.Device) proto.RecordLog
}

// MarkStore reads the replay marks back and returns where new ones go.
type MarkStore interface {
	Load(s *proto.Secure) proto.MarkStore
}

// Preparer is a store that erases flash ahead of its next compaction
// (journal.Records, journal.Marks): Poll gives it one page at a time while
// the air is quiet (radio.Quiet), so a compaction does not stall the node.
type Preparer interface {
	Prepare() bool
	Unprepared() bool
}

// FaultReason says why Start stopped the boot.
type FaultReason uint8

const (
	NoFault FaultReason = iota
	// ModuleFailed: the module's Start returned false.
	ModuleFailed
	// TooManyModules: more than MaxModules were added.
	TooManyModules
	// ActionTaken: another module already handles the action (Detail).
	ActionTaken
	// ActionReserved: the action (Detail) is the protocol's (QueryAction).
	ActionReserved
	// TooManyActions: past MaxActions.
	TooManyActions
	// ResourceTaken: another module already claimed the resource (Detail).
	ResourceTaken
	// TooManyClaims: past MaxClaims.
	TooManyClaims
)

// Fault is the first thing that stopped the boot: which module (its index
// in the order added), why, and the action or resource involved.
type Fault struct {
	Module int
	Reason FaultReason
	Detail uint16
}

// Stats counts what the Stack did besides the protocol's own counters
// (Secure.Stats, Device.Stats).
type Stats struct {
	Received, Rejected uint32
	// Unsent: frames the air refused; HeldBySignal: frames not sent because
	// a signal held the radio (the protocol's retries cover both).
	Unsent, HeldBySignal    uint32
	Signals, SignalsRefused uint32
	Notified, NotifyDropped uint32
	// UnknownAction: requests for an action no module handles (answered ERROR).
	UnknownAction uint32
}

type actionEntry struct {
	action Action
	code   byte
	owner  int8
}

type claim struct {
	resource Resource
	owner    int8
}

// Stack is one node. It is large (the protocol's tables): keep it in a
// global, never on the stack or in a composite literal.
type Stack struct {
	Sec proto.Secure
	Dev proto.Device
	// Events is the queue of module events (Host.Notify, spec 19).
	Events proto.Events

	air     radio.Air
	emitter radio.Emitter
	events  proto.EventConfig
	device  proto.DeviceConfig
	records RecordStore
	rand    proto.Rand
	relay   *mesh.Relay
	port    meshPort
	end     routeEnd
	layers  Config // Watch, Wake, Discovery, Epoch as given
	direct  direct

	// Watch, Waker and Disc are the layers above the relay (Config.Watch,
	// Wake, Discovery); used only when configured.
	Watch  Watch
	Waker  Waker
	Disc   Discovery
	routed [proto.MaxFrame]byte
	reply  [proto.MaxFrame]byte
	prep   [2]Preparer
	nPrep  int

	modules  [MaxModules]Module
	hosts    [MaxModules]Host
	nModules int
	actions  actionTable
	claims   [MaxClaims]claim
	nClaims  int

	rx       rxHook // the receive test filter (rxhook_on.go); empty in every default build
	out      outbound
	plain    [proto.MaxFrame]byte
	sealed   [proto.MaxFrame]byte
	now      proto.Time
	signalOn bool // a signal was started and Busy has not turned false yet
	started  bool

	Fault Fault
	Stats Stats
}

// Init sets the protocol up; modules come next (Add), then Start.
func (s *Stack) Init(cfg Config) {
	s.air, s.emitter, s.events = cfg.Air, cfg.Emitter, cfg.Events
	if s.events == (proto.EventConfig{}) {
		s.events = proto.DefaultEventConfig()
	}
	s.device, s.rand = cfg.Device, cfg.Rand
	s.Sec.Init(cfg.Cipher, cfg.NetworkKey, cfg.LastFcnt, cfg.Store)
	if cfg.Marks != nil {
		marks := cfg.Marks.Load(&s.Sec)
		s.Sec.SetMarks(marks, cfg.Device.Controller)
		s.addPreparer(marks)
	}
	s.records = cfg.Records
	if cfg.AppKey != nil {
		s.Sec.App = true
		s.Sec.Keys.Add(cfg.Device.ID, cfg.AppKeyVersion, cfg.AppKey)
	}
	s.out.s = s
	s.actions.s = s
	if cfg.Relay != nil && cfg.Mesh != nil {
		// Radio, relay, security layer, route endpoint, device (spec 17). The
		// relay's own frames share the one FCNT counter (Sealer); its windows
		// were in RAM, the controller's comes back from the persisted mark.
		mc := *cfg.Mesh
		mc.Node = cfg.Device.ID
		s.relay, s.port.s = cfg.Relay, s
		s.relay.Init(mc, s.Sec.Network(), &s.port, &s.port, cfg.Rand)
		s.relay.Sealer = &s.Sec
		mark, ok := s.Sec.Mark(cfg.Device.Controller)
		s.relay.Reboot(cfg.Device.Controller, mark, ok)
		s.relay.Observe = &s.port
		s.layers = Config{Watch: cfg.Watch, Wake: cfg.Wake, Discovery: cfg.Discovery, Epoch: cfg.Epoch, FixedEpoch: cfg.FixedEpoch}
		s.direct.s = s
	}
}

// Started reports whether Start succeeded (the protocol runs).
func (s *Stack) Started() bool { return s.started }

// Add appends a module; false (and a Fault) past MaxModules.
func (s *Stack) Add(m Module) bool {
	if s.nModules >= MaxModules {
		s.fail(s.nModules, TooManyModules, 0)
		return false
	}
	s.modules[s.nModules] = m
	s.hosts[s.nModules] = Host{s: s, index: int8(s.nModules)}
	s.nModules++
	return true
}

// Start starts the modules in order, then the device endpoint; false when
// a module could not start or two of them want the same action or
// resource (Fault says which). A node that fails to start must not join
// the network: the caller shows the fault and stops.
func (s *Stack) Start(now proto.Time) bool {
	s.now = now
	for i := 0; i < s.nModules && s.Fault.Reason == NoFault; i++ {
		if !s.modules[i].Start(&s.hosts[i]) && s.Fault.Reason == NoFault {
			s.fail(i, ModuleFailed, 0)
		}
	}
	if s.Fault.Reason != NoFault {
		return false
	}
	s.Dev.Init(s.device, &s.actions, &s.out, s.rand, now)
	if s.records != nil {
		log := s.records.Load(&s.Dev)
		s.Dev.Reset(now) // the records read back count from now; open ones report again
		s.Dev.SetLog(log)
		s.addPreparer(log)
	}
	// This run's EPOCH and the counter's start for the node's own messages
	// (the board has no clock that survives a reboot: drawn).
	epoch, seq := byte(s.rand.Uniform(0, 255)), byte(s.rand.Uniform(0, 255))
	if s.layers.FixedEpoch {
		epoch = s.layers.Epoch
	}
	s.Events.Init(s.events, s.device.ID, s.device.Controller, epoch, seq, s.rand)
	if s.relay != nil {
		s.bootLayers(epoch, now)
	}
	s.started = true
	return true
}

// bootLayers starts the layers above the relay as node.NodeStack.boot
// does: the table and the join state are new, and a node booting in a
// sleeping network stays awake long enough to JOIN and hear the answer (Q22).
func (s *Stack) bootLayers(epoch byte, now proto.Time) {
	id, ctrl := s.device.ID, s.device.Controller
	if c := s.layers.Watch; c != nil {
		s.Watch.init(*c, id, ctrl, epoch, &s.Events, s.rand, &s.Dev, &s.direct, now)
	}
	if c := s.layers.Wake; c != nil {
		sampler, _ := s.air.(Sampler)
		s.Waker.init(*c, s.relay, sampler, id, s.onWake, now)
	}
	if c := s.layers.Discovery; c != nil {
		var watch *Watch
		if s.layers.Watch != nil {
			watch = &s.Watch
		}
		s.Disc.init(*c, id, ctrl, epoch, byte(s.rand.Uniform(0, 255)), s.relay, s.rand, &s.Dev, watch, &s.direct)
		s.Disc.now = now
		s.Disc.start(now)
		s.relay.Path = &s.Disc
		if s.layers.Wake != nil && c.JoinWakes {
			// Awake through the JOIN spread, its window and the answer's way back.
			until := now + c.JoinSpread + proto.Time(c.JoinWindow)*mesh.WindowUnit + BootAnswerWait
			s.Disc.bootUntil = until
			s.Waker.boot(now, until)
		}
	}
}

// BootAnswerWait (Q22, node.BOOT_ANSWER_WAIT): how long a node booting in a
// sleeping network stays awake past its JOIN's window.
const BootAnswerWait = 15 * proto.Second

func (s *Stack) onWake(now proto.Time) {
	if s.layers.Watch != nil {
		s.Watch.resume(now)
	}
	s.Events.Resume(now)
}

// Awake reports whether the protocol above the relay runs (a dormant node
// only samples for preambles: the relay and the wake layer).
func (s *Stack) Awake() bool { return s.layers.Wake == nil || s.Waker.Awake() }

func (s *Stack) fail(module int, why FaultReason, detail uint16) {
	if s.Fault.Reason == NoFault {
		s.Fault = Fault{Module: module, Reason: why, Detail: detail}
	}
}

// Poll does everything due at now: frames received, the device's timers,
// the modules' timers.
func (s *Stack) Poll(now proto.Time) {
	s.now = now
	if !s.started {
		return
	}
	s.Waker.now, s.Disc.now = now, now
	for {
		packet := s.air.Receive()
		if packet == nil {
			break
		}
		if s.rxDrop(packet) {
			continue // the test filter (tag cairn_testfilter): never true in a default build
		}
		if s.relay == nil {
			s.deliver(packet)
			continue
		}
		rssi := mesh.NoRSSI
		if l, ok := s.air.(radio.Levels); ok {
			if dbm, ok := l.PacketRSSI(); ok {
				rssi = mesh.DBm(int(dbm))
			}
		}
		s.relay.OnRadio(packet, rssi, now) // frames for us come back through deliver
		ctrl := s.device.Controller
		if mark, ok := s.relay.Mark(ctrl); ok {
			s.Sec.NoteHeard(ctrl, mark) // persisted too (spec 5)
		}
	}
	if s.relay != nil {
		s.relay.Tick(now)
	}
	if s.layers.Wake != nil {
		s.Waker.tick(now) // after the relay: asleep only once it has sent this pass's frames
	}
	s.Sec.FlushMarks(now, false)
	if s.Awake() {
		if now >= s.Dev.NextDue() {
			s.Dev.Tick(now)
		}
		if s.layers.Watch != nil {
			s.Watch.tick(now)
		}
		if now >= s.Events.NextDue() {
			s.Events.Tick(now, &s.out)
		}
		if s.layers.Discovery != nil {
			s.Disc.tick(now)
		}
	}
	for i := 0; i < s.nModules; i++ {
		if now >= s.modules[i].NextDue() {
			s.modules[i].Tick(now)
		}
	}
	if s.quiet() {
		for i := 0; i < s.nPrep; i++ {
			if s.prep[i].Prepare() {
				break // one page (about 22 ms) per pass
			}
		}
	}
}

func (s *Stack) addPreparer(store any) {
	if p, ok := store.(Preparer); ok && s.nPrep < len(s.prep) {
		s.prep[s.nPrep] = p
		s.nPrep++
	}
}

// quiet reports whether the air can tell it is quiet and is.
func (s *Stack) quiet() bool {
	q, ok := s.air.(radio.Quiet)
	return ok && q.Quiet()
}

// unprepared reports whether a store has flash to erase ahead.
func (s *Stack) unprepared() bool {
	for i := 0; i < s.nPrep; i++ {
		if s.prep[i].Unprepared() {
			return true
		}
	}
	return false
}

// deliver opens a sealed frame for this node and hands it to the device
// (through the route endpoint on a multi-hop node).
func (s *Stack) deliver(raw []byte) {
	n, why := s.Sec.OpenFrame(s.plain[:], raw)
	if why != proto.Accepted {
		s.Stats.Rejected++
		return
	}
	s.Stats.Received++
	plain := s.plain[:n]
	if s.relay != nil {
		up, ok, reply := s.end.in(s.device.ID, plain, s.now, s.reply[:])
		if reply != nil {
			s.out.sendPlain(reply)
		}
		if !ok {
			return
		}
		plain = up
	}
	if !s.Sec.LastApp {
		s.Events.OnAck(plain)
	}
	s.Dev.OnFrame(plain, s.now)
}

// NextDue is the earliest time Poll has a timer to run (a frame arriving
// earlier is handled at once): the MCU may sleep until then.
func (s *Stack) NextDue() proto.Time {
	due := s.Sec.MarksDue()
	if s.relay != nil {
		due = min(due, s.relay.NextDue())
	}
	if s.layers.Wake != nil {
		due = min(due, s.Waker.nextDue())
	}
	if s.Awake() {
		// Dormant, the device, the table, the events and discovery do not run.
		due = min(due, s.Dev.NextDue(), s.Events.NextDue())
		if s.layers.Watch != nil {
			due = min(due, s.Watch.nextDue(s.now))
		}
		if s.layers.Discovery != nil {
			due = min(due, s.Disc.nextDue())
		}
	}
	if s.started && s.unprepared() && s.quiet() {
		due = s.now // flash to erase ahead while the air is quiet
	}
	for i := 0; i < s.nModules; i++ {
		due = min(due, s.modules[i].NextDue())
	}
	return due
}

// Flush stores what is waiting for its interval (the replay marks): call
// it before a reboot.
func (s *Stack) Flush(now proto.Time) { s.Sec.FlushMarks(now, true) }

// signaling reports whether a signal holds the radio at now.
func (s *Stack) signaling(now proto.Time) bool {
	if !s.signalOn {
		return false
	}
	if s.emitter.Busy(now) {
		return true
	}
	s.signalOn = false
	return false
}

// outbound seals the device's frames and puts them on the air.
type outbound struct{ s *Stack }

func (o *outbound) Send(plain []byte) {
	s := o.s
	if s.relay != nil {
		plain = s.end.out(plain, s.routed[:]) // the route back, if it came routed
	}
	o.sendPlain(plain)
}

// sendPlain seals a frame and sends it: through the relay (which repeats it
// until a copy acknowledges it) or straight on the air.
func (o *outbound) sendPlain(plain []byte) {
	s := o.s
	if s.signaling(s.now) {
		s.Stats.HeldBySignal++ // checked before sealing: no counter spent
		return
	}
	n, ok := s.Sec.SealFrame(s.sealed[:], plain)
	if !ok {
		return // the counter could not be reserved: the frame must not go out
	}
	if s.relay != nil {
		s.relay.Send(s.sealed[:n], s.now)
		return
	}
	if !s.air.Send(s.sealed[:n]) {
		s.Stats.Unsent++
	}
}

// meshPort is the relay's radio and receiver: the node's air, held while a
// signal is on (Stack.signaling), and the way up to deliver.
type meshPort struct{ s *Stack }

func (p *meshPort) Put(raw []byte, preamble uint16) {
	s := p.s
	if s.signaling(s.now) {
		s.Stats.HeldBySignal++ // the relay's repeats cover it
		return
	}
	if l, ok := s.air.(radio.LongPreamble); ok && preamble != 8 {
		if !l.SendPreamble(raw, preamble) {
			s.Stats.Unsent++
		}
		return
	}
	if !s.air.Send(raw) {
		s.Stats.Unsent++
	}
}

func (p *meshPort) Up(raw []byte) { p.s.deliver(raw) }

// Heard is the relay's observer (mesh.Observer), shown to the layers in
// node.py's order: the neighbour table, the wake layer, discovery.
func (p *meshPort) Heard(plain []byte, rssi mesh.RSSI) {
	s := p.s
	if s.layers.Watch != nil {
		s.Watch.heard(plain, rssi, s.now)
	}
	if s.layers.Wake != nil {
		s.Waker.heard(plain, s.now)
	}
	if s.layers.Discovery != nil {
		s.Disc.heard(plain, rssi, s.now)
	}
}

// direct seals and sends the layers' own frames (HELLO, REPORT, JOIN, scan
// answers) without the route endpoint (node.py: they sit on the transport).
type direct struct{ s *Stack }

func (d *direct) Send(plain []byte) { d.s.out.sendPlain(plain) }

func (p *meshPort) BusyUntil(now proto.Time) (proto.Time, bool) {
	if b, ok := p.s.air.(mesh.Busy); ok {
		return b.BusyUntil(now)
	}
	return 0, false
}

func (p *meshPort) SetRadio(channel, sf uint8) {
	if t, ok := p.s.air.(mesh.Tuner); ok {
		t.SetRadio(channel, sf)
	}
}

// actionTable is the device's proto.Application: ACTION picks a module's
// Action.
type actionTable struct {
	s       *Stack
	entries [MaxActions]actionEntry
	n       int
	// current is the request being performed (Host.Ticket).
	current Ticket
}

func (t *actionTable) find(code byte) *actionEntry {
	for i := 0; i < t.n; i++ {
		if t.entries[i].code == code {
			return &t.entries[i]
		}
	}
	return nil
}

func (t *actionTable) Duration(action, value byte) proto.Time {
	if e := t.find(action); e != nil {
		return e.action.Duration(value)
	}
	return 0
}

func (t *actionTable) Perform(r proto.Request) byte {
	e := t.find(r.Action)
	if e == nil {
		t.s.Stats.UnknownAction++
		return proto.ResultError
	}
	t.current = Ticket{sender: r.Sender, id: r.ID, valid: true}
	result := e.action.Perform(r.Value)
	t.current = Ticket{}
	return result
}
