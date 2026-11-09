package mesh

import (
	"math"

	"cairnmesh/firmware/proto"
)

// Probes (spec 11.3), JOINs and DISCOVERs (12.2): flooded path frames.
// Every relay appends itself and the link it heard the frame over, then
// seals it again as its own frame (Sealer). A relay passes each one on once,
// after a wait set by what the hop just crossed costs, keeping the best copy
// heard meanwhile; the target collects copies for the frame's window and
// answers the best path. Repeated once if nobody further out passes it on.

// Link is one received link as the radio measured it: RSSI in whole dBm
// (-255 .. 0), SNR in quarter dB (the SX1262's packet SNR unit).
type Link struct {
	RSSI int16
	SNR  int8
}

// PathFound hears of a JOIN (at the gateway) or a DISCOVER (at a node)
// collected for its window: the best path heard, one link per hop from the
// origin's side. The layer above answers it.
type PathFound interface {
	PathFound(frame []byte, links []Link)
}

const (
	// MaxProbes path frames waiting or recently passed on (spec 9).
	MaxProbes = 4
	// WindowUnit is a probe window unit (frames.PROBE_WINDOW_UNIT).
	WindowUnit = 50 * proto.Millisecond
	probeWake  = 0x80
	// sealedExtra: a sealed frame is its plain frame + 9 bytes (FCNT, TX,
	// MIC; KIND .. SEQ are the clear header). proto.Overhead is a buffer's
	// margin, not this.
	sealedExtra = 9
	hopBytes    = 3
	maxPath     = 8 + hopBytes*proto.MaxRelays // DISCOVER head + 16 hops
)

type probeKey struct {
	origin   byte
	id       uint16
	kind     byte // 0 for a PROBE (the gateway's scans have their own id space)
	everyone bool // a DISCOVER to all, collected as a target
}

var zeroProbe probe

type probe struct {
	key      probeKey
	frame    [maxPath]byte
	n        int
	links    [proto.MaxRelays + 1]Link
	nLinks   int
	due      proto.Time
	expires  proto.Time
	target   bool
	sent     bool
	acked    bool
	resends  uint8
	passOnce bool // Tick marked it to go now
	// A probe along a route (C5): the relays it still has to pass, after us.
	route    [proto.MaxRelays]byte
	nRoute   int
	hasRoute bool
}

func head(kind byte) int {
	if kind == proto.KindDiscover {
		return 8
	}
	return 7
}

// pathFrame checks a flooded path frame's layout like frames.ProbeFrame:
// head + 3 bytes per hop, at most 16 hops, valid hop list, SENDER = last hop.
func pathFrame(plain []byte) bool {
	h := head(plain[0])
	if len(plain) < h || (len(plain)-h)%hopBytes != 0 || (len(plain)-h)/hopBytes > proto.MaxRelays {
		return false
	}
	origin, target := plain[5], plain[2]
	last := origin
	for i := h; i < len(plain); i += hopBytes {
		node := plain[i]
		if node == origin || node == target || node == proto.BroadcastID {
			return false
		}
		for j := h; j < i; j += hopBytes {
			if plain[j] == node {
				return false
			}
		}
		last = node
	}
	return plain[1] == last
}

func hops(plain []byte) int { return (len(plain) - head(plain[0])) / hopBytes }

func hasHop(plain []byte, node byte) bool {
	for i := head(plain[0]); i < len(plain); i += hopBytes {
		if plain[i] == node {
			return true
		}
	}
	return false
}

func keyOf(plain []byte) probeKey {
	k := probeKey{origin: plain[5], id: proto.MessageID(plain)}
	if plain[0] != proto.KindProbe {
		k.kind = plain[0]
	}
	return k
}

func windowOf(plain []byte) proto.Time {
	w := plain[6]
	if plain[0] != proto.KindDiscover {
		w &^= probeWake
	}
	return proto.Time(w) * WindowUnit
}

// roundHalfEven is Python's round() of v / unit.
func roundHalfEven(v, unit int64) int64 {
	q, rem := v/unit, v%unit
	if rem < 0 {
		q, rem = q-1, rem+unit
	}
	switch {
	case 2*rem > unit, 2*rem == unit && q%2 != 0:
		q++
	}
	return q
}

// measured is LinkQuality.measured(rssi, rssi - noise floor).
func (r *Relay) measured(rssi RSSI) Link {
	if rssi == NoRSSI {
		rssi = r.cfg.RSSIWeak
	}
	level := min(0, max(-255, roundHalfEven(int64(rssi), 1024)))
	snr := min(127, max(-128, roundHalfEven(4*int64(rssi-r.cfg.NoiseFloor), 1024)))
	return Link{RSSI: int16(level), SNR: int8(snr)}
}

// hopETX: expected sends for one hop and its acknowledgement back
// (mesh._link_etx), the chance a frame gets through normal in the SNR
// margin over the demodulation floor, plus the cost of breaking the
// session times the chance the hop fails all its attempts.
func (r *Relay) hopETX(snrQuarter int8) float64 {
	margin := float64(snrQuarter)/4 - r.cfg.ProbeWeakSNR - r.cfg.ETXSafety
	through := max(0.01, 0.5*(1+math.Erf(margin/(r.cfg.ETXSigma*math.Sqrt2))))
	both := through * through
	return 1/both + r.cfg.ETXBreak*math.Pow(1-both, float64(r.cfg.RouteRetries)+1)
}

func (r *Relay) pathETX(links []Link) float64 {
	sum := 0.0
	for _, l := range links {
		sum += r.hopETX(l.SNR)
	}
	return sum
}

// probeDelay: by what the hop just crossed costs (ETX above 1, up to
// ProbeCostSpan), so copies reach every relay in the order of their path's
// cost; a fifth at random keeps equal relays from colliding.
func (r *Relay) probeDelay(links []Link) proto.Time {
	cost := r.hopETX(links[len(links)-1].SNR)
	share := min(1, max(0, (cost-1)/r.cfg.ProbeCostSpan))
	random := float64(r.rand.Uniform(0, ppm)) / ppm
	span := float64(r.cfg.ProbeBackoffHigh - r.cfg.ProbeBackoffLow)
	return r.cfg.ProbeBackoffLow + proto.Time(math.Round(span*(0.8*share+0.2*random)))
}

func (r *Relay) findProbe(k probeKey) *probe {
	for i := 0; i < r.nProbes; i++ {
		if r.probes[i].key == k {
			return &r.probes[i]
		}
	}
	return nil
}

// keepProbe makes room (the oldest already passed on first, else the one
// expiring first) and appends a new probe state.
func (r *Relay) keepProbe(k probeKey) *probe {
	if r.nProbes >= MaxProbes {
		victim := 0
		for i := 1; i < r.nProbes; i++ {
			a, b := &r.probes[i], &r.probes[victim]
			if a.sent && !b.sent || a.sent == b.sent && a.expires < b.expires {
				victim = i
			}
		}
		copy(r.probes[victim:], r.probes[victim+1:r.nProbes])
		r.nProbes--
	}
	p := &r.probes[r.nProbes]
	*p = zeroProbe
	p.key = k
	r.nProbes++
	return p
}

func (p *probe) set(frame []byte, links []Link) {
	p.n = copy(p.frame[:], frame)
	p.nLinks = copy(p.links[:], links)
}

// linksOf lists a path frame's hop links, then extra (if any), into dst.
func linksOf(dst []Link, plain []byte, extra *Link) []Link {
	dst = dst[:0]
	for i := head(plain[0]); i < len(plain); i += hopBytes {
		dst = append(dst, Link{RSSI: -int16(plain[i+1]), SNR: int8(plain[i+2])})
	}
	if extra != nil {
		dst = append(dst, *extra)
	}
	return dst
}

// onProbe: a fresh flooded PROBE, JOIN or DISCOVER heard at the smoothed level.
func (r *Relay) onProbe(plain []byte, level RSSI, now proto.Time) {
	if !pathFrame(plain) {
		r.Stats.ProbesIgnored++
		return
	}
	k := keyOf(plain)
	state := r.findProbe(k)
	me := r.cfg.Node
	if hasHop(plain, me) || plain[5] == me {
		if state != nil {
			state.acked = true // someone further out carries our copy
		}
		return
	}
	if isWakeProbe(plain) && state == nil && r.Gate != nil && !r.Gate.AllowWake(plain) {
		r.Stats.WakesRefused++
		return
	}
	in := r.measured(level)
	buf := r.linkBuf[:]
	// A DISCOVER to everyone: every node is a target (it keeps the best path
	// to answer along) and a relay (it passes the scan on) at once.
	everyone := plain[0] == proto.KindDiscover && plain[2] == proto.BroadcastID
	if plain[2] == me || everyone {
		links := linksOf(buf, plain, &in)
		ck := k
		ck.everyone = everyone
		if c := r.findProbe(ck); c == nil {
			c = r.keepProbe(ck)
			c.set(plain, links)
			c.due, c.expires, c.target = now+windowOf(plain), now+r.cfg.ProbeTTL, true
		} else if !c.sent && r.pathETX(links) < r.pathETX(c.links[:c.nLinks]) {
			c.set(plain, links)
		}
		if !everyone {
			return
		}
		state = r.findProbe(k) // keepProbe may have moved it
	}
	if !r.cfg.Forward || hops(plain) >= proto.MaxRelays {
		r.Stats.ProbesIgnored++
		return
	}
	// The frame as we send it on: SENDER = us, our hop appended.
	var ext [maxPath]byte
	n := copy(ext[:], plain)
	ext[1] = me
	ext[n], ext[n+1], ext[n+2] = me, byte(-in.RSSI), byte(in.SNR)
	n += hopBytes
	links := linksOf(buf, ext[:n], nil)
	due := now + r.probeDelay(links)
	if state == nil {
		p := r.keepProbe(k)
		p.set(ext[:n], links)
		p.due, p.expires = due, now+r.cfg.ProbeTTL
	} else if !state.sent && r.pathETX(links) < r.pathETX(state.links[:state.nLinks]) {
		state.set(ext[:n], links)
		state.due = min(state.due, due)
	}
}

// keepOwnProbe: our own new probe (or JOIN, DISCOVER), confirmed like a
// relay's by any copy passed on.
func (r *Relay) keepOwnProbe(plain []byte, now proto.Time) {
	if r.findProbe(keyOf(plain)) != nil {
		return // a resend comes through here too: it keeps its count
	}
	p := r.keepProbe(keyOf(plain))
	p.set(plain, nil)
	p.expires, p.sent = now+r.cfg.ProbeTTL, true
	p.due = now + r.probeAckWait(p)
}

// onRoutedProbe: a probe along a route (task C5), heard first: only the
// next listed relay passes it on, at once.
func (r *Relay) onRoutedProbe(plain []byte, level RSSI, now proto.Time) {
	cut, relays, ok := proto.SplitRoute(plain)
	base := r.probeBuf[:0]
	if ok {
		base = append(base, plain[:cut]...)
		base[0] &^= proto.FlagRouted
	}
	if !ok || !pathFrame(base) {
		r.Stats.ProbesIgnored++
		return
	}
	k := keyOf(base)
	state := r.findProbe(k)
	me := r.cfg.Node
	if hasHop(base, me) || base[5] == me {
		if state != nil {
			state.acked = true // the next relay carries our copy
		}
		return
	}
	in := r.measured(level)
	buf := r.linkBuf[:]
	if base[2] == me {
		if state == nil {
			p := r.keepProbe(k)
			p.set(base, linksOf(buf, base, &in))
			p.due, p.expires, p.target = now+windowOf(base), now+r.cfg.ProbeTTL, true
		}
		return
	}
	if len(relays) == 0 || relays[0] != me || !r.cfg.Forward || hops(base) >= proto.MaxRelays {
		r.Stats.NotOnRoute++
		return
	}
	if state != nil {
		return // a repeat from behind: ours is out or due
	}
	var ext [maxPath]byte
	n := copy(ext[:], base)
	ext[1] = me
	ext[n], ext[n+1], ext[n+2] = me, byte(-in.RSSI), byte(in.SNR)
	n += hopBytes
	p := r.keepProbe(k)
	p.set(ext[:n], linksOf(buf, ext[:n], nil))
	p.due = now + r.cfg.ProbeBackoffLow + r.rand.Uniform(0, r.cfg.RouteJitter)
	p.expires = now + r.cfg.ProbeTTL
	p.nRoute, p.hasRoute = copy(p.route[:], relays[1:]), true
}

// keepOwnRoutedProbe: our own probe along a route; a resend keeps its count.
func (r *Relay) keepOwnRoutedProbe(plain []byte, now proto.Time) {
	cut, relays, ok := proto.SplitRoute(plain)
	if !ok {
		return
	}
	base := r.probeBuf[:0]
	base = append(base, plain[:cut]...)
	base[0] &^= proto.FlagRouted
	if r.findProbe(keyOf(base)) != nil {
		return
	}
	p := r.keepProbe(keyOf(base))
	p.set(base, nil)
	p.expires, p.sent = now+r.cfg.ProbeTTL, true
	p.nRoute, p.hasRoute = copy(p.route[:], relays), true
	p.due = now + r.probeAckWait(p)
}

func (r *Relay) probeAwaiting(p *probe) bool {
	// A wake probe floods the whole network with long preambles: a resend
	// adds as much again.
	retries := r.cfg.ProbeRetries
	switch {
	case p.hasRoute:
		retries = r.cfg.RouteRetries // one path, no other copy to make up for it
	case isWakeProbe(p.frame[:p.n]):
		retries = r.cfg.WakeProbeRetries
	}
	return !p.target && !p.acked && p.resends < retries
}

// probeAckWait: the next relay waits up to the longest probe backoff, then
// sends; the repeat goes at a fresh random point.
func (r *Relay) probeAckWait(p *probe) proto.Time {
	length := p.n + sealedExtra + hopBytes
	high := r.cfg.ProbeBackoffHigh
	lead := proto.Time(0)
	if isWakeProbe(p.frame[:p.n]) {
		lead = r.wakeLead()
	}
	return high + 2*(r.airtime(length)+lead) + r.cfg.RouteSlack + r.rand.Uniform(0, high)
}

// tickProbes marks the probes to pass on now (Tick sends them after the flights).
func (r *Relay) tickProbes(now proto.Time) {
	for i := 0; i < r.nProbes; {
		p := &r.probes[i]
		switch {
		case p.sent && now >= p.expires:
			copy(r.probes[i:], r.probes[i+1:r.nProbes])
			r.nProbes--
			continue
		case p.sent && r.probeAwaiting(p) && now >= p.due:
			// Nobody further out passed it on: once more, as a new frame.
			p.resends++
			p.due = now + r.probeAckWait(p)
			p.passOnce = true
		case !p.sent && now >= p.due:
			p.sent = true
			p.due = now + r.probeAckWait(p)
			p.passOnce = true
		}
		i++
	}
}

func (r *Relay) passProbes(now proto.Time) {
	for i := 0; i < r.nProbes; i++ {
		if r.probes[i].passOnce {
			r.probes[i].passOnce = false
			r.passProbe(&r.probes[i], now)
		}
	}
}

// passProbe forwards a probe, or answers it as its target.
func (r *Relay) passProbe(p *probe, now proto.Time) {
	if r.Sealer == nil {
		r.Stats.Unsealed++
		return
	}
	frame := p.frame[:p.n]
	if !p.target {
		r.Stats.ProbesRelayed++
		if p.hasRoute {
			frame = withRoute(r.scratch[:0], frame, p.route[:p.nRoute])
		}
		r.sendOwn(frame, now)
		return
	}
	if frame[0] != proto.KindProbe {
		// JOIN at the gateway, DISCOVER at a node: the layer above answers.
		if r.Path != nil {
			r.Path.PathFound(frame, p.links[:p.nLinks])
		}
		return
	}
	// PROBE_REPLY along the hops, reversed, with every link of the path.
	a := r.scratch[:0]
	a = append(a, proto.KindProbeReply|proto.FlagRouted, r.cfg.Node, frame[5], frame[3], frame[4])
	for _, l := range p.links[:p.nLinks] {
		a = append(a, byte(-l.RSSI), byte(l.SNR))
	}
	count := hops(frame)
	for i := head(frame[0]) + hopBytes*(count-1); i >= head(frame[0]); i -= hopBytes {
		a = append(a, frame[i])
	}
	a = append(a, byte(count))
	r.Stats.ProbesAnswered++
	r.sendOwn(a, now)
}

// sendOwn seals a frame the relay originates and sends it as our own.
func (r *Relay) sendOwn(plain []byte, now proto.Time) {
	var out [proto.MaxFrame]byte
	n, ok := r.Sealer.SealFrame(out[:], plain)
	if !ok {
		r.Stats.Unsealed++
		return
	}
	r.Send(out[:n], now)
}

func (r *Relay) probesDue() proto.Time {
	due := proto.Never
	for i := 0; i < r.nProbes; i++ {
		p := &r.probes[i]
		if !p.sent {
			due = min(due, p.due)
			continue
		}
		due = min(due, p.expires)
		if r.probeAwaiting(p) {
			due = min(due, p.due)
		}
	}
	return due
}
