//go:build cairn_wle5 && cairn_testfilter

package main

import (
	"cairnmesh/firmware/console"
	"cairnmesh/firmware/rxfilter"
)

// TEST BUILD (make wle5-build CMD=wle5node WLE5_TESTFILTER=1): the receive
// filter of package rxfilter, set over this console only. Never for a
// full-network test or a deployment: the banner and status say TESTFILTER, and
// the image is named wle5-wle5node-testfilter.

const testFilterBanner = "\r\n*** TESTFILTER BUILD: receive filter present (rxf), NOT for full-network tests ***"

func registerTestFilter(c *console.Console) {
	c.Handle("rxf", "TESTFILTER rx filter: rxf | caps | all | topo | asym-ok | asym | log", func(c *console.Console, args [][]byte) {
		rxfilter.Command(c, args, id, stack.Started(), stack.RxFilter())
	})
}

func printTestFilterStatus(c *console.Console) {
	c.Print(" TESTFILTER ON ")
	c.Print(stack.RxFilter().ModeName())
}
