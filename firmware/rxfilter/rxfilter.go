// Package rxfilter is a TEST receive filter: a node drops frames by who
// transmitted them (the TX byte, spec 5), so a bench of nodes that all hear
// each other on the table behaves like a chosen topology (a line, a hidden
// terminal) while the air stays real.
//
// This is test equipment, not part of the protocol. No production package
// imports it: node/rxhook_on.go (build tag cairn_testfilter) and the
// cmd/*/testfilter_on.go files are its only importers, so a default build
// does not link a byte of it (make wle5-assert-prod checks the image).
//
// What it decides, and where:
//
//   - The hook (node.Stack, tag cairn_testfilter) runs after the frame's MIC
//     verified and before anything else looks at the frame: before the replay
//     window, the relay's windows and suppression, the neighbour table, the
//     route state. A frame that fails the MIC is not judged here; the stack
//     rejects it as it always does. A dropped frame leaves no trace in the
//     protocol, only here.
//   - The key is TX, the last hop: "who can hear whom" is about the node that
//     put this copy on the air. SENDER (the origin) would make a relayed frame
//     look as if the origin had been heard. Both bytes are in the clear
//     header, authenticated by the MIC (spec 5), so no decryption is needed.
//     Known gap: a copy replayed for the furthest holder (spec 11.2) carries
//     that holder's TX, not the transmitter's.
//   - Default: every TX passes, nothing is counted, nothing is logged; the
//     hook returns before it touches the frame (the self-check in the tests
//     compares a filtered and an unfiltered stack frame by frame).
//   - topo: the same undirected link list is given to every node; each node
//     hears exactly its neighbours in it. Symmetric by construction.
//   - asym: a one-way row ("I hear only these"), named, NON-PHYSICAL (a real
//     link is reciprocal in range), refused unless "asym-ok yes" came first.
//     Only for a protocol test that is about asymmetry; its result is not a
//     radio result and does not generalise.
//   - Every change is all-or-nothing and starts from the console only; the
//     air cannot reach it.
package rxfilter

import "cairnmesh/firmware/console"

const (
	// Version is the command set's version ("rxf caps").
	Version = 1
	// MaxLinks bounds the topology; a link is a pair of node IDs.
	MaxLinks = 24
	// LogSize dropped frames are kept; older ones are overwritten (counted).
	LogSize = 16
	// MaxName is the longest name of an asymmetric test.
	MaxName = 12
	// logPage bounds what one "rxf log" prints (the console's MaxOutput).
	logPage = 6
)

// Mode is what the filter does.
type Mode uint8

const (
	// PassAll is the default: nothing is dropped.
	PassAll Mode = iota
	// Topo: hear the neighbours in the symmetric link list only.
	Topo
	// Asym: hear the listed transmitters only (one way, non-physical).
	Asym
)

// config is everything "rxf" sets; it is replaced as a whole.
type config struct {
	mode   Mode
	hear   [32]byte // bit tx set: that transmitter is heard
	links  [MaxLinks][2]byte
	nLinks uint8
	name   [MaxName]byte
	nName  uint8
}

// Drop is one dropped frame. Ordinal counts drops from 1 since the last
// "rxf log clear"; (Sender, Fcnt) names the frame across the nodes' logs.
type Drop struct {
	Ordinal    uint32
	Fcnt       uint32
	MIC        [4]byte // the frame's own MIC: a hash to pair it with the other logs
	RSSI       int16
	HasRSSI    bool
	Sender, TX byte
	Kind       byte
}

// Filter is one node's filter. The zero value passes everything.
type Filter struct {
	cfg    config
	asymOK bool
	gen    uint32

	// Passed counts frames judged and let through (not counted while the
	// filter passes all), Dropped those dropped, Unauthentic frames that
	// failed the MIC (left to the stack).
	Passed, Dropped, Unauthentic uint32

	log [LogSize]Drop
}

// oldest is the ordinal of the oldest dropped frame still in the log.
func (f *Filter) oldest() uint32 {
	if f.Dropped <= LogSize {
		return 1
	}
	return f.Dropped - LogSize + 1
}

// Active reports whether the filter judges frames at all.
func (f *Filter) Active() bool { return f.cfg.mode != PassAll }

// ModeName is the mode for the status line.
func (f *Filter) ModeName() string {
	switch f.cfg.mode {
	case PassAll:
		return "mode all"
	case Topo:
		return "mode topo"
	}
	return "mode asym NONPHYSICAL"
}

// Allows reports whether a frame transmitted by tx is heard.
func (f *Filter) Allows(tx byte) bool {
	return f.cfg.mode == PassAll || f.cfg.hear[tx>>3]&(1<<(tx&7)) != 0
}

// Record keeps a dropped frame.
func (f *Filter) Record(d Drop) {
	f.Dropped++
	d.Ordinal = f.Dropped
	f.log[(f.Dropped-1)%LogSize] = d
}

// clearLog forgets the log and the counters (not the matrix).
func (f *Filter) clearLog() {
	f.Passed, f.Dropped, f.Unauthentic = 0, 0, 0
	f.log = [LogSize]Drop{}
}

// Version-1 command errors.
const (
	errNotRunning = "node is not running (no ID yet): nothing changed"
	errAsymOff    = "asym is off: 'rxf asym-ok yes' first (one-way links are not physical): nothing changed"
	errOneWay     = "a link is written A-B and holds both ways; one-way is 'rxf asym' only: nothing changed"
	errBadLink    = "bad link (want A-B, node IDs 1..254, A != B, comma or space separated): nothing changed"
	errTooMany    = "too many links: nothing changed"
	errBadName    = "bad name (1..12 of a-z 0-9 _ -): nothing changed"
	errLineCut    = "line longer than the console's 120 characters (the rest was dropped): nothing changed"
	errBadList    = "bad list (want IDs 1..254 separated by commas): nothing changed"
)

// Command runs "rxf" for the node with ID self. running is false before the
// node has an ID and a stack (no keys, a fault): reads work, changes do not.
func Command(c *console.Console, args [][]byte, self byte, running bool, f *Filter) {
	// A line the console cut short could still read as a valid, shorter command
	// (a topology that stops at an earlier link): nothing runs, not even a read.
	if c.LineCut() {
		c.Println(errLineCut)
		return
	}
	if len(args) == 1 {
		printStatus(c, f, self, running)
		return
	}
	switch string(args[1]) {
	case "caps":
		c.Print("rxf caps ")
		c.PrintUint(Version)
		c.Println(" key=tx modes=all,topo,asym atomic readback log=16 page=6")
	case "all":
		if len(args) != 2 {
			usage(c)
			return
		}
		f.cfg, f.asymOK = config{}, false
		f.gen++
		printStatus(c, f, self, running)
	case "topo":
		if !running {
			c.Println(errNotRunning)
			return
		}
		next := config{mode: Topo}
		if msg := parseLinks(&next, args[2:]); msg != "" {
			c.Println(msg)
			return
		}
		deriveHear(&next, self)
		f.cfg = next // all of it or none of it
		f.gen++
		printStatus(c, f, self, running)
	case "asym":
		if !running {
			c.Println(errNotRunning)
			return
		}
		if !f.asymOK {
			c.Println(errAsymOff)
			return
		}
		if len(args) != 4 {
			usage(c)
			return
		}
		next := config{mode: Asym}
		if !parseName(&next, args[2]) {
			c.Println(errBadName)
			return
		}
		if !parseList(&next, args[3]) {
			c.Println(errBadList)
			return
		}
		f.cfg = next
		f.gen++
		printStatus(c, f, self, running)
	case "asym-ok":
		if len(args) != 3 || (string(args[2]) != "yes" && string(args[2]) != "no") {
			usage(c)
			return
		}
		yes := string(args[2]) == "yes"
		if !yes && f.cfg.mode == Asym {
			// Closing the switch while an asymmetric row is live would leave a
			// one-way matrix running without its switch: go back to all instead.
			f.cfg = config{}
		}
		f.asymOK = yes
		f.gen++
		printStatus(c, f, self, running)
	case "log":
		printLog(c, args[2:], f)
	default:
		usage(c)
	}
}

func usage(c *console.Console) {
	c.Println("usage: rxf | rxf caps | rxf all | rxf topo A-B[,C-D ...] | rxf asym-ok yes|no | rxf asym NAME ID[,ID ...] | rxf log [FROM|clear]")
}

// printStatus prints the state in effect, the readback of every change.
func printStatus(c *console.Console, f *Filter, self byte, running bool) {
	c.Print("rxf TESTFILTER v")
	c.PrintUint(Version)
	c.PrintField("node", uint32(self))
	c.PrintField("gen", f.gen)
	switch f.cfg.mode {
	case PassAll:
		c.Print(" mode all")
	case Topo:
		c.Print(" mode topo")
	default:
		c.Print(" mode asym NONPHYSICAL name ")
		c.PrintBytes(f.cfg.name[:f.cfg.nName])
	}
	if f.asymOK {
		c.Print(" asym-ok yes")
	} else {
		c.Print(" asym-ok no")
	}
	if !running {
		c.Print(" (node not running)")
	}
	c.PrintField("passed", f.Passed)
	c.PrintField("dropped", f.Dropped)
	c.PrintField("unauth", f.Unauthentic)
	c.Println("")
	if f.cfg.mode == Topo {
		c.Print("rxf links ")
		for i := uint8(0); i < f.cfg.nLinks; i++ {
			if i > 0 {
				c.Print(",")
			}
			c.PrintUint(uint64(f.cfg.links[i][0]))
			c.Print("-")
			c.PrintUint(uint64(f.cfg.links[i][1]))
		}
		c.Println("")
	}
	if f.cfg.mode != PassAll {
		c.Print("rxf hear ")
		first := true
		for id := 1; id < 255; id++ {
			if f.cfg.hear[id>>3]&(1<<(id&7)) != 0 {
				if !first {
					c.Print(",")
				}
				first = false
				c.PrintUint(uint64(id))
			}
		}
		if first {
			c.Print("none")
		}
		c.Println("")
	}
}

// printLog prints up to logPage dropped frames from ordinal FROM on (the
// oldest kept when omitted), then where to continue and what is gone.
func printLog(c *console.Console, args [][]byte, f *Filter) {
	if len(args) == 1 && string(args[0]) == "clear" {
		f.clearLog()
		f.gen++
		c.Println("rxf log cleared")
		return
	}
	from := f.oldest()
	if len(args) > 1 {
		usage(c)
		return
	}
	if len(args) == 1 {
		v, ok := console.ParseUint(args[0])
		if !ok || v == 0 {
			usage(c)
			return
		}
		from = v
	}
	lost := uint32(0)
	if f.Dropped > 0 && from < f.oldest() {
		lost = f.oldest() - from
		from = f.oldest()
	}
	n := 0
	for ord := from; ord <= f.Dropped && n < logPage; ord++ {
		d := &f.log[(ord-1)%LogSize]
		c.Print("d ")
		c.PrintUint(uint64(d.Ordinal))
		c.PrintField("sender", uint32(d.Sender))
		c.PrintField("tx", uint32(d.TX))
		c.Print(" kind ")
		c.PrintHex([]byte{d.Kind})
		c.PrintField("fcnt", d.Fcnt)
		c.Print(" rssi ")
		if d.HasRSSI {
			if d.RSSI < 0 {
				c.Print("-")
				c.PrintUint(uint64(-int32(d.RSSI)))
			} else {
				c.PrintUint(uint64(d.RSSI))
			}
		} else {
			c.Print("?")
		}
		c.Print(" mic ")
		c.PrintHex(d.MIC[:])
		c.Println("")
		n++
		from = ord + 1
	}
	if n == 0 {
		from = f.Dropped + 1
	}
	c.Print("rxf log next ")
	c.PrintUint(uint64(from))
	c.PrintField("lost", lost)
	c.PrintField("total", f.Dropped)
	c.Println("")
}

// parseLinks reads A-B links (words and comma separated) into next.
func parseLinks(next *config, words [][]byte) string {
	if len(words) == 0 {
		return errBadLink
	}
	for _, w := range words {
		start := 0
		for i := 0; i <= len(w); i++ {
			if i < len(w) && w[i] != ',' {
				continue
			}
			item := w[start:i]
			start = i + 1
			if len(item) == 0 {
				return errBadLink
			}
			for _, ch := range item {
				if ch == '>' || ch == '<' {
					return errOneWay
				}
			}
			dash := -1
			for j, ch := range item {
				if ch == '-' {
					if dash >= 0 {
						return errBadLink
					}
					dash = j
				}
			}
			if dash < 0 {
				return errBadLink
			}
			a, okA := nodeID(item[:dash])
			b, okB := nodeID(item[dash+1:])
			if !okA || !okB || a == b {
				return errBadLink
			}
			if a > b {
				a, b = b, a
			}
			if !next.addLink(a, b) {
				return errTooMany
			}
		}
	}
	return ""
}

func (g *config) addLink(a, b byte) bool {
	for i := uint8(0); i < g.nLinks; i++ {
		if g.links[i] == [2]byte{a, b} {
			return true // said twice: once
		}
	}
	if g.nLinks == MaxLinks {
		return false
	}
	g.links[g.nLinks] = [2]byte{a, b}
	g.nLinks++
	return true
}

// deriveHear sets who self hears: its neighbours in the links, both ways of
// every link being the same link.
func deriveHear(g *config, self byte) {
	for i := uint8(0); i < g.nLinks; i++ {
		l := g.links[i]
		switch self {
		case l[0]:
			g.hear[l[1]>>3] |= 1 << (l[1] & 7)
		case l[1]:
			g.hear[l[0]>>3] |= 1 << (l[0] & 7)
		}
	}
}

func parseName(g *config, s []byte) bool {
	if len(s) == 0 || len(s) > MaxName {
		return false
	}
	for _, ch := range s {
		if !(ch >= 'a' && ch <= 'z' || ch >= '0' && ch <= '9' || ch == '_' || ch == '-') {
			return false
		}
	}
	g.nName = uint8(copy(g.name[:], s))
	return true
}

func parseList(g *config, s []byte) bool {
	start, n := 0, 0
	for i := 0; i <= len(s); i++ {
		if i < len(s) && s[i] != ',' {
			continue
		}
		id, ok := nodeID(s[start:i])
		if !ok {
			return false
		}
		start = i + 1
		g.hear[id>>3] |= 1 << (id & 7)
		n++
	}
	return n > 0
}

// nodeID reads a node ID, 1..254 (0 and 0xFF are not nodes).
func nodeID(s []byte) (byte, bool) {
	v, ok := console.ParseUint(s)
	if !ok || v < 1 || v > 254 {
		return 0, false
	}
	return byte(v), true
}
