package node

import "cairnmesh/firmware/proto"

// routeEnd is the device side of routed sessions (cairnmesh
// routed.py, RouteEndpoint): the device never sees a route. A routed
// REQUEST or WAKE goes up without its trailer and the device's REPLY goes
// back along the same route walked backwards; an ACCEPTED request keeps
// its route until its RESULT has gone back along it once. A KEEPALIVE is
// answered here (REPLY OK; it never reaches the duplicate cache). A request
// that arrives by flood is answered by flood, even if an earlier copy came
// routed: that route may be what just broke.
type routeEnd struct {
	answer  routeBack
	results [maxResultRoutes]routeBack // oldest first
	nResult int
	// leaseUntil: when the gateway's last KEEPALIVE says the session ends.
	leaseUntil proto.Time
	leased     bool
	Stats      struct{ RoutedIn, RoutedOut, Keepalives, Bad uint32 }
}

// maxResultRoutes: ACCEPTED requests whose RESULT may still take their route back.
const maxResultRoutes = 2

type routeBack struct {
	asker byte
	id    uint16
	back  [proto.MaxRelays]byte
	n     int
	ok    bool
}

// in takes a frame opened by the security layer. It returns the frame for
// the device without its route (deliver false: nothing goes up), and a
// KEEPALIVE's answer to send at once (reply non-nil), built in buf.
func (e *routeEnd) in(me byte, plain []byte, now proto.Time, buf []byte) (up []byte, deliver bool, reply []byte) {
	if plain[0]&proto.FlagRouted == 0 {
		if len(plain) >= 5 && (plain[0] == proto.KindRequest || plain[0] == proto.KindWake) && plain[2] == me {
			e.answer.ok = false // flooded: answer by flood
		}
		return plain, true, nil
	}
	cut, relays, ok := proto.SplitRoute(plain)
	if !ok || !routedLength(plain[0]&^proto.FlagRouted, cut) {
		e.Stats.Bad++
		return nil, false, nil
	}
	base := plain[:cut]
	base[0] &^= proto.FlagRouted
	if base[2] != me {
		return nil, false, nil
	}
	e.Stats.RoutedIn++
	switch base[0] {
	case proto.KindKeepalive:
		e.Stats.Keepalives++
		lease := proto.Time(base[5]) * proto.Second
		e.leased, e.leaseUntil = lease != 0, now+lease
		r := append(buf[:0], proto.KindReply|proto.FlagRouted, me, base[1], base[3], base[4], proto.ResultOK)
		return nil, false, appendBack(r, relays)
	case proto.KindRequest, proto.KindWake:
		e.answer = routeBack{asker: base[1], id: proto.MessageID(base), ok: true}
		e.answer.n = reverse(e.answer.back[:], relays)
	}
	return base, true, nil
}

// out puts the route back on the device's REPLY to the routed request (or
// on the RESULT of one it accepted), built in buf; otherwise the frame goes
// as it is.
func (e *routeEnd) out(plain, buf []byte) []byte {
	if len(plain) != 6 {
		return plain
	}
	switch {
	case plain[0] == proto.KindReply && e.answer.ok && plain[2] == e.answer.asker && proto.MessageID(plain) == e.answer.id:
		if plain[5] == proto.ResultAccepted {
			if e.nResult == maxResultRoutes {
				copy(e.results[:], e.results[1:])
				e.nResult--
			}
			e.results[e.nResult] = e.answer
			e.nResult++
		}
		e.Stats.RoutedOut++
		return withRoute(buf, plain, e.answer.back[:e.answer.n])
	case plain[0] == proto.KindResult:
		for i := 0; i < e.nResult; i++ {
			r := e.results[i]
			if r.asker == plain[2] && r.id == proto.MessageID(plain) {
				copy(e.results[i:], e.results[i+1:e.nResult])
				e.nResult--
				e.Stats.RoutedOut++
				return withRoute(buf, plain, r.back[:r.n])
			}
		}
	}
	return plain
}

// routedLength: a routed frame's own fields have their kind's length
// (frames.decode_routed).
func routedLength(kind byte, cut int) bool {
	switch kind {
	case proto.KindRequest, proto.KindKeepalive:
		return cut == 7
	case proto.KindReply:
		return cut == 6
	case proto.KindWake:
		return cut == 5
	}
	return true // the layers above check the others
}

func reverse(dst, relays []byte) int {
	for i, node := range relays {
		dst[len(relays)-1-i] = node
	}
	return len(relays)
}

// appendBack appends relays walked backwards and the RINFO byte.
func appendBack(dst, relays []byte) []byte {
	for i := len(relays) - 1; i >= 0; i-- {
		dst = append(dst, relays[i])
	}
	return append(dst, byte(len(relays)))
}

// withRoute is frames.add_route: ROUTED set, the relays, then RINFO.
func withRoute(buf, plain, relays []byte) []byte {
	out := append(buf[:0], plain[0]|proto.FlagRouted)
	out = append(out, plain[1:]...)
	out = append(out, relays...)
	return append(out, byte(len(relays)))
}
