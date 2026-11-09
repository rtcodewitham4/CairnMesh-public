//go:build cairn_testbed

package main

// TESTBED ONLY (make GATEWAY_BENCH_FAULT=radio1|hang): inject one fault five
// seconds after boot. "radio1" pulses radio 1's reset (its setup is lost, as
// after a brownout); "hang" stops the loop and lets the watchdog reset the
// board. The build tag keeps all of this out of production images.

import (
	"time"

	"cairnmesh/firmware/board"
	"cairnmesh/firmware/proto"
)

// gatewayBenchFault is linked in by the Makefile only for cairn_testbed builds.
var gatewayBenchFault string

var (
	testbedFaultAt    proto.Time
	testbedFaultArmed bool
)

func testbedStart() {
	testbedFaultArmed = gatewayBenchFault != ""
	testbedFaultAt = board.Now() + 5*proto.Second
}

func testbedPoll() {
	if !testbedFaultArmed || board.Now() < testbedFaultAt {
		return
	}
	testbedFaultArmed = false
	switch gatewayBenchFault {
	case "radio1":
		board.Radios[1].Pins.Reset.Set(false)
		time.Sleep(time.Millisecond) // NRESET low for at least 100 µs
		board.Radios[1].Pins.Reset.Set(true)
	case "hang":
		for {
		}
	}
}
