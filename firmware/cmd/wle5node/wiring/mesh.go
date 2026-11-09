// Package wiring is what cmd/wle5node decides without a radio: how the node's
// multi-hop layers are configured (relay, HELLO, JOIN / discovery, route
// endpoint) and what its transmit power is. It is separate from main only so
// the host can test it (main builds for the WLE5 target alone, as cwpower).
package wiring

import (
	"cairnmesh/firmware/mesh"
	"cairnmesh/firmware/node"
	"cairnmesh/firmware/proto"
	"cairnmesh/firmware/radio/sx126x"
)

// The air parameters the relay's timing is computed for: those of cmd/wle5node
// (433.5 MHz channel 0, SF7, 125 kHz, CR 4/5, preamble 8), the same as the
// vectors' relay.
const (
	BaseSF         = 7
	BandwidthHz    = 125_000
	CodingRate     = 5
	PreambleSymbol = 8
)

// Mesh holds the multi-hop node's configuration and its relay (large: a
// global, never on the stack). Apply puts them into the node.Config.
//
// Layers: relay (spec 7, 17), HELLO and neighbour table (spec 8), JOIN and scan
// answers (spec 12.3), and the route endpoint the node.Stack builds itself
// once a relay is present (it answers PROBE / KEEPALIVE for sessions through
// this node, spec 17). Not here: the wake layer (14.2; it needs the radio's
// duty-cycled receive, node.Sampler, which the SX126x driver does not offer
// yet: the node stays awake, Device.Awake). Every value is the library's
// default, which is the Python default and what the vectors pin.
type Mesh struct {
	Relay     mesh.Relay
	Config    mesh.Config
	Watch     node.WatchConfig
	Discovery node.DiscoveryConfig
}

func airtime(sf uint8, n int) proto.Time {
	return sx126x.Airtime(sf, BandwidthHz, CodingRate, PreambleSymbol, n)
}

// Apply makes cfg a multi-hop node: Relay, Mesh, Watch and Discovery set, and
// the single-hop HEARTBEAT switched off (HELLO replaces it, spec 3 and 18).
// The rest of cfg (keys, air, stores) is left as it was. Wake stays nil.
func (m *Mesh) Apply(cfg *node.Config) {
	m.Config = mesh.DefaultConfig(cfg.Device.ID, airtime)
	m.Watch = node.DefaultWatchConfig()
	m.Discovery = node.DefaultDiscoveryConfig()
	cfg.Relay, cfg.Mesh = &m.Relay, &m.Config
	cfg.Watch, cfg.Discovery = &m.Watch, &m.Discovery
	cfg.Device.HeartbeatInterval = proto.Never
}
