//go:build cairn_wle5

package main

// The part of the firmware that is the same on every node besides the
// protocol: the debug console on USART2 and its commands, and the fault blink.
// It is cmd/node's system.go without USB (the WLE5 has none): no uid, no dfu.
// The console runs whether or not the protocol started (a node with a fault,
// or without keys, can still be inspected and provisioned).

import (
	"device/arm"
	"machine"

	"cairnmesh/firmware/board/wle5"
	"cairnmesh/firmware/board/wle5/adapt"
	"cairnmesh/firmware/cmd/wle5node/wiring"
	"cairnmesh/firmware/console"
	"cairnmesh/firmware/journal"
	"cairnmesh/firmware/keystore"
	"cairnmesh/firmware/proto"
	"cairnmesh/firmware/stackwater"
)

var (
	con console.Console
	// state is what the node is doing, and id its address, for "status".
	state = "starting"
	id    byte
	// keyCipher and counter serve the key and counter commands.
	keyCipher *adapt.Cipher
	rng       *adapt.Rand
	counter   *adapt.CounterStore
	// records and marks are the journals, for "stats" (nil before the boot reaches them).
	records *journal.Records
	marks   *journal.Marks
)

// startConsole brings the console up on USART2 and registers the commands.
func startConsole() {
	con.Banner = "CairnMesh WLE5 node console. Type help." + testFilterBanner + testbedActionsBanner
	if powerErr == nil {
		con.Banner += "\r\n" + wiring.TxText(txAllowed, txPower, txRequested)
	}
	con.Init(adapt.Serial{S: machine.UART2})
	con.Handle("status", "node ID, state, frame counter, uptime", cmdStatus)
	con.Handle("stats", "protocol, module, radio and flash counters", cmdStats)
	con.Handle("stack", "MSP and goroutine stack watermarks (read-only)", cmdStack)
	con.Handle("diag", "boot self-tests, RNG clock, flash layout (no keys)", adapt.DiagCommand(func(d *adapt.Diag) {
		wle5.Diagnose(d, keyCipher, rng)
	}))
	con.Handle("reboot", "restart the node", cmdReboot)
	con.Handle("keys", "keys | keys set NODE NETKEY APPKEY [KEYVER] | keys erase yes", cmdKeys)
	con.Handle("fcnt", "fcnt | fcnt raise BOUND (only upward, below 4026531840; reboots)", cmdFcnt)
	registerTestFilter(&con) // the receive test filter: only in a build with the cairn_testfilter tag
}

// consoleLoop never returns: only the console runs (the node has no keys, is
// not read-protected, or its radio did not start).
func consoleLoop() {
	for {
		pollConsole()
	}
}

// printKCV shows a key by its check value only: a key is never printed.
func printKCV(c *console.Console, name string, key *[16]byte) {
	c.Print(" ")
	c.Print(name)
	c.Print(" kcv ")
	if keyCipher == nil {
		c.Print("?")
		return
	}
	kcv := keystore.KCV(keyCipher, key)
	keyCipher.Forget() // key may be a temporary: never let the cipher match its address again
	c.PrintHex(kcv[:])
}

func cmdKeys(c *console.Console, args [][]byte) {
	switch {
	case len(args) == 1:
		var r keystore.Record
		if !wle5.LoadKeys(&r) {
			c.Println("no keys stored")
			return
		}
		c.Print("node ")
		c.PrintUint(uint64(r.Node))
		c.Print(" keyver ")
		c.PrintUint(uint64(r.AppKeyVersion))
		printKCV(c, "net", &r.Network)
		printKCV(c, "app", &r.App)
		c.Println("")
		if r.Node != id || state != "running" {
			c.Println("stored keys are used from the next boot")
		}
	case string(args[1]) == "set" && (len(args) == 5 || len(args) == 6):
		var r keystore.Record
		node, ok := console.ParseUint(args[2])
		if !ok || node > 255 || !keystore.ValidNode(byte(node)) {
			c.Println("bad node ID (1..254, not 66)")
			return
		}
		r.Node = byte(node)
		if !console.ParseHex(r.Network[:], args[3]) || !console.ParseHex(r.App[:], args[4]) {
			c.Println("keys are 32 hex digits each")
			return
		}
		if len(args) == 6 {
			v, ok := console.ParseUint(args[5])
			if !ok || v > 255 {
				c.Println("bad key version")
				return
			}
			r.AppKeyVersion = byte(v)
		}
		if !wle5.StoreKeys(&r) {
			c.Println("flash write failed")
			return
		}
		c.Print("stored: node ")
		c.PrintUint(uint64(r.Node))
		printKCV(c, "net", &r.Network)
		printKCV(c, "app", &r.App)
		c.Println("")
		c.Println("reboot to use them")
	case string(args[1]) == "erase" && len(args) == 3 && string(args[2]) == "yes":
		if wle5.EraseKeys() {
			c.Println("keys erased; after a reboot the node stays off the network")
		} else {
			c.Println("flash erase failed")
		}
	default:
		c.Println("usage: keys | keys set NODE NETKEY APPKEY [KEYVER] | keys erase yes")
	}
}

func cmdFcnt(c *console.Console, args [][]byte) {
	if counter == nil {
		c.Println("no counter")
		return
	}
	switch {
	case len(args) == 1:
		if stack.Started() {
			c.Print("fcnt ")
			c.PrintUint(uint64(stack.Sec.Fcnt()))
			c.Print(" ")
		}
		c.Print("stored bound ")
		c.PrintUint(uint64(counter.Bound()))
		c.Println("")
	case len(args) == 3 && string(args[1]) == "raise":
		bound, ok := console.ParseUint(args[2])
		if !ok || bound <= counter.Bound() || bound > adapt.MaxRaise {
			c.Println("the bound only goes up, and stops short of the top of the counter:")
			c.Print("give a number above the stored bound and at most ")
			c.PrintUint(adapt.MaxRaise)
			c.Println("")
			return
		}
		if !counter.Raise(bound) {
			c.Println("flash write failed")
			return
		}
		c.Print("bound raised to ")
		c.PrintUint(uint64(bound))
		c.Println("")
		cmdReboot(c, nil) // the running protocol must not store its old, lower reservation
	default:
		c.Println("usage: fcnt | fcnt raise BOUND")
	}
}

// pollConsole runs the console.
func pollConsole() { con.Poll(wle5.Now()) }

func cmdStatus(c *console.Console, _ [][]byte) {
	c.Print("node ")
	c.PrintUint(uint64(id))
	c.Print(" state ")
	c.Print(state)
	if stack.Started() {
		c.Print(" fcnt ")
		c.PrintUint(uint64(stack.Sec.Fcnt()))
	}
	c.Print(" uptime ")
	c.PrintUint(uint64(wle5.Now() / proto.Second))
	c.Print(" s rdp ")
	c.PrintUint(uint64(wle5.ReadProtection()))
	if benchBuild == "1" {
		c.Print(" bench build")
	}
	if wle5.BootResults.RNGErr != nil {
		c.Print(" rng FAIL (diag)")
	}
	if powerErr != nil {
		c.Print(" tx power refused (WLE5_TX_DBM)")
	} else {
		c.Print(" ")
		c.Print(wiring.TxText(txAllowed, txPower, txRequested))
	}
	if stack.Started() {
		c.Print(" mesh relay hello join neighbours ")
		c.PrintUint(uint64(stack.Watch.Neighbours()))
	}
	printTestFilterStatus(c)
	c.Println("")
	if f := stack.Fault; f.Reason != 0 {
		c.Print("boot fault: module")
		c.PrintField("index", uint32(f.Module))
		c.PrintField("reason", uint32(f.Reason))
		c.PrintField("detail", uint32(f.Detail))
		c.Println("")
	}
}

func cmdStats(c *console.Console, _ [][]byte) {
	s := &stack.Sec.Stats
	c.Print("secure:")
	c.PrintField("sealed", s.Sealed)
	c.PrintField("opened", s.Opened)
	c.PrintField("auth_failed", s.AuthFailed)
	c.PrintField("replayed", s.Replayed)
	c.PrintField("plain_dropped", s.PlainDropped)
	c.PrintField("app_failed", s.AppFailed)
	c.PrintField("app_missing", s.AppMissing)
	c.PrintField("app_no_key", s.AppNoKey)
	c.PrintField("reserve_failed", s.ReserveFailed)
	c.Println("")
	d := &stack.Dev.Stats
	c.Print("device:")
	c.PrintField("accepted", d.Accepted)
	c.PrintField("results_sent", d.ResultsSent)
	c.PrintField("results_acked", d.ResultsAcked)
	c.PrintField("results_abandoned", d.ResultsAbandoned)
	c.PrintField("refused_busy", d.RefusedBusy)
	c.PrintField("deferred_dropped", d.DeferredDropped)
	c.PrintField("ignored", d.Ignored)
	c.PrintField("deadline_missed", d.DeadlineMissed)
	c.PrintField("late_completions", d.LateCompletions)
	c.PrintField("log_failed", d.LogFailed)
	c.Println("")
	n := &stack.Stats
	c.Print("stack:")
	c.PrintField("received", n.Received)
	c.PrintField("rejected", n.Rejected)
	c.PrintField("unsent", n.Unsent)
	c.PrintField("held_by_signal", n.HeldBySignal)
	c.PrintField("signals", n.Signals)
	c.PrintField("signals_refused", n.SignalsRefused)
	c.PrintField("notify_dropped", n.NotifyDropped)
	c.PrintField("unknown_action", n.UnknownAction)
	c.Println("")
	m := &meshNode.Relay.Stats
	c.Print("relay:")
	c.PrintField("originated", m.Originated)
	c.PrintField("repeated", m.Repeated)
	c.PrintField("relayed", m.Relayed)
	c.PrintField("suppressed", m.Suppressed)
	c.PrintField("answered", m.Answered)
	c.PrintField("duplicates", m.Duplicates)
	c.PrintField("not_on_path", m.NotOnPath)
	c.PrintField("overflow", m.Overflow)
	c.PrintField("auth_failed", m.AuthFailed)
	c.PrintField("routed", m.Routed)
	c.PrintField("hop_acked", m.HopAcked)
	c.PrintField("route_errors", m.RouteErrors)
	c.PrintField("probes_relayed", m.ProbesRelayed)
	c.PrintField("probes_answered", m.ProbesAnswered)
	c.Println("")
	w, j := &stack.Watch.Stats, &stack.Disc.Stats
	c.Print("hello:")
	c.PrintField("heard", w.Hellos)
	c.PrintField("lost", w.Lost)
	c.PrintField("recovered", w.Recovered)
	c.PrintField("reports_sent", w.ReportsSent)
	c.PrintField("reports_acked", w.ReportsAcked)
	c.PrintField("neighbours", uint32(stack.Watch.Neighbours()))
	c.Println("")
	c.Print("join:")
	c.PrintField("joins", j.Joins)
	c.PrintField("joined", j.Joined)
	c.PrintField("answers", j.Answers)
	c.PrintField("answers_dropped", j.AnswersDropped)
	c.Println("")
	r := &wle5.Radio.Stats
	c.Print("radio:")
	c.PrintField("received", r.Received)
	c.PrintField("crc_errors", r.CRCErrors)
	c.PrintField("tx_done", r.TxDone)
	c.PrintField("tx_timeouts", r.TxTimeouts)
	c.PrintField("faults", r.Faults)
	c.PrintField("resets", r.Resets)
	c.PrintField("dropped", r.Dropped)
	c.PrintField("gate_refused", air.Refused+gatedEmit.Refused)
	c.PrintField("irq", wle5.RadioInterrupts())
	c.Println("")
	adapt.PrintHW(c, keyCipher, counter, wle5.ECCFaults)
	if records != nil {
		r, m := &records.J.Stats, &marks.J.Stats
		c.Print("journal:")
		c.PrintField("records", r.Appended)
		c.PrintField("records_compactions", r.Compactions)
		c.PrintField("records_failed", r.Failed)
		c.PrintField("marks", m.Appended)
		c.PrintField("marks_compactions", m.Compactions)
		c.PrintField("marks_failed", m.Failed)
		c.PrintField("skipped", r.Skipped+m.Skipped)
		c.Println("")
	}
}

func cmdStack(c *console.Console, args [][]byte) {
	if !stackwater.ParseArgs(args) {
		c.Println("usage: stack")
		return
	}
	printStackUsage(c, "msp", stackwater.SampleSystem())
	printStackUsage(c, "task", stackwater.SampleTask())
}

func printStackUsage(c *console.Console, name string, u stackwater.Usage) {
	c.Print(name)
	if !u.Found {
		if name == "task" {
			c.Println(": canary not found in the last 4 KiB")
		} else {
			c.Println(": unavailable")
		}
		return
	}
	c.Print(": low ")
	printAddress(c, u.Low)
	c.Print(" used ")
	c.PrintUint(uint64(u.Used))
	c.Print(" B free ")
	c.PrintUint(uint64(u.Free))
	c.Print(" B")
	if name == "task" || !u.CanaryOK {
		c.Print(" canary ")
		if u.CanaryOK {
			c.Print("ok")
		} else {
			c.Print("BAD")
		}
	}
	c.Println("")
}

func printAddress(c *console.Console, addr uintptr) {
	var b [4]byte
	v := uint32(addr)
	b[0], b[1], b[2], b[3] = byte(v>>24), byte(v>>16), byte(v>>8), byte(v)
	c.Print("0x")
	c.PrintHex(b[:])
}

// flushStack stores the replay marks still waiting for their interval
// (spec 5: written at a shutdown too).
func flushStack() {
	if stack.Started() {
		stack.Flush(wle5.Now())
	}
}

func cmdReboot(c *console.Console, _ [][]byte) {
	flushStack()
	c.Println("rebooting")
	end := wle5.Now() + 50*proto.Millisecond // let the reply go out
	for wle5.Now() < end {
		pollConsole()
	}
	arm.SystemReset() // wle5.Setup holds the radio in reset first thing (RM0461 6.1.4)
}

// faultLoop never returns: the console stays up and LED 0 blinks n + 1 short
// flashes, a pause, again.
func faultLoop(n int) {
	start := wle5.Now()
	for {
		pollConsole()
		const flash, pause = 300 * proto.Millisecond, 1 * proto.Second
		period := proto.Time(n+1)*flash + pause
		phase := (wle5.Now() - start) % period
		wle5.LED(0, phase < proto.Time(n+1)*flash && phase%flash < flash/2)
	}
}
