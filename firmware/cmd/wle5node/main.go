//go:build cairn_wle5

// Command wle5node is the node firmware on an STM32WLE5 module (E77-400M22S for
// verification, PCB4 later): the common node (node.Stack, the protocol, spec
// section 17 as a multi-hop node) on the
// chip's own radio, with the node's peripherals on the WLE5: hardware AES, the
// RNG, the flash journals, the key page and the FCNT counter pages,
// and the debug console on USART2.
//
// Nothing here names a pin. The RF switch and the LEDs are the board layer's
// (board/wle5: rf_e77.go or rf_pcb4.go, WLE5_BOARD), the console pins are in
// TinyGo's cairn-wle5 board file. The air parameters are those of cmd/wle5link
// (433.5 MHz, SF7, 125 kHz, CR 4/5, preamble 8, sync 0x1424); no wire field is
// added or changed.
//
// Mesh (wiring.Mesh, tested on the host in cmd/wle5node/wiring): the node
// relays other nodes' frames (flooding with backoff, spec 7), answers along the
// route back, is a route endpoint for probes and sessions (spec 17), sends
// HELLOs and keeps the neighbour table (spec 8), and JOINs / answers scans
// (spec 12.3). All of it is on the air only if the node may transmit (below).
// With the layers on, an idle node transmits by itself: a HELLO every 60 s and
// a JOIN after boot (retried with backoff until the controller answers), so a
// bench that counts bursts must expect them. Not in this build yet: the wake
// layer (the SX126x driver has no duty-cycled receive, node.Sampler), listen
// before talk (the driver has no mesh.Busy), low power, the ADC and button
// modules of cmd/node.
//
// Transmit safety, unchanged from the radio test programs: the power ceiling
// is the board layer's (safety.MaxPowerDBm, 10 dBm, on Radio.PowerCap; more
// only with the tag wle5_allow_high_power), and the node transmits only if the
// build says an antenna or a 50 ohm load is on the port: WLE5_ANT_OK=1
// (safety.AntennaConfirmed). Without it the node listens and runs the
// protocol, but every packet and signal is refused.
//
// Power: WLE5_TX_DBM (linked in as main.txDBM), default 0 dBm (it was a fixed
// 10 dBm before; the E77 link tests run at 0 dBm or less). A text that is not a
// whole number, or outside the chip's -9..22 dBm, stops the boot (faultLoop(1):
// 2 flashes, the same pattern as "radio did not start"; only status tells them
// apart; the radio is never started). Between 10 and 22 the board cap lowers
// it to 10 unless the build has the tag wle5_allow_high_power; banner and
// status show the power in effect.
//
// The node's address and keys are not in the image: they are
// provisioned over the console ("keys set", tools/provision.py on the serial
// port) into the key page. Without them the node stays off the network. A chip
// without read protection (RDP level 0) does not join either; the bench builds
// with BENCH=1 (-X main.benchBuild=1), the default build is the deployment one.
//
// Actions of the production build:
//
//	0x01  indicator LED 0 (board layer): VALUE 0 off, else on     ext.Switch
//
// WLE5_BENCH_ACTIONS=1 adds the testbed actions 0x02-0x05 below. They are in a
// separate cairn_testbed file and are not part of a production image:
//
//	0x02  slow action of VALUE x 0.1 s (past 0.6 s: ACCEPTED, then RESULT)
//	0x03  fails (ERROR)
//	0x04  does nothing, at once
//	0x05  result known later: completes OK after VALUE x 0.1 s, deadline 5 s
//
// A failed boot blinks LED 0, short flashes then a pause, forever: 1 flash no
// AES, 2 radio did not start, index + 3 the module that stopped the boot.
//
// Build: make wle5-build CMD=wle5node [WLE5_ANT_OK=1] [WLE5_TX_DBM=<dBm, -9..10, default 0>] [WLE5_TCXO_MV=<mV>] [BENCH=1] [WLE5_BOARD=pcb4] [WLE5_BENCH_ACTIONS=1]
package main

import (
	"runtime"
	"time"

	"cairnmesh/firmware/board/wle5"
	"cairnmesh/firmware/board/wle5/adapt"
	"cairnmesh/firmware/board/wle5/safety"
	"cairnmesh/firmware/cmd/wle5node/wiring"
	"cairnmesh/firmware/ext"
	"cairnmesh/firmware/keystore"
	"cairnmesh/firmware/node"
	"cairnmesh/firmware/proto"
	"cairnmesh/firmware/radio/sx126x"
)

var (
	benchBuild = ""   // "1": a bench build, joins without read protection (make BENCH=1)
	antOK      string // "1": an antenna or a 50 ohm load is connected (WLE5_ANT_OK=1)
	txDBM      string // transmit power in dBm (WLE5_TX_DBM=); empty: wiring.DefaultPowerDBm
)

const (
	freqHz        = 433_500_000
	defaultTCXOmV = 1800 // as wle5check / wle5link when the build gives no WLE5_TCXO_MV
	checkEvery    = 5 * proto.Second
)

var (
	stack     node.Stack
	air       adapt.GatedAir
	gatedEmit adapt.GatedEmitter
	txAllowed bool
	meshNode  wiring.Mesh // the relay and the layers above it (large: a global)
	// txRequested is WLE5_TX_DBM, txPower what the board's cap leaves of it;
	// powerErr is set when the text was refused (the boot stops).
	txRequested, txPower int8
	powerErr             error
	netKey               [16]byte
	appKey               [16]byte
	products             = wiring.Products{LED: ext.Switch{Out: led{}, Code: wiring.ProductLEDAction, Claim: node.User}}
	nextCheck            proto.Time
	checkFails           uint32
)

// led is the board layer's LED 0 as an ext.Output.
type led struct{}

func (led) Set(on bool) { wle5.LED(0, on) }

func main() {
	time.Sleep(200 * time.Millisecond)
	wle5.Setup() // RF switch idle, radio held in reset until InitRadio (RM0461 6.1.4)
	// The interlock is known from the first line, so status is right in every
	// state (no keys, RDP0, a fault), not only when the node runs.
	txAllowed = safety.AntennaConfirmed(antOK)
	txRequested, powerErr = wiring.ParsePower(txDBM)
	txPower = wle5.EffectivePower(txRequested)
	startConsole()
	if powerErr != nil {
		state = "fault: WLE5_TX_DBM \"" + txDBM + "\": " + powerErr.Error()
		faultLoop(1) // the radio is never started
	}

	cipher, ok := wle5.NewCipher()
	wle5.BootResults.AESRan, wle5.BootResults.AESOK = true, ok
	keyCipher = cipher
	if !ok {
		state = "fault: no AES"
		faultLoop(0) // no working AES: never send anything
	}
	random, rngErr := wle5.NewRand()
	wle5.BootResults.RNGRan, wle5.BootResults.RNGErr = true, rngErr
	rng = random
	// A failed RNG start does not stop the node: the RNG only draws the jitter of
	// retransmissions and relays (proto.Rand; keys and nonces never come from it:
	// keys are provisioned, nonces are the FCNT), and Rand falls back to the
	// midpoint and counts it. cmd/node does the same (it discards the error); here
	// the failure is shown in status and diag instead of staying silent.
	store, last := wle5.NewCounterStore()
	counter = store

	var keys keystore.Record
	if !wle5.LoadKeys(&keys) {
		state = "no keys: off the network until provisioned (keys set, then reboot)"
		consoleLoop()
	}
	id, netKey, appKey = keys.Node, keys.Network, keys.App
	if wle5.ReadProtection() == 0 && benchBuild != "1" {
		state = "unprotected: RDP level 0, off the network (DEPLOY.md; the bench builds with BENCH=1)"
		consoleLoop()
	}

	mv, linked := wle5.TCXOMillivolts()
	if !linked {
		mv = defaultTCXOmV
	}
	if err := wle5.InitRadio(sx126x.Config{
		FrequencyHz: freqHz, SpreadingFactor: 7, BandwidthHz: 125_000, CodingRate: 5,
		PowerDBm: txRequested, TCXOMillivolts: mv,
	}); err != nil {
		state = "fault: radio: " + err.Error()
		faultLoop(1)
	}
	wle5.EnableRadioInterrupt()

	cfg := proto.DefaultDeviceConfig(id)
	cfg.Awake = true // no duty-cycled receiver yet
	records, marks = wle5.Journals()
	nodeConfig := node.Config{
		Device: cfg, NetworkKey: &netKey, AppKey: &appKey, AppKeyVersion: keys.AppKeyVersion,
		LastFcnt: last, Store: store, Records: records, Marks: marks, Cipher: cipher, Rand: random,
	}
	// The transmit interlock (without WLE5_ANT_OK=1 the node only listens) and
	// the multi-hop layers (relay, HELLO, JOIN, route endpoint) are put on in
	// wiring.Build, which the host tests run as it is.
	stack.Init(wiring.Build(nodeConfig, &wle5.Radio, &wle5.Radio, txAllowed, &air, &gatedEmit, &meshNode))
	stack.Add(products.Module())
	addTestbedModules()
	if !stack.Start(wle5.Now()) {
		state = "fault: a module stopped the boot (see below)"
		faultLoop(stack.Fault.Module + 2)
	}
	state = "running"
	nextCheck = wle5.Now() + checkEvery
	for {
		pollConsole()
		now := wle5.Now()
		stack.Poll(now)
		if now >= nextCheck {
			nextCheck = now + checkEvery
			if air.Quiet() && !wle5.Radio.Check() {
				checkFails++
				wle5.Radio.Recover() // sets the radio up again as it was; its interrupt stays on
			}
		}
		runtime.Gosched()
	}
}
