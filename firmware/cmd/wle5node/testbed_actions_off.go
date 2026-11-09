//go:build cairn_wle5 && !cairn_testbed

package main

// The production build has only the product modules: no testbed action table
// entries and no marker for them.
const testbedActionsBanner = ""

func addTestbedModules() {}
