//go:build cairn_wle5 && cairn_testbed

package main

// TESTBED ONLY (make wle5-build CMD=wle5node WLE5_BENCH_ACTIONS=1): retain
// actions 0x02-0x05 for the bench. The tag keeps the module and banner out of
// production images without changing the receive filter (cairn_testfilter).

import "cairnmesh/firmware/testbed/benchactions"

const testbedActionsBanner = "\r\n*** BENCHACTIONS BUILD: actions 0x02-0x05 present, NOT for deployment ***"

var testbedActions benchactions.Actions

func addTestbedModules() {
	stack.Add(&testbedActions)
}
