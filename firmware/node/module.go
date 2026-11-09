// Package node assembles one node: the protocol (proto.Secure under
// proto.Device, spec section 17) and the modules that give the node its
// functions. Modules never see the protocol. They reach the node through a
// Host: actions the controller can run (Handle), the hardware they use
// (Claim), signals outside the protocol (Emit), events for the controller
// (Notify), and their own timers (NextDue, Tick). So a special node is the
// common firmware plus its modules; the protocol code is the same on every
// node.
//
// Pure Go like proto: it builds for the host (go test) and TinyGo, and
// nothing allocates after Start.
package node

import "cairnmesh/firmware/proto"

// Module is one function of a node. The Stack calls Start once, in the
// order modules were added, then Tick whenever NextDue has come. Tick must
// return quickly (well under a millisecond: frames wait meanwhile); longer
// work is a state machine over several Ticks, or an Action with a Duration.
type Module interface {
	// Start registers the module's actions and claims its hardware through
	// h, which stays valid for the module's life; false stops the boot.
	Start(h *Host) bool
	// NextDue is the earliest time Tick has something to do; proto.Never:
	// only actions drive it.
	NextDue() proto.Time
	Tick(now proto.Time)
}

// Action is what a REQUEST runs: ACTION picks it, VALUE is its argument.
// The protocol runs each request at most once (spec 4.1), across reboots
// too: an action never sees a retry or a late copy.
type Action interface {
	// Duration is how long the action takes (0: instant). Up to the
	// answer budget the answer waits for it; longer ones answer ACCEPTED and
	// the result follows in a RESULT.
	Duration(value byte) proto.Time
	// Perform starts the action and returns its result, proto.ResultOK or
	// proto.ResultError, which counts once Duration is over. An action whose
	// result shows only while it runs returns proto.ResultPending instead,
	// keeps Host.Ticket and calls Host.Complete when it knows; Duration is
	// then its deadline (spec 4.1, result known later).
	Perform(value byte) byte
}

// Func is an instant action: Perform calls it, Duration is 0. Build it at
// Start (a func value converted to an interface is allocated once).
type Func func(value byte) byte

func (f Func) Duration(byte) proto.Time { return 0 }
func (f Func) Perform(value byte) byte  { return f(value) }

// Resource is a piece of hardware one module owns: a pin (its machine.Pin
// number, below 0x100) or a peripheral.
type Resource uint16

// Pin is the resource of a pin, by its machine.Pin number (PA0 = 0, PB0 =
// 16, ...).
func Pin(n uint8) Resource { return Resource(n) }

// Peripherals. The radio itself is not one: the Stack shares it (Host.Emit).
const (
	ADC Resource = 0x100 + iota
	UART1
	UART2
	UART3
	UART4
	SPI1
	SPI2
	I2C1
	I2C2
	I2C3
	I2C4
	DAC
	LPTIM1
	LPTIM2
	TIM2
	TIM3
	// User is the first number free for resources a deployment names itself.
	User Resource = 0x200
)
