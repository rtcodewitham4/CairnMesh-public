package wiring

import (
	"cairnmesh/firmware/board/wle5/adapt"
	"cairnmesh/firmware/node"
	"cairnmesh/firmware/radio"
)

// Build is how cmd/wle5node puts the radio under the node: the transmit
// interlock first, then the multi-hop layers. main calls only this, so what it
// builds is what the host tests build.
//
// gate and gatedEmit are the caller's (main reads their Refused counts and asks
// gate.Quiet); Build sets them over the radio. cfg.Air and cfg.Emitter become
// the gates, never the radio itself: with allow false (no WLE5_ANT_OK=1) not one
// packet or signal reaches the radio. mesh.Apply then adds relay, HELLO, JOIN
// and the route endpoint.
func Build(cfg node.Config, air radio.Air, emitter radio.Emitter, allow bool,
	gate *adapt.GatedAir, gatedEmit *adapt.GatedEmitter, m *Mesh) node.Config {
	*gate = adapt.GatedAir{Air: air, Allow: allow}
	*gatedEmit = adapt.GatedEmitter{E: emitter, Allow: allow}
	cfg.Air, cfg.Emitter = gate, gatedEmit
	m.Apply(&cfg)
	return cfg
}
