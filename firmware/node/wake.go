package node

import (
	"cairnmesh/firmware/mesh"
	"cairnmesh/firmware/proto"
)

// The dormant / awake state of a node in a sleeping network (spec 14.2;
// wake.WakeAgent). Dormant, the radio only samples for preambles and the
// relay takes only frames that may wake the node; a WAKE_NET wakes it for
// its LEASE (paid from an awake-time budget), a wake probe for its window,
// a routed frame through or to it for a while, a session's KEEPALIVE for
// its lease; SLEEP_NET ends the lease. It goes dormant again once the
// lease is over and the relay has nothing left to send.

// Sampler is implemented by an air that can sample for preambles only (the
// SX126x's duty-cycled receive; spec 14.2).
type Sampler interface {
	SetDormant(dormant bool)
}

// Table sizes of the wake layer (spec 9).
const (
	maxWakeTimes = 16
	maxSeenWakes = 4
)

// WakeConfig is wake.WakeAgent's options (spec 18); DefaultWakeConfig has the defaults.
type WakeConfig struct {
	// MaxWakes WAKE_NET (and wake JOINs) and MaxProbeWakes wake probes are
	// honoured per WakePeriod; at most maxWakeTimes each.
	MaxWakes, MaxProbeWakes int
	WakePeriod              proto.Time
	// Hold past a KEEPALIVE's lease, RouteHold after another routed frame,
	// ProbeHold past a wake probe's window.
	Hold, RouteHold, ProbeHold proto.Time
	// NetAwakeQuota of awake time WAKE_NET leases may add per QuotaPeriod
	// (review B2); 0: no budget.
	NetAwakeQuota, QuotaPeriod proto.Time
	// Dormant: start sampling, waiting to be woken.
	Dormant bool
}

// DefaultWakeConfig is wake.WakeAgent's defaults.
func DefaultWakeConfig() WakeConfig {
	return WakeConfig{
		MaxWakes: 3, MaxProbeWakes: 10, WakePeriod: 60 * proto.Second,
		Hold: 5 * proto.Second, RouteHold: 10 * proto.Second, ProbeHold: 3 * proto.Second,
		NetAwakeQuota: 600 * proto.Second, QuotaPeriod: 3600 * proto.Second,
		Dormant: true,
	}
}

// WakeStats counts what the wake layer did (wake.WakeAgent.stats).
type WakeStats struct {
	Woken, WokenByRoute, WokenByBoot, Slept, Refused, ToldToSleep, Held, OverBudget, LeaseCut uint32
}

type wakeTimes struct {
	t [maxWakeTimes]proto.Time // oldest first
	n int
}

// admit drops the times up to now − period and adds now if fewer than limit remain.
func (w *wakeTimes) admit(now, period proto.Time, limit int) bool {
	k := 0
	for k < w.n && w.t[k] <= now-period {
		k++
	}
	copy(w.t[:], w.t[k:w.n])
	w.n -= k
	if w.n >= limit || w.n >= maxWakeTimes {
		return false
	}
	w.t[w.n] = now
	w.n++
	return true
}

type seenWake struct {
	origin byte
	id     uint16
}

// Waker is one node's wake layer (Config.Wake on a Stack).
type Waker struct {
	cfg   WakeConfig
	relay *mesh.Relay
	air   Sampler
	node  byte
	// onWake is called when the node wakes (the neighbour table, the events).
	onWake func(now proto.Time)

	budget, budgetAt      proto.Time
	wakes, probeWakes     wakeTimes
	seen                  [maxSeenWakes]seenWake // oldest first
	nSeen                 int
	decided               [proto.MaxFrame]byte // the frame the relay last asked about
	nDecided              int
	allowed               bool
	leaseUntil, holdUntil proto.Time
	now                   proto.Time
	Stats                 WakeStats
}

// zeroWaker clears a Waker: a composite literal this large would be
// allocated on the heap (TinyGo).
var zeroWaker Waker

func (w *Waker) init(cfg WakeConfig, relay *mesh.Relay, air Sampler, node byte, onWake func(proto.Time), now proto.Time) {
	*w = zeroWaker
	w.cfg, w.relay, w.air, w.node = cfg, relay, air, node
	w.budget, w.budgetAt = cfg.NetAwakeQuota, now
	w.leaseUntil, w.holdUntil = -1, -1
	w.now = now
	relay.Gate = w
	w.set(cfg.Dormant, now)
	// WakeAgent installs its callbacks after construction, so starting awake
	// does not call them; later transitions still do.
	w.onWake = onWake
}

// Awake reports whether the protocol runs (dormant: only the relay and this layer).
func (w *Waker) Awake() bool { return !w.relay.Asleep }

func (w *Waker) set(dormant bool, now proto.Time) {
	if dormant {
		w.relay.Home(now) // samples the common setting
	}
	w.relay.Asleep = dormant
	if w.air != nil {
		w.air.SetDormant(dormant)
	}
	if !dormant && w.onWake != nil {
		w.onWake(now)
	}
}

func (w *Waker) refill(now proto.Time) {
	q := w.cfg.NetAwakeQuota
	gap := max(0, min(now-w.budgetAt, w.cfg.QuotaPeriod))
	w.budget = min(q, w.budget+gap*q/w.cfg.QuotaPeriod)
	w.budgetAt = now
}

func (w *Waker) allow(now proto.Time, plain []byte) bool {
	if plain[0] == proto.KindWakeNet && w.cfg.NetAwakeQuota > 0 {
		w.refill(now)
		if w.budget < proto.Second {
			w.Stats.OverBudget++
			return false
		}
	}
	times, limit := &w.wakes, w.cfg.MaxWakes
	if plain[0] == proto.KindProbe {
		times, limit = &w.probeWakes, w.cfg.MaxProbeWakes
	}
	if !times.admit(now, w.cfg.WakePeriod, limit) {
		w.Stats.Refused++
		return false
	}
	return true
}

// AllowWake is the relay's wake gate (mesh.WakeGate): the decision is kept
// for the observer, which sees the same frame next.
func (w *Waker) AllowWake(plain []byte) bool {
	w.allowed = w.allow(w.now, plain)
	w.nDecided = copy(w.decided[:], plain)
	return w.allowed
}

func (w *Waker) admitted(plain []byte, now proto.Time) bool {
	if w.nDecided > 0 && string(w.decided[:w.nDecided]) == string(plain) {
		w.nDecided = 0
		return w.allowed
	}
	return w.allow(now, plain) // the relay did not ask (full, or not forwarding)
}

// budgeted: a WAKE_NET lease to until, cut to what the awake budget still pays for.
func (w *Waker) budgeted(now, until proto.Time) proto.Time {
	if w.cfg.NetAwakeQuota <= 0 {
		return until
	}
	w.refill(now)
	start := max(now, w.leaseUntil, w.holdUntil) // awake anyway until then
	extra := max(0, until-start)
	granted := min(extra, w.budget)
	if granted < extra {
		w.Stats.LeaseCut++
	}
	w.budget -= granted
	if granted > 0 {
		return start + granted
	}
	return w.leaseUntil
}

func (w *Waker) wake(now, until proto.Time, counter *uint32) {
	w.leaseUntil = max(w.leaseUntil, until)
	if !w.Awake() {
		*counter++
		w.set(false, now)
	}
}

// heard is the relay observer's share: every authenticated first copy.
func (w *Waker) heard(plain []byte, now proto.Time) {
	w.now = now
	switch k := plain[0]; {
	case k == proto.KindWakeNet:
		if !w.admitted(plain, now) || len(plain) != 6 || plain[2] != proto.BroadcastID {
			return
		}
		if until := w.budgeted(now, now+proto.Time(plain[5])*proto.Second); until > now {
			w.wake(now, until, &w.Stats.Woken)
		}
	case k == proto.KindSleepNet:
		if w.Awake() {
			w.Stats.ToldToSleep++
			w.leaseUntil, w.holdUntil = now, now // asleep once the relay has passed it on
		}
	case (k == proto.KindProbe || k == proto.KindJoin) && len(plain) > 6 && plain[6]&0x80 != 0:
		// A wake probe, or (Q22) a booting node's wake JOIN.
		if !w.admitted(plain, now) || !mesh.IsPathFrame(plain) {
			return
		}
		// Every relay seals its copy as its own frame: only the first copy sets the lease.
		key := seenWake{origin: plain[5], id: proto.MessageID(plain)}
		for i := 0; i < w.nSeen; i++ {
			if w.seen[i] == key {
				return
			}
		}
		if w.nSeen == maxSeenWakes {
			copy(w.seen[:], w.seen[1:])
			w.nSeen--
		}
		w.seen[w.nSeen] = key
		w.nSeen++
		until := now + proto.Time(plain[6]&0x7F)*mesh.WindowUnit + w.cfg.ProbeHold
		if plain[2] == w.node && k == proto.KindProbe {
			// The target answers after the window; its answer crosses every
			// hop with a long preamble before the first keepalive comes back.
			until += w.cfg.RouteHold + proto.Time(mesh.PathHops(plain)+1)*w.relay.ReplyHop()
		}
		w.wake(now, until, &w.Stats.Woken)
	case k&proto.FlagRouted != 0:
		cut, relays, ok := proto.SplitRoute(plain)
		if !ok {
			return
		}
		through := false
		for _, n := range relays {
			through = through || n == w.node
		}
		if !through && plain[2] != w.node {
			return
		}
		keepalive := k&proto.KindMask == proto.KindKeepalive
		if keepalive && cut < 7 {
			return
		}
		w.Stats.Held++
		if keepalive && plain[5] == 0 {
			w.holdUntil = now // the session closed; a network lease still holds
			return
		}
		hold := w.cfg.RouteHold
		if keepalive {
			hold = proto.Time(plain[5])*proto.Second + w.cfg.Hold
		}
		w.holdUntil = max(w.holdUntil, now+hold)
		w.wake(now, w.leaseUntil, &w.Stats.WokenByRoute)
	}
}

// boot: the node just started in a sleeping network; awake until until to
// send its JOIN and hear the answer (Q22).
func (w *Waker) boot(now, until proto.Time) { w.wake(now, until, &w.Stats.WokenByBoot) }

// nextDue is the earliest time tick can put the node to sleep (dormant:
// only a frame wakes it).
func (w *Waker) nextDue() proto.Time {
	if !w.Awake() {
		return proto.Never
	}
	due := max(w.leaseUntil, w.holdUntil)
	if w.relay.Pending() > 0 {
		return max(due, w.relay.NextDue())
	}
	return due
}

func (w *Waker) tick(now proto.Time) {
	w.now = now
	if w.Awake() && now >= max(w.leaseUntil, w.holdUntil) && w.relay.Pending() == 0 {
		w.Stats.Slept++
		w.set(true, now)
	}
}
