package mesh

import "cairnmesh/firmware/proto"

// Waking a dormant network (spec 14.3) and a session's radio setting
// (15.2, Q11 / Q12).
//
// WAKE_NET floods like a REQUEST, but every copy goes out with a preamble
// of WakePreamble symbols, long enough for a radio that only samples the
// channel. A relay sends it again (at most WakeCopies copies) while a
// neighbour it knows well enough has not been heard since it woke. The
// originator repeats it (WakeRetries) until it hears a relay pass it on.
//
// A session's KEEPALIVE names a channel and SF; a relay on its route moves
// there once it has passed that keepalive's answer on, and comes back when
// it has heard nothing there for RadioTimeout.

// Tuner is implemented by a Radio that can change channel and SF.
type Tuner interface {
	SetRadio(channel, sf uint8)
}

// WakeGate may refuse to relay a fresh WAKE_NET (a rate limit against
// forged wake-ups, spec 14.2); WakeHeard hears that one this node sent was
// passed on.
type WakeGate interface {
	AllowWake(plain []byte) bool
}

// WakeHeard is told when a WAKE_NET this node originated was passed on.
type WakeHeard interface {
	WakeHeard()
}

const (
	// MaxRadioPending keepalives whose new setting waits for their answer.
	MaxRadioPending = 2
)

type radioSetting struct {
	channel, sf uint8
}

type radioPending struct {
	id requestID
	radioSetting
}

type radioFlight struct {
	origin byte
	fcnt   uint32
	radioSetting
}

func (r *Relay) airtime(n int) proto.Time { return r.cfg.Airtime(r.setting.sf, n) }

// symbol is one symbol at the current SF (2^SF / bandwidth).
func (r *Relay) symbol() proto.Time {
	return proto.Time((int64(1) << r.setting.sf) * int64(proto.Second) / int64(r.cfg.BandwidthHz))
}

// wakeLead is what a wake preamble adds to a frame's airtime.
func (r *Relay) wakeLead() proto.Time {
	return proto.Time(r.cfg.WakePreamble-8) * r.symbol()
}

func (r *Relay) wakeWindow() (proto.Time, proto.Time) {
	if r.cfg.WakeBackoffHigh > 0 {
		return r.cfg.WakeBackoffLow, r.cfg.WakeBackoffHigh
	}
	return r.cfg.BackoffLow, r.cfg.BackoffHigh
}

// wakeAckWait: the first relay backs off at most the window's top, then
// sends a long preamble and the frame.
func (r *Relay) wakeAckWait(length int) proto.Time {
	_, high := r.wakeWindow()
	return high + r.wakeLead() + 2*r.airtime(length) + r.cfg.RouteSlack
}

// wakeGap: long enough for the neighbours this copy woke to pass it on first.
func (r *Relay) wakeGap(length int) proto.Time {
	low, high := r.wakeWindow()
	return high + r.wakeLead() + r.airtime(length) + r.backoffIn(NoRSSI, low, high)
}

// wakeNeeded: is a neighbour this relay knows (at least WakeExpect) unheard
// since it woke? Then it may still be dormant, and a repeat is worth its
// long preamble.
func (r *Relay) wakeNeeded(f *flight) bool {
	for i := 0; i < r.nLinks; i++ {
		l := &r.links[i]
		if l.level >= r.cfg.WakeExpect && r.heardAt[l.node] < f.created {
			return true
		}
	}
	return false
}

// isWakeProbe: a wake probe, or (Q22) a wake JOIN: both go on long.
func isWakeProbe(plain []byte) bool {
	k := kindOf(plain)
	return (k == proto.KindProbe || k == proto.KindJoin) && len(plain) > 6 && plain[6]&probeWake != 0
}

// wakesUs: may this frame wake a dormant node: a WAKE_NET, a wake probe,
// or routed through or to it?
func (r *Relay) wakesUs(plain []byte) bool {
	if plain[0] == proto.KindWakeNet || isWakeProbe(plain) {
		return true
	}
	if plain[0]&proto.FlagRouted == 0 {
		return false
	}
	rt, ok := routeOf(plain)
	return ok && rt.index(r.cfg.Node) > 0
}

// answersWakeProbe: a PROBE_REPLY to a wake probe (or, Q22, the REPLY to a
// wake JOIN) this node passed on or answered: every hop back goes long.
func (r *Relay) answersWakeProbe(plain []byte) bool {
	if r.cfg.WakePreamble == 8 {
		return false
	}
	k := probeKey{origin: plain[2], id: proto.MessageID(plain)}
	switch kindOf(plain) {
	case proto.KindReply:
		k.kind = proto.KindJoin
	case proto.KindProbeReply:
	default:
		return false
	}
	p := r.findProbe(k)
	return p != nil && isWakeProbe(p.frame[:p.n])
}

// noteKeepalive: a routed KEEPALIVE through us naming another setting
// waits for its answer to pass.
func (r *Relay) noteKeepalive(plain []byte, rt *route) bool {
	if rt.index(r.cfg.Node) <= 0 {
		return true
	}
	cut, _, _ := proto.SplitRoute(plain)
	if cut != 7 || plain[6]&0x07 > 5 {
		r.Stats.BadRoute++
		return false
	}
	s := radioSetting{channel: plain[6] >> 3, sf: 7 + plain[6]&0x07}
	if s == r.setting {
		return true
	}
	id, _ := requestOf(plain)
	for i := 0; i < r.nRadioPending; i++ {
		if r.radioPending[i].id == id {
			r.radioPending[i].radioSetting = s
			return true
		}
	}
	if r.nRadioPending == MaxRadioPending {
		copy(r.radioPending[:], r.radioPending[1:])
		r.nRadioPending--
	}
	r.radioPending[r.nRadioPending] = radioPending{id, s}
	r.nRadioPending++
	return true
}

// followAnswer: a REPLY to a keepalive that named a new setting moves us
// once it has gone out.
func (r *Relay) followAnswer(origin byte, fcnt uint32, plain []byte) {
	if kindOf(plain) != proto.KindReply {
		return
	}
	id, _ := requestOf(plain)
	for i := 0; i < r.nRadioPending; i++ {
		if r.radioPending[i].id == id {
			if r.nRadioFlights < MaxFlights {
				r.radioFlights[r.nRadioFlights] = radioFlight{origin, fcnt, r.radioPending[i].radioSetting}
				r.nRadioFlights++
			}
			copy(r.radioPending[i:], r.radioPending[i+1:r.nRadioPending])
			r.nRadioPending--
			return
		}
	}
}

// moveDue: a move whose answer has gone out waits for the next Tick.
func (r *Relay) moveDue() bool {
	for i := 0; i < r.nRadioFlights; i++ {
		if r.find(r.radioFlights[i].origin, r.radioFlights[i].fcnt) < 0 {
			return true
		}
	}
	return false
}

// tickRadio follows the session once its answer is on its way, and comes
// home after RadioTimeout of silence on the session's setting.
func (r *Relay) tickRadio(now proto.Time) {
	for i := 0; i < r.nRadioFlights; {
		m := r.radioFlights[i]
		if r.find(m.origin, m.fcnt) >= 0 {
			i++
			continue
		}
		copy(r.radioFlights[i:], r.radioFlights[i+1:r.nRadioFlights])
		r.nRadioFlights--
		r.SetRadio(m.channel, m.sf, now)
	}
	if r.cfg.Forward && r.setting != r.home() && now-r.heardAny >= r.cfg.RadioTimeout {
		r.Stats.RadioReverted++
		r.SetRadio(0, r.cfg.BaseSF, now)
	}
}

func (r *Relay) home() radioSetting { return radioSetting{0, r.cfg.BaseSF} }

// SetRadio moves to channel at sf (the radio follows if it can); timing
// follows the SF's airtime, and silence counts from the move.
func (r *Relay) SetRadio(channel, sf uint8, now proto.Time) {
	s := radioSetting{channel, sf}
	if s == r.setting {
		return
	}
	r.setting = s
	r.Stats.RadioSwitches++
	r.heardAny = max(r.heardAny, now)
	if t, ok := r.radio.(Tuner); ok {
		t.SetRadio(channel, sf)
	}
}
