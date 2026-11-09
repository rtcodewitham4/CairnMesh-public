// Package proto is the device side of the CairnMesh protocol, wire format v5
// (cairnmesh/protocol_spec.md). The Python stack in cairnmesh is
// the reference: every rule here names the spec section it comes from, and
// the host tests replay the same vector files (tests/vectors) as the Python
// tests.
//
// The package is pure Go: it builds for the host (go test) and for the
// STM32U083 (TinyGo). Nothing allocates after construction; frames live in
// caller-owned buffers, tables are fixed arrays (spec section 9).
package proto

// Frame kinds, the low 5 bits of the first byte (spec section 2).
const (
	KindRequest       = 0x01
	KindReply         = 0x02
	KindWake          = 0x03
	KindHeartbeat     = 0x04
	KindHello         = 0x05
	KindReport        = 0x06
	KindKeepalive     = 0x07
	KindProbe         = 0x08
	KindProbeReply    = 0x09
	KindRouteError    = 0x0A
	KindTrace         = 0x0B
	KindTraceReply    = 0x0C
	KindJoin          = 0x0D
	KindDiscover      = 0x0E
	KindDiscoverReply = 0x0F
	KindWakeNet       = 0x10
	KindSleepNet      = 0x11
	KindResult        = 0x12
	KindEvent         = 0x13 // a module's event, wire format v4 (spec section 19)
)

// Flags in the KIND byte (spec section 16).
const (
	FlagSecure = 0x80 // sealed frame (always set on air)
	FlagRouted = 0x40 // a route trailer R1..RN | RINFO follows the fields
	FlagApp    = 0x20 // application layer inside (below the security layer only)
	KindMask   = 0x1F
)

// REPLY / RESULT values (spec section 2).
const (
	ResultOK       = 0x00
	ResultError    = 0x01
	ResultNotRun   = 0x02
	ResultAccepted = 0x03
	// ResultInterrupted: a reboot ended an action without a final result;
	// it may have completed, and its safe state has been applied (spec 4.1).
	ResultInterrupted = 0x04
)

// Node states carried by HEARTBEAT and HELLO.
const (
	StateSleep = 0x00
	StateReady = 0x01
)

const (
	DefaultControllerID = 0x42
	BroadcastID         = 0xFF
	// QueryAction marks a REQUEST as a status query (spec 4.1).
	QueryAction = 0xFF
	// MaxRelays is the longest route: 16 relays, 17 hops (spec section 2).
	MaxRelays = 16
	// MaxFrame is the largest frame on air: a LoRa packet.
	MaxFrame = 255
)

// Fixed plain lengths of the kinds that have one (spec section 3, 11.1, 14.1).
func fixedLen(kind byte) int {
	switch kind {
	case KindWake, KindHeartbeat, KindHello, KindSleepNet:
		return 5
	case KindReply, KindResult, KindEvent, KindRouteError, KindWakeNet:
		return 6
	case KindRequest, KindReport, KindKeepalive, KindTrace:
		return 7
	}
	return 0
}

// appLen is the plain length of the kinds the application layer seals
// (frames.APP_KINDS), 0 for the others.
func appLen(kind byte) int {
	switch kind {
	case KindRequest, KindReport:
		return 7
	case KindReply, KindResult, KindEvent:
		return 6
	}
	return 0
}

// MessageID is EPOCH (high byte) | SEQ (low byte) of a frame that has an
// EPOCH (spec section 3).
func MessageID(frame []byte) uint16 { return uint16(frame[4])<<8 | uint16(frame[3]) }

// RouteCut returns where the route trailer of a routed plain frame begins:
// the frame's own fields are frame[:cut] (with ROUTED still set in the KIND
// byte). Like secure._split it only reads RINFO, the last byte; ok is false
// when the trailer leaves less than a frame head of 5 bytes. An unrouted
// frame returns len(frame).
func RouteCut(frame []byte) (cut int, ok bool) {
	if len(frame) == 0 {
		return 0, false
	}
	if frame[0]&FlagRouted == 0 {
		return len(frame), true
	}
	cut = len(frame) - 1 - int(frame[len(frame)-1]&0x1F)
	return cut, cut >= 5
}

// SplitRoute checks a routed plain frame like frames.split_route and returns
// the cut (see RouteCut) and the relays in transmission order, a view into
// frame.
func SplitRoute(frame []byte) (cut int, relays []byte, ok bool) {
	if len(frame) < 5 || frame[0]&FlagRouted == 0 {
		return 0, nil, false
	}
	info := frame[len(frame)-1]
	count := int(info & 0x1F)
	if info>>5 != 0 || count > MaxRelays || len(frame) < 4+count+1 {
		return 0, nil, false
	}
	cut = len(frame) - 1 - count
	relays = frame[cut : len(frame)-1]
	sender, target := frame[1], frame[2]
	for i, node := range relays {
		if node == sender || node == target || node == BroadcastID {
			return 0, nil, false
		}
		for _, earlier := range relays[:i] {
			if earlier == node {
				return 0, nil, false
			}
		}
	}
	return cut, relays, true
}
