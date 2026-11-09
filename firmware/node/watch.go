package node

import (
	"math"

	"cairnmesh/firmware/mesh"
	"cairnmesh/firmware/proto"
)

// Liveness without flooded heartbeats (spec 8; liveness.NeighbourWatch):
// a one-hop HELLO every interval, a table of the neighbours heard, and a
// LOST / RECOVERED REPORT flooded to the controller when a watched
// neighbour falls silent or comes back, repeated until acknowledged.

// Table sizes (spec 9).
const (
	MaxNeighbours = 16
	maxReports    = 4
)

// Report events (frames.ReportEvent).
const (
	ReportLost      = 0x00
	ReportRecovered = 0x01
)

// WatchConfig is liveness.NeighbourWatch's options (spec 18);
// DefaultWatchConfig has the defaults.
type WatchConfig struct {
	HelloInterval proto.Time
	// MissLimit silent intervals make a watched neighbour LOST.
	MissLimit int
	// ReportDelay before a report goes (another node's report about the
	// same cancels it); unacknowledged, again every ReportTimeout,
	// ReportRetries more times.
	ReportDelayMin, ReportDelayMax, ReportTimeout proto.Time
	ReportRetries                                 int
	// MaxNeighbours (at most the const) and MaxReports (at most 4).
	MaxNeighbours, MaxReports int
	// Neighbours whose smoothed RSSI is below WatchRSSI dBm are watched only
	// once known for WeakWindow intervals (0: never), on a fuse of the least
	// n with (1 − rate)ⁿ ≤ WeakFalseAlarm, if n ≤ MaxMiss. WatchAll: every
	// neighbour like a strong one.
	WatchRSSI      float64
	WatchAll       bool
	WeakWindow     int
	WeakFalseAlarm float64
	MaxMiss        int
}

// DefaultWatchConfig is liveness.NeighbourWatch's defaults (HELLO every 60 s).
func DefaultWatchConfig() WatchConfig {
	return WatchConfig{
		HelloInterval: 60 * proto.Second, MissLimit: 5,
		ReportDelayMin: 0, ReportDelayMax: 10 * proto.Second, ReportTimeout: 15 * proto.Second, ReportRetries: 3,
		MaxNeighbours: MaxNeighbours, MaxReports: maxReports,
		WatchRSSI: -118.5, WeakWindow: 10, WeakFalseAlarm: 1e-4, MaxMiss: 30,
	}
}

// WatchStats counts what the neighbour table did (NeighbourWatch.stats).
type WatchStats struct {
	Hellos, Lost, WeakSilent, Recovered                                        uint32
	ReportsSent, ReportsSuppressed, ReportsAcked, ReportsAbandoned, Overflowed uint32
}

type neighbour struct {
	node      byte
	lastHeard proto.Time
	rssi      mesh.RSSI // of the last HELLO; mesh.NoRSSI: none
	level     float64   // smoothed RSSI in dBm, once hasLevel
	hasLevel  bool
	lost      bool
	reported  bool // it went silent while watched: the controller was told
	heard     uint16
	since     proto.Time
}

type report struct {
	subject, event, seq byte
	sent                int
	due                 proto.Time
}

// Watch is one node's HELLOs, neighbour table and reports (Config.Watch on a Stack).
type Watch struct {
	cfg              WatchConfig
	node, controller byte
	epoch            byte
	events           *proto.Events // the sequence counter shared with module events
	rand             proto.Rand
	dev              *proto.Device // its State goes in every HELLO
	out              proto.FrameSender

	table     [MaxNeighbours]neighbour // in the order first heard
	n         int
	reports   [maxReports]report // in the order raised
	nReports  int
	helloSeq  byte
	nextHello proto.Time
	frame     [7]byte
	Stats     WatchStats
}

// zeroWatch clears a Watch: a composite literal this large would be
// allocated on the heap (TinyGo).
var zeroWatch Watch

func (w *Watch) init(cfg WatchConfig, node, controller, epoch byte, events *proto.Events, r proto.Rand, dev *proto.Device, out proto.FrameSender, now proto.Time) {
	*w = zeroWatch
	w.cfg, w.node, w.controller, w.epoch = cfg, node, controller, epoch
	w.events, w.rand, w.dev, w.out = events, r, dev, out
	w.cfg.MaxNeighbours = min(max(cfg.MaxNeighbours, 1), MaxNeighbours)
	w.cfg.MaxReports = min(max(cfg.MaxReports, 1), maxReports)
	w.nextHello = now + r.Uniform(0, cfg.HelloInterval)
}

// forget empties the table and the reports (a reboot: RAM is lost).
func (w *Watch) forget() { w.n, w.nReports = 0, 0 }

// resume: back from dormant, silence while the network slept proves
// nothing; every neighbour counts as heard just now.
func (w *Watch) resume(now proto.Time) {
	for i := 0; i < w.n; i++ {
		nb := &w.table[i]
		gap := max(0, now-nb.lastHeard)
		nb.lastHeard += gap
		nb.since += gap // no HELLOs while asleep: not a weaker link
	}
	w.nextHello = now + w.rand.Uniform(0, w.cfg.HelloInterval)
}

// Neighbours is how many live neighbours the table holds.
func (w *Watch) Neighbours() int {
	n := 0
	for i := 0; i < w.n; i++ {
		if !w.table[i].lost {
			n++
		}
	}
	return n
}

// missLimit is the silent intervals before nb counts as LOST; ok false: not watched.
func (w *Watch) missLimit(nb *neighbour, now proto.Time) (int, bool) {
	c := &w.cfg
	if c.WatchAll || !nb.hasLevel || nb.level >= c.WatchRSSI {
		return c.MissLimit, true
	}
	span := now - nb.since
	if c.WeakWindow <= 0 || span < proto.Time(c.WeakWindow)*c.HelloInterval {
		return 0, false // not known long enough to say how often it gets through
	}
	// HELLOs after the first over the intervals since, the silence included.
	rate := min(1.0, float64(nb.heard-1)*seconds(c.HelloInterval)/seconds(span))
	if rate >= 1.0 {
		return c.MissLimit, true
	}
	limit := math.Ceil(math.Log(c.WeakFalseAlarm) / math.Log(1.0-rate))
	if limit > float64(c.MaxMiss) {
		return 0, false
	}
	return max(c.MissLimit, int(limit)), true
}

func seconds(t proto.Time) float64 { return float64(t) / float64(proto.Second) }

// nextDue: the next HELLO, a neighbour's silence reaching its limit as it
// stands now (a weak one's only grows while it is silent: never early,
// never past), a weak one becoming known long enough, a report.
func (w *Watch) nextDue(now proto.Time) proto.Time {
	due := w.nextHello
	for i := 0; i < w.n; i++ {
		nb := &w.table[i]
		if nb.lost {
			continue
		}
		limit, watched := w.missLimit(nb, now)
		if !watched {
			limit = w.cfg.MissLimit
		}
		due = min(due, nb.lastHeard+proto.Time(limit)*w.cfg.HelloInterval)
		if !watched && w.cfg.WeakWindow > 0 && nb.hasLevel {
			if from := nb.since + proto.Time(w.cfg.WeakWindow)*w.cfg.HelloInterval; from > now {
				due = min(due, from)
			}
		}
	}
	for i := 0; i < w.nReports; i++ {
		due = min(due, w.reports[i].due)
	}
	return due
}

func (w *Watch) tick(now proto.Time) {
	if now >= w.nextHello {
		w.frame = [7]byte{proto.KindHello, w.node, proto.BroadcastID, w.helloSeq, w.dev.State}
		w.helloSeq++
		w.Stats.Hellos++
		w.out.Send(w.frame[:5])
		i := w.cfg.HelloInterval
		w.nextHello = now + w.rand.Uniform(i*85/100, i*115/100)
	}
	for i := 0; i < w.n; i++ {
		nb := &w.table[i]
		if nb.lost {
			continue
		}
		limit, watched := w.missLimit(nb, now)
		if !watched {
			limit = w.cfg.MissLimit
		}
		if now-nb.lastHeard < proto.Time(limit)*w.cfg.HelloInterval {
			continue
		}
		nb.lost = true
		if !watched {
			w.Stats.WeakSilent++
			continue
		}
		nb.reported = true
		w.Stats.Lost++
		w.raise(nb.node, ReportLost, now)
	}
	for i := 0; i < w.nReports; {
		r := &w.reports[i]
		if now < r.due {
			i++
			continue
		}
		if r.sent > w.cfg.ReportRetries {
			w.dropReport(i)
			w.Stats.ReportsAbandoned++
			continue
		}
		w.frame = [7]byte{proto.KindReport, w.node, w.controller, r.seq, w.epoch, r.subject, r.event}
		w.Stats.ReportsSent++
		w.out.Send(w.frame[:7])
		r.sent++
		r.due = now + w.cfg.ReportTimeout
		i++
	}
}

func (w *Watch) findReport(subject byte) int {
	for i := 0; i < w.nReports; i++ {
		if w.reports[i].subject == subject {
			return i
		}
	}
	return -1
}

func (w *Watch) dropReport(i int) {
	copy(w.reports[i:w.nReports], w.reports[i+1:w.nReports])
	w.nReports--
}

func (w *Watch) raise(subject, event byte, now proto.Time) {
	i := w.findReport(subject)
	if i >= 0 && w.reports[i].sent == 0 && w.reports[i].event != event {
		w.dropReport(i) // it flapped back before anyone was told
		return
	}
	if i < 0 {
		if w.nReports >= w.cfg.MaxReports {
			w.Stats.Overflowed++
			return
		}
		i = w.nReports
		w.nReports++
	}
	w.reports[i] = report{subject: subject, event: event, seq: w.events.NextSequence(),
		due: now + w.rand.Uniform(w.cfg.ReportDelayMin, w.cfg.ReportDelayMax)}
}

// heard is the relay observer's share: every authenticated first copy.
func (w *Watch) heard(plain []byte, rssi mesh.RSSI, now proto.Time) {
	switch {
	case plain[0] == proto.KindReport|proto.FlagApp && len(plain) == 12:
		// v3 (review C1): SUBJECT and EVENT in clear under the reporter's application key.
		w.heardReport(plain[1], plain[6], plain[7])
	case plain[0] == proto.KindHello && len(plain) == 5 && plain[4] <= proto.StateReady:
		w.onHello(plain[1], rssi, now)
	case plain[0] == proto.KindReport && len(plain) == 7 && plain[6] <= ReportRecovered:
		w.heardReport(plain[1], plain[5], plain[6])
	case plain[0] == proto.KindReply && len(plain) == 6 &&
		plain[2] == w.node && plain[1] == w.controller && plain[4] == w.epoch:
		for i := 0; i < w.nReports; {
			if r := &w.reports[i]; r.sent > 0 && r.seq == plain[3] {
				w.dropReport(i)
				w.Stats.ReportsAcked++
				continue
			}
			i++
		}
	}
}

func (w *Watch) heardReport(sender, subject, event byte) {
	if sender == w.node {
		return
	}
	// Someone else already told the controller.
	if i := w.findReport(subject); i >= 0 && w.reports[i].sent == 0 && w.reports[i].event == event {
		w.dropReport(i)
		w.Stats.ReportsSuppressed++
	}
}

func (w *Watch) onHello(node byte, rssi mesh.RSSI, now proto.Time) {
	for i := 0; i < w.n; i++ {
		nb := &w.table[i]
		if nb.node != node {
			continue
		}
		nb.lastHeard, nb.rssi = now, rssi
		if nb.heard < math.MaxUint16 {
			nb.heard++
		}
		if rssi != mesh.NoRSSI {
			dbm := float64(rssi) / 1024
			if nb.hasLevel {
				nb.level = 0.75*nb.level + 0.25*dbm
			} else {
				nb.level, nb.hasLevel = dbm, true
			}
		}
		if nb.lost {
			nb.lost = false
			if nb.reported {
				nb.reported = false
				w.Stats.Recovered++
				w.raise(node, ReportRecovered, now)
			}
		}
		return
	}
	if w.n >= w.cfg.MaxNeighbours {
		// Make room: forget whoever was heard least recently.
		oldest := 0
		for i := 1; i < w.n; i++ {
			if w.table[i].lastHeard < w.table[oldest].lastHeard {
				oldest = i
			}
		}
		copy(w.table[oldest:w.n], w.table[oldest+1:w.n])
		w.n--
	}
	nb := neighbour{node: node, lastHeard: now, rssi: rssi, heard: 1, since: now}
	if rssi != mesh.NoRSSI {
		nb.level, nb.hasLevel = float64(rssi)/1024, true
	}
	w.table[w.n] = nb
	w.n++
}
