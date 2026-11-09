// Command node is the device firmware on the bench: the common node
// (node.Stack: the protocol, spec section 17) plus this build's modules.
// Until a board carries a radio, the ST-LINK serial port (USART2, SLIP +
// CRC16) is its air (board.SerialAir): the PC runs the controller
// (cairnmesh LinkSession.serial, see tools/serial_bench.py), and
// signals outside the protocol go to board.BenchEmitter, which only holds
// the "radio" for their duration.
//
// A special node is this file with other modules: everything below the
// module list is the same on every node.
//
// The node's address and keys are not in the image: they are
// provisioned over the USB console ("keys set") into their flash page
// (board.KeyPage). Without them the node stays off the network and only
// the console runs. The bench board is provisioned with the test keys of
// tests/vectors by tools/provision.py.
//
// A chip without read protection (RDP level 0) does not join the network
// either: anyone could read its keys out. The bench board stays
// at level 0, so the bench builds with BENCH=1 (-X main.benchBuild=1), which
// allows it; the default build is the deployment build.
//
// Actions of this build:
//
//	0x01  user LED (PA5): VALUE 0 off, else on                    ext.Switch
//	0x02  slow action of VALUE × 0.1 s (past 0.6 s: ACCEPTED, then RESULT)
//	0x03  fails (ERROR)
//	0x04  does nothing, at once (the bench's burst)
//	0x05  result known later: completes OK after VALUE × 0.1 s, deadline
//	      5 s (past it: ERROR); ACCEPTED first, the RESULT on completion
//	0x10  arms (VALUE ≠ 0) / disarms (0) the B1 alarm             ext.Watch
//	0x11  raises the B1 interrupt by software (a press without a hand)
//
// The B1 alarm: the user button (PC13) pressed or released sends a 500 ms
// carrier at full power outside the protocol and notifies the controller
// (EVENT eventButton, spec 19), at most once per 2 s. The supply watch does
// the same (EVENT eventLowSupply) when VDDA falls below 3.0 V.
//
// A failed boot blinks the LED, short flashes then a pause, forever; the
// node never joins the network: 1 flash no AES, 2 no ADC, index + 3 the
// module that stopped the boot (two modules on one pin or action, see
// node.Fault).
package main

import (
	"machine"
	"runtime"

	"cairnmesh/firmware/board"
	"cairnmesh/firmware/ext"
	"cairnmesh/firmware/hw/cyc"
	"cairnmesh/firmware/hw/extihw"
	"cairnmesh/firmware/keystore"
	"cairnmesh/firmware/node"
	"cairnmesh/firmware/proto"
	"cairnmesh/firmware/radio"
)

var (
	baudRate   = "115200" // -ldflags "-X main.baudRate=..."
	benchBuild = ""       // "1": a bench build, joins without read protection (make BENCH=1)
)

// Module event ids (spec 19): the gateway defines what each means; these
// are the demo's.
const (
	eventButton    = 0x01 // the button was pressed while the watch was armed
	eventLowSupply = 0x02 // the supply fell below 3.0 V
)

var (
	uart    = machine.UART2
	led     = machine.PA5
	button  = machine.PC13
	stack   node.Stack
	air     = board.SerialAir{UART: machine.UART2}
	emitter board.BenchEmitter
	netKey  [16]byte
	appKey  [16]byte
)

// ---- this build's modules ----

var (
	ledSwitch = ext.Switch{Out: led, Code: 0x01, Claim: node.Pin(uint8(led))}

	alarm = ext.EmitSignal{Signal: radio.Signal{
		Kind: radio.Carrier, PowerDBm: radio.MaxPower, Duration: 500 * proto.Millisecond}}

	buttonLatch ext.Latch
	buttonWatch = ext.Watch{
		Source:     &buttonLatch,
		Detect:     ext.Nonzero{},
		Trigger:    ext.Triggers{&alarm, &ext.NotifyEvent{ID: eventButton}},
		Period:     1 * proto.Second, // the latch makes it due at once anyway
		Cooldown:   2 * proto.Second,
		Claims:     []node.Resource{node.Pin(uint8(button))},
		Controlled: true, ArmAction: 0x10,
	}

	supply      board.Supply
	supplyWatch = ext.Watch{
		Source:   &supply,
		Detect:   &ext.Threshold{Level: 3000, Hysteresis: 100, Below: true},
		Trigger:  ext.Triggers{&alarm, &ext.NotifyEvent{ID: eventLowSupply}},
		Period:   10 * proto.Second,
		Cooldown: 60 * proto.Second,
		Claims:   []node.Resource{node.ADC},
	}

	bench benchActions
	late  laterDemo
)

func modules() []node.Module {
	return []node.Module{&ledSwitch, &bench, &late, &buttonWatch, &supplyWatch}
}

// benchActions are the serial bench's demo actions (tools/serial_bench.py).
type benchActions struct{}

type slow struct{}

func (slow) Duration(value byte) proto.Time { return proto.Time(value) * 100 * proto.Millisecond }
func (slow) Perform(byte) byte              { return proto.ResultOK }

func (benchActions) Start(h *node.Host) bool {
	return h.Handle(0x02, slow{}) &&
		h.Handle(0x03, node.Func(func(byte) byte { return proto.ResultError })) &&
		h.Handle(0x04, node.Func(func(byte) byte { return proto.ResultOK })) &&
		h.Handle(0x11, node.Func(func(byte) byte {
			extihw.Trigger(uint8(button) % 16)
			return proto.ResultOK
		}))
}

func (benchActions) NextDue() proto.Time { return proto.Never }
func (benchActions) Tick(proto.Time)     {}

// laterDemo is an action whose result is known only later (proto.ResultPending,
// node.Host.Complete): it stands for a motor or a peripheral that may not
// finish. One at a time; a second while one waits fails at once.
type laterDemo struct {
	h      *node.Host
	ticket node.Ticket
	at     proto.Time
}

func (l *laterDemo) Start(h *node.Host) bool {
	l.h, l.at = h, proto.Never
	return h.Handle(0x05, l)
}

func (l *laterDemo) Duration(byte) proto.Time { return 5 * proto.Second }

func (l *laterDemo) Perform(value byte) byte {
	if l.at != proto.Never {
		return proto.ResultError
	}
	l.ticket = l.h.Ticket()
	l.at = l.h.Now() + proto.Time(value)*100*proto.Millisecond
	return proto.ResultPending
}

func (l *laterDemo) NextDue() proto.Time { return l.at }

func (l *laterDemo) Tick(proto.Time) {
	l.at = proto.Never
	l.h.Complete(l.ticket, proto.ResultOK) // false past the deadline: answered ERROR already
}

// ---- the same on every node ----

func main() {
	board.ResetIfStartedByBootloader() // after a DFU download's Go: start from a clean reset
	led.Configure(machine.PinConfig{Mode: machine.PinOutput})
	button.Configure(machine.PinConfig{Mode: machine.PinInput}) // the board has the pull resistor
	baud := atoi(baudRate)
	uart.Configure(machine.UARTConfig{TX: machine.PA2, RX: machine.PA3, BaudRate: baud})
	if cyc.To48MHz() {
		// The runtime set TIM16 and the UART up for 4 MHz (see README).
		machine.TIM16.Device.PSC.Set(48/4 - 1)
		cyc.SetUART2Baud(baud)
	}
	startConsole()

	cipher, ok := board.NewCipher()
	if !ok {
		state = "fault: no AES"
		faultLoop(0) // no working AES: never send anything
	}
	random, _ := board.NewRand()
	store, last := board.NewCounterStore()
	if board.ConfigureADC() != nil {
		state = "fault: no ADC"
		faultLoop(1)
	}
	board.WatchEdges(button, extihw.Toggle, &buttonLatch)
	keyCipher, counter = cipher, store

	var keys keystore.Record
	if !board.LoadKeys(&keys) {
		state = "no keys: off the network until provisioned (keys set, then reboot)"
		consoleLoop()
	}
	id, netKey, appKey = keys.Node, keys.Network, keys.App
	if board.ReadProtection() == 0 && benchBuild != "1" {
		state = "unprotected: RDP level 0, off the network (DEPLOY.md; the bench builds with BENCH=1)"
		consoleLoop()
	}
	cfg := proto.DefaultDeviceConfig(id)
	cfg.Awake = true // the bench has no duty-cycled receiver
	records, marks = board.Journals()
	stack.Init(node.Config{
		Device: cfg, NetworkKey: &netKey, AppKey: &appKey, AppKeyVersion: keys.AppKeyVersion,
		LastFcnt: last, Store: store, Records: records, Marks: marks, Cipher: cipher, Rand: random,
		Air: &air, Emitter: &emitter,
	})
	for _, m := range modules() {
		stack.Add(m)
	}
	if !stack.Start(board.Now()) {
		state = "fault: a module stopped the boot (see below)"
		faultLoop(stack.Fault.Module + 2)
	}
	state = "running"
	for {
		pollConsole()
		stack.Poll(board.Now())
		runtime.Gosched()
	}
}

func atoi(s string) uint32 {
	var v uint32
	for _, c := range s {
		v = v*10 + uint32(c-'0')
	}
	return v
}
