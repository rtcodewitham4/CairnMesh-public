package wiring

import (
	"cairnmesh/firmware/ext"
	"cairnmesh/firmware/node"
)

// ProductLEDAction is the only action in a WLE5 production image.
const ProductLEDAction byte = 0x01

// Products is the production module set. The bench-action module is added
// separately by the cairn_testbed build.
type Products struct {
	LED ext.Switch
}

// Module lets the host test run exactly the production registration path.
func (p *Products) Module() node.Module { return &p.LED }
