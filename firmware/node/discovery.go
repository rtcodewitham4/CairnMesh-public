package node

import (
	"math"

	"cairnmesh/firmware/mesh"
	"cairnmesh/firmware/proto"
)

// Node discovery, device side (spec 12.3; discovery.DiscoveryAgent): a JOIN
// some time after boot, repeated with fresh ids until the gateway's routed
// REPLY; answers to the gateway's scans (DISCOVER) back along the best path
// the relay kept, naming the neighbours heard best.

// Table sizes (spec 9).
const (
	maxAnswers     = 2
	maxAnswered    = 4
	maxScans       = 2
	maxScanHeard   = 8
	maxNamed       = 3 // frames.MAX_NEIGHBOURS
	discoverSpread = 250 * proto.Millisecond
	probeWakeHold  = 2 * proto.Second
)

// DiscoveryConfig is discovery.DiscoveryAgent's options (spec 18);
// DefaultDiscoveryConfig has the defaults.
type DiscoveryConfig struct {
	// Join: send a JOIN at boot, within JoinSpread, WIN JoinWindow units.
	Join       bool
	JoinSpread proto.Time
	JoinWindow byte
	// Unanswered: again after JoinBackoffMin, doubling up to JoinBackoffMax.
	JoinBackoffMin, JoinBackoffMax proto.Time
	// JoinQuiet: another node's JOIN pushes ours back by this plus as much
	// again at random (0: off).
	JoinQuiet proto.Time
	// JoinByScan: answering a scan stands in for a JOIN.
	JoinByScan bool
	// IslandAfter unanswered JOINs with nothing heard from the controller:
	// JOIN every IslandBackoff (0: off, the default; STATUS Q17).
	IslandAfter   int
	IslandBackoff proto.Time
	// JoinWakes (Q22; mesh.py join_wakes): booting in a sleeping network,
	// the JOINs until the boot wake ends are wake JOINs.
	JoinWakes bool
}

// DefaultDiscoveryConfig is discovery.DiscoveryAgent's defaults.
func DefaultDiscoveryConfig() DiscoveryConfig {
	return DiscoveryConfig{
		Join: true, JoinSpread: 30 * proto.Second, JoinWindow: 10,
		JoinBackoffMin: 30 * proto.Second, JoinBackoffMax: 300 * proto.Second,
		JoinQuiet: 3 * proto.Second, JoinByScan: true,
		IslandBackoff: 3600 * proto.Second, JoinWakes: true,
	}
}

// DiscoveryStats counts what discovery did (DiscoveryAgent.stats).
type DiscoveryStats struct {
	Joins, Joined, Answers, AnswersDropped uint32
}

type scanKey struct {
	origin byte
	id     uint16
}

type scanHeard struct {
	key   scanKey
	nodes [maxScanHeard]byte // in the order first heard
	rssi  [maxScanHeard]mesh.RSSI
	n     int
}

type answer struct {
	due   proto.Time
	plain [proto.MaxFrame]byte
	n     int
}

type named struct {
	node byte
	rssi mesh.RSSI
}

// Discovery is one node's JOIN and scan answers (Config.Discovery on a Stack).
type Discovery struct {
	cfg              DiscoveryConfig
	node, controller byte
	epoch            byte
	relay            *mesh.Relay
	rand             proto.Rand
	dev              *proto.Device // its State goes in every scan answer
	watch            *Watch        // the HELLO table's neighbours; nil: none
	out              proto.FrameSender

	seq             byte
	joined          bool
	joinAt          proto.Time // Never: no JOIN due
	joinWait        proto.Time
	joinSeq         byte
	joining         bool // a JOIN went out and waits for its REPLY
	unanswered      int
	heardController bool
	// bootUntil: the boot wake's end (Q22); JOINs before it are wake JOINs.
	bootUntil proto.Time

	answers   [maxAnswers]answer // in the order collected
	nAnswers  int
	answered  [maxAnswered]scanKey // oldest first
	nAnswered int
	scans     [maxScans]scanHeard // oldest first
	nScans    int
	pick      [maxScanHeard + MaxNeighbours]named
	frame     [7]byte
	// now: the time of what is being handled (the relay calls PathFound
	// from its OnRadio or Tick; the Stack sets it first).
	now   proto.Time
	Stats DiscoveryStats
}

// zeroDiscovery clears a Discovery: a composite literal this large would be
// allocated on the heap (TinyGo).
var zeroDiscovery Discovery

func (d *Discovery) init(cfg DiscoveryConfig, node, controller, epoch, seq byte, relay *mesh.Relay, r proto.Rand, dev *proto.Device, watch *Watch, out proto.FrameSender) {
	*d = zeroDiscovery
	d.cfg, d.node, d.controller, d.epoch, d.seq = cfg, node, controller, epoch, seq
	d.relay, d.rand, d.dev, d.watch, d.out = relay, r, dev, watch, out
	d.bootUntil = -1
}

// start: the node (re)starts; RAM is lost and it joins again.
func (d *Discovery) start(now proto.Time) {
	d.joined, d.joining = false, false
	d.nAnswers, d.nScans = 0, 0
	d.joinWait = d.cfg.JoinBackoffMin
	d.unanswered = 0
	d.heardController = false
	d.joinAt = proto.Never
	if d.cfg.Join {
		d.joinAt = now + d.rand.Uniform(0, d.cfg.JoinSpread)
	}
}

// Joined reports whether the gateway knows this node (its REPLY, or a scan answered).
func (d *Discovery) Joined() bool { return d.joined }

func (d *Discovery) island() bool {
	return d.cfg.IslandAfter > 0 && !d.joined && !d.heardController && d.unanswered >= d.cfg.IslandAfter
}

func (d *Discovery) nextDue() proto.Time {
	due := proto.Never
	if !d.joined {
		due = d.joinAt
	}
	for i := 0; i < d.nAnswers; i++ {
		due = min(due, d.answers[i].due)
	}
	return due
}

func (d *Discovery) tick(now proto.Time) {
	if !d.joined && d.joinAt != proto.Never && now >= d.joinAt {
		d.sendJoin(now)
		d.unanswered++
		wait := d.joinWait
		if d.island() {
			wait = d.cfg.IslandBackoff
		}
		d.joinAt = now + wait
		d.joinWait = min(2*d.joinWait, d.cfg.JoinBackoffMax)
	}
	for i := 0; i < d.nAnswers; {
		a := &d.answers[i]
		if now < a.due {
			i++
			continue
		}
		d.Stats.Answers++
		// The scan answer tells the gateway about us: no JOIN needed.
		if d.cfg.JoinByScan {
			d.joined, d.joining = true, false
		}
		d.out.Send(a.plain[:a.n])
		copy(d.answers[i:d.nAnswers], d.answers[i+1:d.nAnswers])
		d.nAnswers--
	}
}

func (d *Discovery) sendJoin(now proto.Time) {
	d.seq++
	window := d.cfg.JoinWindow
	if d.cfg.JoinWakes && d.relay.WakeNetwork() && now < d.bootUntil {
		window |= 0x80 // Q22: a wake JOIN (long preamble)
	}
	d.frame = [7]byte{proto.KindJoin, d.node, d.controller, d.seq, d.epoch, d.node, window}
	d.joinSeq, d.joining = d.seq, true
	d.Stats.Joins++
	d.out.Send(d.frame[:7])
}

// hold pushes a pending JOIN back by quiet plus as much again at random.
func (d *Discovery) hold(now, quiet proto.Time) {
	if d.joined || d.joinAt == proto.Never {
		return
	}
	d.joinAt = max(d.joinAt, now+quiet+d.rand.Uniform(0, quiet))
}

func (d *Discovery) fromController(plain []byte) bool {
	if len(plain) < 2 {
		return false
	}
	if plain[1] == d.controller {
		return true
	}
	k := plain[0] & proto.KindMask
	path := k == proto.KindProbe || k == proto.KindJoin || k == proto.KindDiscover
	return path && len(plain) >= 6 && plain[5] == d.controller
}

// heard is the relay observer's share: every authenticated first copy.
func (d *Discovery) heard(plain []byte, rssi mesh.RSSI, now proto.Time) {
	if !d.heardController && d.fromController(plain) {
		if d.island() && d.joinAt != proto.Never {
			// The controller is out there after all: join at the usual pace.
			d.joinWait = d.cfg.JoinBackoffMin
			d.joinAt = min(d.joinAt, now+d.rand.Uniform(0, d.cfg.JoinSpread))
		}
		d.heardController = true
	}
	k := plain[0]
	if k == proto.KindJoin && len(plain) >= 7 && plain[5] != d.node {
		if d.cfg.JoinQuiet > 0 {
			d.hold(now, d.cfg.JoinQuiet)
		}
		return
	}
	if k == proto.KindProbe && len(plain) >= 7 && plain[6]&0x80 != 0 {
		// Q22: woken by a wake probe, a JOIN due long ago would go out on top of it.
		d.hold(now, proto.Time(plain[6]&0x7F)*mesh.WindowUnit+probeWakeHold)
	}
	scan := k == proto.KindDiscover && len(plain) >= 8
	if scan && d.cfg.JoinByScan && (plain[2] == proto.BroadcastID || plain[2] == d.node) {
		// About to answer a scan, which makes a JOIN needless.
		d.hold(now, proto.Time(plain[6])*mesh.WindowUnit+proto.Time(plain[7])*discoverSpread)
	}
	if scan && rssi != mesh.NoRSSI {
		d.noteScan(scanKey{origin: plain[5], id: proto.MessageID(plain)}, plain[1], rssi)
		return
	}
	if !d.joining || k&proto.FlagRouted == 0 || k&proto.KindMask != proto.KindReply {
		return
	}
	cut, _, ok := proto.SplitRoute(plain)
	if ok && cut == 6 && plain[1] == d.controller && plain[2] == d.node && plain[3] == d.joinSeq && plain[4] == d.epoch {
		d.joined, d.joining = true, false
		d.Stats.Joined++
	}
}

// noteScan: the copy's SENDER is whoever just passed the scan on, a neighbour.
func (d *Discovery) noteScan(key scanKey, sender byte, rssi mesh.RSSI) {
	var s *scanHeard
	for i := 0; i < d.nScans; i++ {
		if d.scans[i].key == key {
			s = &d.scans[i]
		}
	}
	if s == nil {
		if d.nScans == maxScans {
			copy(d.scans[:], d.scans[1:])
			d.nScans--
		}
		s = &d.scans[d.nScans]
		*s = scanHeard{key: key}
		d.nScans++
	}
	at := -1
	for i := 0; i < s.n; i++ {
		if s.nodes[i] == sender {
			at = i
		}
	}
	if at < 0 && s.n >= maxScanHeard {
		weakest := 0
		for i := 1; i < s.n; i++ {
			if s.rssi[i] < s.rssi[weakest] {
				weakest = i
			}
		}
		if s.rssi[weakest] >= rssi {
			return
		}
		copy(s.nodes[weakest:s.n], s.nodes[weakest+1:s.n])
		copy(s.rssi[weakest:s.n], s.rssi[weakest+1:s.n])
		s.n--
	}
	if at < 0 {
		s.nodes[s.n], s.rssi[s.n] = sender, rssi
		s.n++
		return
	}
	s.rssi[at] = max(s.rssi[at], rssi)
}

// PathFound is the relay's mesh.PathFound: the best path of a scan, once
// its window is over (a JOIN's is the gateway's).
func (d *Discovery) PathFound(frame []byte, links []mesh.Link) {
	if frame[0] != proto.KindDiscover {
		return
	}
	key := scanKey{origin: frame[5], id: proto.MessageID(frame)}
	for i := 0; i < d.nAnswered; i++ {
		if d.answered[i] == key {
			return
		}
	}
	if d.nAnswered == maxAnswered {
		copy(d.answered[:], d.answered[1:])
		d.nAnswered--
	}
	d.answered[d.nAnswered] = key
	d.nAnswered++
	if d.nAnswers >= maxAnswers {
		d.Stats.AnswersDropped++
		return
	}
	h := mesh.PathHead(proto.KindDiscover)
	count := mesh.PathHops(frame)
	parent := frame[5]
	if count > 0 {
		parent = frame[h+3*(count-1)]
	}
	a := &d.answers[d.nAnswers]
	p := append(a.plain[:0], proto.KindDiscoverReply|proto.FlagRouted, d.node, frame[5], frame[3], frame[4], d.dev.State)
	for _, l := range links {
		p = append(p, byte(-l.RSSI), byte(l.SNR))
	}
	for _, n := range d.best(key, parent) {
		l := d.measured(n.rssi)
		p = append(p, n.node, byte(-l.RSSI), byte(l.SNR))
	}
	for i := count - 1; i >= 0; i-- { // the scan's hops walked backwards
		p = append(p, frame[h+3*i])
	}
	p = append(p, byte(count))
	a.n = len(p)
	a.due = d.now + d.rand.Uniform(0, proto.Time(frame[7])*discoverSpread)
	d.nAnswers++
}

// best: the scan copies' senders, then the HELLO table's neighbours, the
// strongest maxNamed of them, not the parent, not us (ties: first heard).
func (d *Discovery) best(key scanKey, parent byte) []named {
	pick := d.pick[:0]
	for i := 0; i < d.nScans; i++ {
		if s := &d.scans[i]; s.key == key {
			for j := 0; j < s.n; j++ {
				pick = append(pick, named{s.nodes[j], s.rssi[j]})
			}
		}
	}
	if d.watch != nil {
	table:
		for i := 0; i < d.watch.n; i++ {
			nb := &d.watch.table[i]
			if nb.lost || nb.rssi == mesh.NoRSSI {
				continue
			}
			for j := range pick {
				if pick[j].node == nb.node {
					pick[j].rssi = max(pick[j].rssi, nb.rssi)
					continue table
				}
			}
			pick = append(pick, named{nb.node, nb.rssi})
		}
	}
	kept := pick[:0]
	for _, n := range pick {
		if n.node != parent && n.node != d.node {
			kept = append(kept, n)
		}
	}
	for i := 1; i < len(kept); i++ { // stable, strongest first (no allocation)
		for j := i; j > 0 && kept[j].rssi > kept[j-1].rssi; j-- {
			kept[j], kept[j-1] = kept[j-1], kept[j]
		}
	}
	if len(kept) > maxNamed {
		kept = kept[:maxNamed]
	}
	return kept
}

// measured rounds and clamps a level into a link (frames.LinkQuality.measured,
// SNR = RSSI − the noise floor).
func (d *Discovery) measured(rssi mesh.RSSI) mesh.Link {
	dbm := float64(rssi) / 1024
	snr := (dbm - float64(d.relay.NoiseFloor())/1024) * 4
	return mesh.Link{
		RSSI: int16(min(0, max(-255, math.RoundToEven(dbm)))),
		SNR:  int8(min(127, max(-128, math.RoundToEven(snr)))),
	}
}
