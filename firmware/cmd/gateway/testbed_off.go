//go:build !cairn_testbed

package main

// Production builds have no testbed fault-injection code or build variable.
func testbedStart() {}
func testbedPoll()  {}
