// Package mesh is one node's relay: managed flooding over a radio whose
// topology nobody knows (spec section 7) and routed sessions on top of it
// (11.2), ported from cairnmesh mesh.py (MeshRelay) and held to its
// behaviour vectors (tests/vectors/lora_relay_behaviour.json, spec 16.2).
//
// The relay sits between the radio and the security layer: it opens every
// sealed frame with the network key, keeps one forwarding window per origin
// (RAM only: spec 7, only the controller's is seeded after a reboot),
// relays each transmission at most once after a backoff that grows with
// RSSI, lets covered neighbourhoods suppress outward floods, guides replies
// back along the requests' backbone, and repeats the node's own frames
// until a copy acknowledges them. Routed frames go only through the relays
// they list, each hop acknowledged by the next one's copy. Frames for this
// node go up sealed, once.
//
// Pure Go, no heap after Init: the host tests replay the vectors, the board
// gives it the radio (Radio) and the clock (the caller's now).
package mesh

import "cairnmesh/firmware/proto"

// Radio puts frames on the air.
type Radio interface {
	// Put transmits one sealed frame with a preamble of that many symbols
	// (8: the standard one; longer: a wake preamble, spec 14.3).
	Put(raw []byte, preamble uint16)
}

// Busy is implemented by a Radio with channel activity detection: until
// when it is receiving or sending (ok false: the channel is clear). Every
// frame then listens before it talks (spec 7).
type Busy interface {
	BusyUntil(now proto.Time) (until proto.Time, ok bool)
}

// Receiver takes a sealed frame for this node (or broadcast), first copy only.
type Receiver interface {
	Up(raw []byte)
}

// Sealer seals a plain frame the relay originates (a route error) under the
// node's one FCNT counter (proto.Secure.SealFrame, spec 17); ok false: the
// counter could not be reserved, the frame does not go out.
type Sealer interface {
	SealFrame(dst, plain []byte) (int, bool)
}

// RouteFailed hears of a routed frame this node originated whose next hop
// never acknowledged it (the gateway's session layer re-probes).
type RouteFailed interface {
	RouteFailed(plain []byte, next byte)
}

// Observer is shown every authenticated first copy heard (mesh.py
// observers), before a frame for this node goes up: the neighbour table,
// the wake and discovery layers read the air through it.
type Observer interface {
	Heard(plain []byte, rssi RSSI)
}

// RSSI is a signal level in 1/1024 dBm (fixed point: the MCUs have no FPU;
// fine enough that the smoothed level rounds like mesh.py's floats).
type RSSI int32

// DBm is a whole dBm level as an RSSI.
func DBm(dbm int) RSSI { return RSSI(dbm * 1024) }

// NoRSSI marks a frame without a measured level (the layer above's own).
const NoRSSI RSSI = -1 << 30

// Config is the relay's behaviour (mesh.py defaults; spec 18).
type Config struct {
	// Node is this node's id; Forward false makes a pure endpoint (the
	// gateway): it filters duplicates and tracks acknowledgements, never relays.
	Node    byte
	Forward bool
	// BackoffLow, BackoffHigh: a relay waits low + (high - low) × share,
	// share = RSSIWeight × closeness + (1 - RSSIWeight) × random (ppm).
	BackoffLow, BackoffHigh proto.Time
	RSSIWeak, RSSIStrong    RSSI
	RSSIWeightPPM           int32
	// SuppressAfter other copies of an outward flood heard during the
	// backoff cancel the relay (0: never).
	SuppressAfter uint8
	// GuidedReplies: a REPLY is relayed only by nodes that relayed its
	// request; ConfirmReplies: a relayed REPLY waits for a copy and repeats.
	GuidedReplies, ConfirmReplies bool
	// HopRetries repeats of an unacknowledged flooded frame, HopTimeout apart.
	HopRetries uint8
	HopTimeout proto.Time
	// MaxPending frames waiting (at most MaxFlights).
	MaxPending int
	// Routed hops (11.2): RouteJitter (also listen before talk's), the
	// slack and the target's answer time in the hop timeout, RouteRetries
	// repeats before a ROUTE_ERROR; LastHopAck: the gateway echoes an
	// inbound frame's last hop instead of it going twice, blind.
	RouteJitter, RouteSlack, RouteAnswer proto.Time
	RouteRetries                         uint8
	LastHopAck                           bool
	// LinkWeightPPM of a new copy in a neighbour's smoothed RSSI (0: links off).
	LinkWeightPPM int32
	// NoiseFloor: SNR = RSSI - NoiseFloor where the radio gives no SNR.
	NoiseFloor RSSI
	// Probes (11.3): wait ProbeBackoffLow .. High by the hop's cost (ETX
	// above 1, up to ProbeCostSpan), repeat ProbeRetries times when nobody
	// passes it on, forget it after ProbeTTL. ETX: a link is barely usable at
	// ProbeWeakSNR dB; ETXSigma, ETXSafety (dB) and ETXBreak (sends) as mesh.py.
	ProbeBackoffLow, ProbeBackoffHigh, ProbeTTL proto.Time
	ProbeRetries                                uint8
	ProbeWeakSNR, ProbeCostSpan                 float64
	ETXSigma, ETXSafety, ETXBreak               float64
	// Waking (14.3): WakePreamble symbols on every WAKE_NET copy (8: off),
	// the originator's WakeRetries, a relay's WakeCopies while a neighbour
	// at least WakeExpect stays unheard; WakeBackoffLow .. High (0: Backoff)
	// for wake relays; WakeProbeRetries for a wake probe.
	WakePreamble                    uint16
	WakeRetries, WakeCopies         uint8
	WakeBackoffLow, WakeBackoffHigh proto.Time
	WakeExpect                      RSSI
	WakeProbeRetries                uint8
	// Sessions (15.2): the common setting is channel 0 at BaseSF; RadioTimeout
	// of silence on a session's setting brings the relay home.
	BaseSF       uint8
	RadioTimeout proto.Time
	BandwidthHz  uint32
	// Airtime of an n-byte frame at sf with the standard preamble.
	Airtime func(sf uint8, n int) proto.Time
}

// DefaultConfig is mesh.py's MeshRelay defaults at SF7 (airtime is the caller's).
func DefaultConfig(node byte, airtime func(sf uint8, n int) proto.Time) Config {
	return Config{
		Node:             node,
		Forward:          true,
		BackoffLow:       50 * proto.Millisecond,
		BackoffHigh:      1000 * proto.Millisecond,
		RSSIWeak:         DBm(-120),
		RSSIStrong:       DBm(-40),
		RSSIWeightPPM:    700000,
		SuppressAfter:    3,
		GuidedReplies:    true,
		ConfirmReplies:   true,
		HopRetries:       1,
		HopTimeout:       1500 * proto.Millisecond,
		MaxPending:       MaxFlights,
		RouteJitter:      10 * proto.Millisecond,
		RouteSlack:       20 * proto.Millisecond,
		RouteAnswer:      20 * proto.Millisecond,
		RouteRetries:     3,
		LastHopAck:       true,
		LinkWeightPPM:    250000,
		NoiseFloor:       DBm(-117),
		ProbeBackoffLow:  20 * proto.Millisecond,
		ProbeBackoffHigh: 600 * proto.Millisecond,
		ProbeTTL:         30 * proto.Second,
		ProbeRetries:     1,
		ProbeWeakSNR:     -7.5,
		ProbeCostSpan:    4,
		ETXSigma:         3,
		ETXSafety:        3,
		ETXBreak:         200,
		WakePreamble:     8,
		WakeRetries:      3,
		WakeCopies:       3,
		WakeExpect:       RSSI(-121.5 * 1024),
		BaseSF:           7,
		RadioTimeout:     6 * proto.Second,
		BandwidthHz:      125000,
		Airtime:          airtime,
	}
}

const (
	// MaxFlights frames waiting to be relayed or acknowledged (spec 9).
	MaxFlights = 8
	// MaxPaths requests relayed lately: the replies' backbone (spec 9).
	MaxPaths = 8
	// MaxLinks neighbours with a smoothed RSSI (spec 9: link_memory).
	MaxLinks = 16
	// MaxDone routed frames passed on and acknowledged (to answer a retry).
	MaxDone = 8
	// MaxHolds answers held back for the last relay's copy.
	MaxHolds = 2
	ppm      = 1000000
)

// requestID names a request (or report) and its reply alike: asker,
// answerer, SEQ, EPOCH.
type requestID [4]byte

// route is L = SENDER, R1 .. RN, TARGET of a routed frame.
type route struct {
	nodes [proto.MaxRelays + 2]byte
	n     int
}

func routeOf(plain []byte) (route, bool) {
	var rt route
	_, relays, ok := proto.SplitRoute(plain)
	if !ok {
		return rt, false
	}
	rt.nodes[0] = plain[1]
	rt.n = 1 + copy(rt.nodes[1:], relays)
	rt.nodes[rt.n] = plain[2]
	rt.n++
	return rt, true
}

func (rt *route) index(node byte) int {
	for i := 0; i < rt.n; i++ {
		if rt.nodes[i] == node {
			return i
		}
	}
	return -1
}

type flight struct {
	raw    [proto.MaxFrame]byte
	n      int
	origin byte
	fcnt   uint32
	due    proto.Time
	// request: a REPLY to it cancels this flight (relays waiting) or
	// acknowledges it (our own request); none for frames heading home.
	request    requestID
	hasRequest bool
	// id: the request this frame is or answers (requestOf).
	id requestID
	// suppressible: hearing SuppressAfter copies before sending cancels it.
	suppressible bool
	// confirm: after sending, wait for an implicit acknowledgement and
	// repeat when none comes.
	confirm  bool
	heard    uint8
	sent     uint8
	preamble uint16
	// Routed frames: the route, our place in it, the hop timeout and repeats.
	routed     bool
	rt         route
	index      int
	timeout    proto.Time
	hasTimeout bool
	retries    uint8
	hasRetries bool
	sentAt     proto.Time
	// blind: the last hop into the gateway, sent twice a short gap apart.
	blind bool
	// answer: a one-off copy answering a retry from behind.
	answer bool
	// onDemand: the target's reply, sent again only when the last relay
	// repeats the request (demanded), until linger; each repeat heard moves
	// linger to at least lingerSpan past it.
	onDemand, demanded bool
	linger, lingerSpan proto.Time
	// failed: repeats ran out; Tick reports it once every send has gone.
	failed bool
	// created: when we took it up (a wake relay repeats for neighbours
	// unheard since); onAck: our own WAKE_NET, WakeHeard when passed on.
	created proto.Time
	onAck   bool
	// logKey: the route log entry this routed flight reports to.
	logKey logKey
	hasLog bool
}

type link struct {
	node  byte
	level RSSI
}

type done struct {
	origin   byte
	fcnt     uint32
	furthest int
	answered bool
}

type hold struct {
	id    requestID
	until proto.Time
}

// Stats counts what happened to every frame (mesh.py stats).
type Stats struct {
	Originated, Repeated, Relayed, Suppressed, Answered, Duplicates   uint32
	NotOnPath, Overflow, AuthFailed, PlainDropped                     uint32
	Routed, NotOnRoute, BadRoute, HopAcked, Overtaken, RetryAnswered  uint32
	Echoed, RouteErrors, RouteDropped, Unsealed                       uint32
	ProbesRelayed, ProbesAnswered, ProbesIgnored                      uint32
	AsleepDropped, WakesRelayed, WakesRefused, WakeRepeats, WakesDone uint32
	RadioSwitches, RadioReverted                                      uint32
	TracesAnswered, TracesRelayed                                     uint32
}

// Relay is one node's relay. The zero value is unusable: Init first.
// Keep it in a global: it is large (MaxFlights frames).
type Relay struct {
	// Sealer: frames the relay originates (nil: none go out, counted).
	Sealer Sealer
	// Failed hears of this node's routed frames that found no next hop.
	Failed RouteFailed
	// Path hears of the JOINs and DISCOVERs collected here.
	Path PathFound
	// Asleep (wake.WakeAgent): only frames that may wake the node are taken
	// (a CAD look caught one), and nothing else is recorded.
	Asleep bool
	Gate   WakeGate
	Heard  WakeHeard
	// Observe: the layers above that read the air (node.Stack).
	Observe Observer

	cfg   Config
	ccm   proto.CCM
	radio Radio
	up    Receiver
	rand  proto.Rand

	windows       [256]proto.ReplayWindow
	flights       [MaxFlights]flight
	nFlights      int
	paths         [MaxPaths]requestID
	nPaths        int
	nextPath      int
	links         [MaxLinks]link // least recently heard first
	nLinks        int
	heardAt       [256]proto.Time
	heardAny      proto.Time
	done          [MaxDone]done // oldest first
	nDone         int
	holds         [MaxHolds]hold // oldest first
	nHolds        int
	probes        [MaxProbes]probe // oldest first
	nProbes       int
	setting       radioSetting
	radioPending  [MaxRadioPending]radioPending
	nRadioPending int
	radioFlights  [MaxFlights]radioFlight
	nRadioFlights int
	wakeHeardLast proto.Time
	routeLog      [MaxRouteLog]hopRecord // oldest first
	nLog          int
	traces        [MaxTraces]traceQuery // oldest first
	nTraces       int
	traceOut      [MaxTraces]traceOut
	nTraceOut     int

	plain    [proto.MaxFrame]byte
	scratch  [proto.MaxFrame]byte
	linkBuf  [proto.MaxRelays + 1]Link
	probeBuf [maxPath]byte
	broken   flight
	Stats    Stats
}

// Init sets the relay up: the network key's CCM, the radio, where frames
// for this node go, and the random source (proto.Midpoint for the vectors).
func (r *Relay) Init(cfg Config, ccm proto.CCM, radio Radio, up Receiver, rand proto.Rand) {
	if cfg.MaxPending <= 0 || cfg.MaxPending > MaxFlights {
		cfg.MaxPending = MaxFlights
	}
	r.cfg, r.ccm, r.radio, r.up, r.rand = cfg, ccm, radio, up, rand
	r.heardAny, r.wakeHeardLast = -proto.Never, -proto.Never
	r.setting = r.home()
	for i := range r.heardAt {
		r.heardAt[i] = -proto.Never
	}
}

// Reboot: the windows were in RAM and start empty; the controller's comes
// back from its persisted mark (spec 5, 7), so its old floods are not
// relayed again.
func (r *Relay) Reboot(controller byte, mark uint32, marked bool) {
	for i := range r.windows {
		r.windows[i] = proto.ReplayWindow{}
	}
	if marked {
		r.windows[controller] = proto.RestoredWindow(mark)
	}
}

// Mark is the highest FCNT heard from origin, relayed or not (the
// controller's is persisted, spec 5).
func (r *Relay) Mark(origin byte) (uint32, bool) { return r.windows[origin].Last() }

func kindOf(plain []byte) byte {
	return plain[0] &^ (proto.FlagRouted | proto.FlagApp) & proto.KindMask
}

// answered kinds are named by (sender, target, SEQ, EPOCH); their REPLY by
// (target, sender, SEQ, EPOCH).
func requestOf(plain []byte) (requestID, bool) {
	if len(plain) < 5 {
		return requestID{}, false
	}
	switch kindOf(plain) {
	case proto.KindRequest, proto.KindWake, proto.KindReport, proto.KindKeepalive,
		proto.KindTrace, proto.KindResult, proto.KindEvent:
		return requestID{plain[1], plain[2], plain[3], plain[4]}, true
	case proto.KindReply:
		return requestID{plain[2], plain[1], plain[3], plain[4]}, true
	}
	return requestID{}, false
}

// spreading floods go out from the controller: covered neighbourhoods
// suppress them (spec 7). Everything else heads for the controller.
func spreading(kind byte) bool {
	switch kind {
	case proto.KindRequest, proto.KindWake, proto.KindWakeNet, proto.KindSleepNet:
		return true
	}
	return false
}

// inbound routed kinds head for the gateway, which never relays.
func inbound(kind byte) bool {
	switch kind {
	case proto.KindReply, proto.KindProbeReply, proto.KindRouteError, proto.KindTraceReply,
		proto.KindDiscoverReply, proto.KindResult:
		return true
	}
	return false
}

// echoed inbound kinds: the gateway echoes their last hop (LastHopAck).
func echoed(kind byte) bool {
	switch kind {
	case proto.KindReply, proto.KindProbeReply, proto.KindRouteError, proto.KindResult:
		return true
	}
	return false
}

func pathKind(b byte) bool {
	return b == proto.KindProbe || b == proto.KindJoin || b == proto.KindDiscover
}

// OnRadio takes one frame heard on air with its RSSI (NoRSSI: none) at now.
func (r *Relay) OnRadio(raw []byte, rssi RSSI, now proto.Time) {
	if len(raw) == 0 {
		return
	}
	if raw[0]&proto.FlagSecure == 0 {
		r.Stats.PlainDropped++ // the relay passes sealed frames only
		return
	}
	n, fcnt, ok := proto.OpenNetwork(r.ccm, r.plain[:], raw)
	if !ok {
		r.Stats.AuthFailed++ // checked before anything is recorded
		return
	}
	plain := r.plain[:n]
	if r.Asleep && !r.wakesUs(plain) {
		r.Stats.AsleepDropped++
		return
	}
	origin, target := plain[1], plain[2]
	tx := proto.Transmitter(raw)
	routed := plain[0]&proto.FlagRouted != 0
	r.heardAt[tx], r.heardAny = now, now
	if plain[0] == proto.KindWakeNet {
		r.wakeHeardLast = now
	}
	window := &r.windows[origin]
	if !window.Check(fcnt) {
		if routed {
			r.onRoutedCopy(plain, origin, fcnt, tx, rssi, now)
		} else {
			r.onCopy(origin, fcnt)
		}
		return
	}
	window.Accept(fcnt)
	routedProbe := plain[0] == proto.KindProbe|proto.FlagRouted
	heard := rssi
	if !routed || routedProbe {
		heard = r.noteLink(tx, rssi)
	}
	if kindOf(plain) == proto.KindReply {
		if id, ok := requestOf(plain); ok {
			r.onReply(id)
		}
	}
	switch {
	case routed && kindOf(plain) == proto.KindTraceReply:
		r.onTraceReply(plain, now)
	case routedProbe:
		r.onRoutedProbe(plain, heard, now)
	case routed:
		r.routeRelay(plain, origin, fcnt, tx, rssi, now)
	case pathKind(plain[0]):
		r.onProbe(plain, heard, now)
	case r.shouldRelay(plain, origin, target):
		r.schedule(plain, origin, fcnt, rssi, now)
	}
	if r.Observe != nil {
		r.Observe.Heard(plain, rssi)
	}
	if pathKind(plain[0]) || plain[0] == proto.KindTrace|proto.FlagRouted || routedProbe {
		return // consumed here; the target answers from this layer
	}
	if (target == r.cfg.Node || target == proto.BroadcastID) && r.up != nil {
		r.up.Up(raw)
	}
}

// Send puts a frame the layer above sealed (with the node's own FCNT) on
// air; it is repeated until a copy acknowledges it (spec 7, 11.2).
func (r *Relay) Send(raw []byte, now proto.Time) {
	n, fcnt, ok := proto.OpenNetwork(r.ccm, r.plain[:], raw)
	if !ok {
		return
	}
	r.originate(raw, r.plain[:n], fcnt, now)
}

func (r *Relay) originate(raw, plain []byte, fcnt uint32, now proto.Time) {
	r.windows[plain[1]].Accept(fcnt) // our own frame coming back is a copy
	r.Stats.Originated++
	room := r.nFlights < r.cfg.MaxPending
	sendNow := true
	preamble := uint16(8)
	switch {
	case plain[0] == proto.KindProbe|proto.FlagRouted && plain[1] == r.cfg.Node && plain[5] == r.cfg.Node:
		// Our own probe along a route (task C5): confirmed by the first
		// relay's copy, like a flooded one; repeated on its route.
		r.keepOwnRoutedProbe(plain, now)
	case plain[0] == proto.KindProbe|proto.FlagRouted:
		// A relay's probe along a route, sealed as its own: only its probe
		// state (acked by a copy naming us) confirms it.
	case plain[0]&proto.FlagRouted != 0:
		if kindOf(plain) == proto.KindTrace {
			r.noteTrace(plain) // so its answer acknowledges our hop
		}
		rt, ok := routeOf(plain)
		index := rt.index(r.cfg.Node)
		if !ok || index < 0 || !room || index >= rt.n-1 {
			break
		}
		id, _ := requestOf(plain)
		held, until := r.popHold(id)
		if b, ok := r.radio.(Busy); ok {
			if busy, ok := b.BusyUntil(now); ok && busy > now && (!held || busy > until) {
				held, until = true, busy
			}
		}
		if held && until > now {
			// The request reached us past the last relay, which is about to
			// pass it on: answering now would collide with that copy.
			sendNow = false
			r.routedFlight(raw, plain, rt, index, fcnt, now, 0, until-now, NoRSSI, 0, false)
		} else {
			r.routedFlight(raw, plain, rt, index, fcnt, now, 1, 0, NoRSSI, 0, false)
			preamble = r.flights[r.nFlights-1].preamble
		}
		r.followAnswer(plain[1], fcnt, plain)
	case pathKind(plain[0]) && plain[1] == r.cfg.Node && plain[5] == r.cfg.Node:
		// Our own new probe (or JOIN, DISCOVER): confirmed like a relay's,
		// by any copy passed on.
		r.keepOwnProbe(plain, now)
	case plain[0] == proto.KindWakeNet:
		if room {
			f := r.push(plain[1], fcnt, raw)
			wait := r.wakeAckWait(len(raw))
			f.due, f.timeout, f.hasTimeout = now+wait, wait, true
			f.confirm, f.sent, f.onAck = true, 1, true
			f.retries, f.hasRetries = r.cfg.WakeRetries, true
			f.preamble = r.cfg.WakePreamble
		}
		preamble = r.cfg.WakePreamble
	// Nobody relays a HELLO, and every relay re-seals a probe as its own
	// frame: waiting for a copy of either would only send it twice.
	case plain[0] != proto.KindHello && !pathKind(plain[0]):
		if r.cfg.HopRetries > 0 && room {
			f := r.push(plain[1], fcnt, raw)
			f.due = now + r.cfg.HopTimeout
			r.setRequest(f, plain)
			f.confirm, f.sent = true, 1
		}
	}
	if isWakeProbe(plain) {
		preamble = r.cfg.WakePreamble
	}
	if sendNow {
		r.radio.Put(raw, preamble)
	}
}

func (r *Relay) shouldRelay(plain []byte, origin, target byte) bool {
	if !r.cfg.Forward || origin == r.cfg.Node || target == r.cfg.Node {
		return false
	}
	if plain[0] == proto.KindHello {
		return false // one hop by definition
	}
	if kindOf(plain) == proto.KindReply && r.cfg.GuidedReplies {
		id, _ := requestOf(plain)
		if !r.onPath(id) {
			r.Stats.NotOnPath++
			return false
		}
	}
	return true
}

func (r *Relay) schedule(plain []byte, origin byte, fcnt uint32, rssi RSSI, now proto.Time) {
	if r.nFlights >= r.cfg.MaxPending {
		r.Stats.Overflow++
		return
	}
	kind := kindOf(plain)
	wake := plain[0] == proto.KindWakeNet
	if wake && r.Gate != nil && !r.Gate.AllowWake(plain) {
		r.Stats.WakesRefused++
		return
	}
	low, high := r.cfg.BackoffLow, r.cfg.BackoffHigh
	if wake {
		low, high = r.wakeWindow()
	}
	f := r.push(origin, fcnt, nil)
	f.n = proto.SealNetwork(r.ccm, f.raw[:], plain, fcnt, r.cfg.Node) // the same frame, TX = us
	f.due = now + r.backoffIn(rssi, low, high)
	r.setRequest(f, plain)
	f.suppressible = spreading(kind)
	f.confirm = kind == proto.KindReply && r.cfg.ConfirmReplies
	f.created = now
	if wake {
		f.preamble = r.cfg.WakePreamble
		f.retries, f.hasRetries = r.cfg.WakeCopies-1, true
	}
}

// setRequest: what a REPLY cancels or acknowledges (not a REPLY itself).
func (r *Relay) setRequest(f *flight, plain []byte) {
	f.id, _ = requestOf(plain)
	if kindOf(plain) != proto.KindReply {
		f.request, f.hasRequest = requestOf(plain)
	}
}

// backoff: part position (a close sender waits longer: a distant relay
// covers more new ground), part chance, so equally placed relays spread out.
func (r *Relay) backoffIn(rssi RSSI, low, high proto.Time) proto.Time {
	random := int64(r.rand.Uniform(0, ppm))
	var share int64
	if rssi == NoRSSI {
		share = random
	} else {
		weak, strong := int64(r.cfg.RSSIWeak), int64(r.cfg.RSSIStrong)
		close := (int64(rssi) - weak) * ppm / (strong - weak)
		close = min(ppm, max(0, close))
		w := int64(r.cfg.RSSIWeightPPM)
		share = (w*close + (ppm-w)*random) / ppm
	}
	return low + proto.Time(int64(high-low)*share/ppm)
}

// onCopy: a copy of a transmission already seen. One we sent is
// acknowledged; one we wait to relay counts towards suppression.
func (r *Relay) onCopy(origin byte, fcnt uint32) {
	r.Stats.Duplicates++
	i := r.find(origin, fcnt)
	if i < 0 {
		return
	}
	f := &r.flights[i]
	if f.sent > 0 && f.preamble != 8 && !f.confirm {
		return // a relay's further wake copies go out blind
	}
	if f.sent > 0 {
		ack := f.onAck
		r.remove(i) // someone carried it on: implicit acknowledgement
		if ack && r.Heard != nil {
			r.Heard.WakeHeard()
		}
		return
	}
	f.heard++
	if f.suppressible && r.cfg.SuppressAfter > 0 && f.heard >= r.cfg.SuppressAfter {
		r.remove(i)
		r.Stats.Suppressed++
	}
}

// onReply: the target answered, so relays of its request still waiting
// are pointless and our own request is acknowledged end to end; on a route
// the reply acknowledges every hop of its request.
func (r *Relay) onReply(id requestID) {
	for i := 0; i < r.nFlights; {
		f := &r.flights[i]
		if f.hasRequest && f.request == id {
			if f.routed {
				r.noteDone(f.origin, f.fcnt, f.rt.n-1)
				r.logEvent(f.logKey, f.hasLog, stateAnswered, true)
			}
			if f.sent == 0 {
				r.Stats.Answered++
			}
			r.remove(i)
			continue
		}
		i++
	}
}

func (r *Relay) onPath(id requestID) bool {
	for i := 0; i < r.nPaths; i++ {
		if r.paths[i] == id {
			return true
		}
	}
	return false
}

func (r *Relay) addPath(id requestID) {
	r.paths[r.nextPath] = id
	r.nextPath = (r.nextPath + 1) % MaxPaths
	if r.nPaths < MaxPaths {
		r.nPaths++
	}
}

// noteLink folds a copy from tx into its smoothed RSSI (EWMA), the
// neighbour heard least recently dropped past MaxLinks, and returns the
// estimate. Unrouted frames only: there TX is always who sent the copy.
func (r *Relay) noteLink(tx byte, rssi RSSI) RSSI {
	if rssi == NoRSSI || r.cfg.LinkWeightPPM <= 0 {
		return rssi
	}
	level := rssi
	for i := 0; i < r.nLinks; i++ {
		if r.links[i].node == tx {
			old := int64(r.links[i].level)
			w := int64(r.cfg.LinkWeightPPM)
			level = RSSI(((ppm-w)*old + w*int64(rssi)) / ppm)
			copy(r.links[i:], r.links[i+1:r.nLinks])
			r.nLinks--
			break
		}
	}
	if r.nLinks == MaxLinks {
		copy(r.links[:], r.links[1:])
		r.nLinks--
	}
	r.links[r.nLinks] = link{tx, level}
	r.nLinks++
	return level
}

// Tick relays frames whose backoff ran out and repeats unacknowledged ones.
func (r *Relay) Tick(now proto.Time) {
	busy, isBusy := proto.Time(0), false
	if b, ok := r.radio.(Busy); ok {
		busy, isBusy = b.BusyUntil(now)
		isBusy = isBusy && busy > now
	}
	for i := 0; i < r.nFlights; {
		f := &r.flights[i]
		if now < f.due || f.failed {
			i++
			continue
		}
		if isBusy {
			// Listen before talk: a neighbour is on the air (spec 7). After a
			// long preamble, a full backoff: neighbours that waited out the same
			// copy would otherwise all start within the jitter.
			if f.preamble != 8 {
				low, high := r.wakeWindow()
				f.due = busy + r.backoffIn(NoRSSI, low, high)
			} else {
				f.due = busy + r.rand.Uniform(0, r.cfg.RouteJitter)
			}
			i++
			continue
		}
		if f.blind {
			// The last hop into the gateway (never acknowledged) goes twice,
			// a short gap apart; a flight made with both sends behind it goes no more.
			if f.sent >= 2 {
				r.remove(i)
				continue
			}
			if f.sent == 0 {
				r.Stats.Relayed++
				if f.routed && f.index > 0 {
					r.Stats.Routed++
				}
			} else {
				r.Stats.Repeated++
			}
			f.sent++
			r.logEvent(f.logKey, f.hasLog, stateSent, false)
			r.radio.Put(f.raw[:f.n], f.preamble)
			if f.sent >= 2 {
				r.remove(i)
				continue
			}
			f.due = now + r.repeatGap(f.n)
			i++
			continue
		}
		retries := r.cfg.HopRetries
		if f.hasRetries {
			retries = f.retries
		}
		if f.preamble != 8 && !f.confirm && f.sent > 0 && !f.routed {
			if quiet := r.wakeHeardLast + r.wakeGap(f.n); now < quiet {
				// Wake copies are still going round: those unheard may just be
				// queued behind them.
				f.due = quiet
				i++
				continue
			}
			if !r.wakeNeeded(f) {
				r.remove(i) // every neighbour we know is awake
				r.Stats.WakesDone++
				continue
			}
		}
		if f.answer {
			r.Stats.RetryAnswered++
			r.logEvent(f.logKey, f.hasLog, stateSent, false)
			r.radio.Put(f.raw[:f.n], f.preamble)
			r.remove(i)
			continue
		}
		if f.onDemand {
			if f.sent > 0 && !f.demanded {
				r.remove(i) // nobody asked again: it got there
				continue
			}
			if f.sent > 0 {
				r.Stats.Repeated++
			}
			f.sent++
			f.demanded = false
			f.due = f.linger
			r.logEvent(f.logKey, f.hasLog, stateSent, false)
			r.radio.Put(f.raw[:f.n], f.preamble)
			if f.sent > retries+1 {
				r.remove(i)
				continue
			}
			i++
			continue
		}
		switch {
		case f.sent == 0 && f.routed && f.index == 0:
			// our own answer, held back: not a relay
		case f.sent == 0:
			r.Stats.Relayed++
			if f.preamble != 8 {
				r.Stats.WakesRelayed++
			}
			if f.routed {
				r.Stats.Routed++
			} else if f.hasRequest {
				r.addPath(f.request)
			}
		case f.sent > retries:
			// Nobody carried it on; the controller's end-to-end retry
			// (a fresh FCNT, hence a new flood) takes over.
			if f.routed {
				f.failed = true // reported once every send has gone
				i++
				continue
			}
			r.remove(i)
			continue
		default:
			r.Stats.Repeated++
			if f.preamble != 8 {
				r.Stats.WakeRepeats++
			}
		}
		f.sent++
		f.sentAt = now
		if f.routed {
			r.logEvent(f.logKey, f.hasLog, stateSent, f.confirm)
		}
		r.radio.Put(f.raw[:f.n], f.preamble)
		if f.confirm {
			if f.hasTimeout {
				// Up to one more airtime at random, so two senders around one
				// receiver cannot repeat in step.
				f.due = now + f.timeout + r.rand.Uniform(0, r.airtime(f.n))
			} else {
				f.due = now + r.cfg.HopTimeout
			}
			i++
			continue
		}
		if f.preamble != 8 && f.sent < r.cfg.WakeCopies {
			f.due = now + r.wakeGap(f.n)
			i++
			continue
		}
		r.remove(i)
	}
	r.tickTraces(now, busy, isBusy)
	r.tickRadio(now)
	for i := 0; i < r.nFlights; {
		if !r.flights[i].failed {
			i++
			continue
		}
		r.broken = r.flights[i]
		r.remove(i)
		r.routeBroken(&r.broken, now)
	}
	r.tickProbes(now)
	r.passProbes(now)
	r.passTraces()
}

// NextDue is the earliest time Tick can do anything (proto.Never: nothing
// waits); a frame heard or handed down changes it.
func (r *Relay) NextDue() proto.Time {
	if r.moveDue() {
		return -proto.Never // a move waits for its answer to go; then the next Tick
	}
	due := min(r.probesDue(), r.tracesDue())
	for i := 0; i < r.nFlights; i++ {
		due = min(due, r.flights[i].due)
	}
	if r.cfg.Forward && r.setting != r.home() {
		due = min(due, r.heardAny+r.cfg.RadioTimeout)
	}
	return due
}

// Pending is how many frames wait to be relayed or acknowledged, probes
// and trace records included (mesh.py pending: a node goes dormant only
// once it is 0).
func (r *Relay) Pending() int {
	n := r.nFlights
	for i := 0; i < r.nProbes; i++ {
		if p := &r.probes[i]; !p.sent || r.probeAwaiting(p) {
			n++
		}
	}
	for i := 0; i < r.nTraceOut; i++ {
		if t := &r.traceOut[i]; !t.acked || t.echo {
			n++
		}
	}
	return n
}

// zeroFlight clears a flight slot: a composite literal this large would be
// allocated on the heap (TinyGo).
var zeroFlight flight

func (r *Relay) push(origin byte, fcnt uint32, raw []byte) *flight {
	f := &r.flights[r.nFlights]
	*f = zeroFlight
	f.origin, f.fcnt, f.preamble = origin, fcnt, 8
	f.n = copy(f.raw[:], raw)
	r.nFlights++
	return f
}

func (r *Relay) find(origin byte, fcnt uint32) int {
	for i := 0; i < r.nFlights; i++ {
		if r.flights[i].origin == origin && r.flights[i].fcnt == fcnt {
			return i
		}
	}
	return -1
}

// remove keeps the others in the order they came (the vectors' order).
func (r *Relay) remove(i int) {
	copy(r.flights[i:], r.flights[i+1:r.nFlights])
	r.nFlights--
}
