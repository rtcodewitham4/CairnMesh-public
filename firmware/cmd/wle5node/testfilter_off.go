//go:build cairn_wle5 && !cairn_testfilter

package main

import "cairnmesh/firmware/console"

// The default and production build: no receive test filter (testfilter_on.go).
// No "rxf" command exists (the console answers "unknown command"), and status
// says so, which is how a bench script tells this build from a test build.

const testFilterBanner = ""

func registerTestFilter(*console.Console) {}

func printTestFilterStatus(c *console.Console) { c.Print(" testfilter none") }
