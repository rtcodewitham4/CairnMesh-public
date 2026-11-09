package mesh

import "cairnmesh/firmware/proto"

// What the layers above the relay read from it (node.Stack: the wake,
// neighbour and discovery layers, spec 17).

// IsPathFrame checks a flooded PROBE / JOIN / DISCOVER's layout like
// frames.ProbeFrame.from_bytes.
func IsPathFrame(plain []byte) bool { return len(plain) > 0 && pathFrame(plain) }

// PathHops is the number of hops a path frame lists.
func PathHops(plain []byte) int { return hops(plain) }

// PathHead is where a path frame's hops begin (7; a DISCOVER 8).
func PathHead(kind byte) int { return head(kind) }

// Home puts the radio back on the common setting (a node going dormant
// samples there).
func (r *Relay) Home(now proto.Time) { r.SetRadio(0, r.cfg.BaseSF, now) }

// WakeNetwork reports whether wake frames go with a long preamble (a
// sleeping network): only then does a booting node send wake JOINs (Q22).
func (r *Relay) WakeNetwork() bool { return r.cfg.WakePreamble != 8 }

// ReplyHop is one hop of a wake probe's answer on its way back: a long
// preamble, a 48-byte frame, the routed jitter and slack (wake.WakeAgent).
func (r *Relay) ReplyHop() proto.Time {
	return r.wakeLead() + r.airtime(48) + r.cfg.RouteJitter + r.cfg.RouteSlack
}

// NoiseFloor: SNR = RSSI − it where the radio gives none.
func (r *Relay) NoiseFloor() RSSI { return r.cfg.NoiseFloor }
