package main

// The part of the firmware that is the same on every node besides the
// protocol: the USB debug console and its commands, and the fault blink.
// The console runs whether or not the protocol started (a node with a
// fault, or without keys, can still be inspected and provisioned).

import (
	"device/arm"

	"cairnmesh/firmware/board"
	"cairnmesh/firmware/console"
	"cairnmesh/firmware/hw/usbhw"
	"cairnmesh/firmware/journal"
	"cairnmesh/firmware/keystore"
	"cairnmesh/firmware/proto"
	"cairnmesh/firmware/usb"
)

var (
	usbCtl usbhw.Controller
	usbDev usb.Device
	con    console.Console
	uid    [24]byte
	usbOK  bool
	// state is what the node is doing, and id its address, for "status".
	state = "starting"
	id    byte
	// keyCipher and counter serve the key and counter commands.
	keyCipher *board.Cipher
	counter   *board.CounterStore
	// records and marks are the journals, for "stats" (nil before the boot reaches them).
	records *journal.Records
	marks   *journal.Marks
)

// startConsole brings the USB console up and registers the commands.
func startConsole() {
	usbDev.Init(&usbCtl, usbhw.Serial(&uid))
	con.Banner = "CairnMesh node console. Type help."
	con.Init(&usbDev)
	con.Handle("status", "node ID, state, frame counter, uptime", cmdStatus)
	con.Handle("stats", "protocol, module and USB counters", cmdStats)
	con.Handle("uid", "the chip's unique ID (the USB serial number)", cmdUID)
	con.Handle("reboot", "restart the node", cmdReboot)
	con.Handle("keys", "keys | keys set NODE NETKEY APPKEY [KEYVER] | keys erase yes", cmdKeys)
	con.Handle("fcnt", "fcnt | fcnt raise BOUND (only upward; reboots)", cmdFcnt)
	con.Handle("dfu", "dfu yes: leave for the USB DFU bootloader (until a reset)", cmdDFU)
	usbOK = usbCtl.Init()
}

func cmdDFU(c *console.Console, args [][]byte) {
	if len(args) != 2 || string(args[1]) != "yes" {
		c.Println("dfu yes: the node leaves the network and the console for the ST USB DFU bootloader;")
		c.Println("a reset (or the programmer's go command) brings the firmware back")
		return
	}
	switch board.ReadProtection() {
	case 2:
		c.Println("read protection level 2: the bootloader is disabled")
		return
	case 1:
		c.Println("read protection level 1: the bootloader can neither read nor write the flash (DEPLOY.md)")
	}
	flushStack()
	c.Println("entering the DFU bootloader")
	end := board.Now() + 50*proto.Millisecond // let the reply go out
	for board.Now() < end {
		pollConsole()
	}
	board.EnterBootloader()
}

// consoleLoop never returns: only the console runs (the node has no keys).
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
		if !board.LoadKeys(&r) {
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
		if !board.StoreKeys(&r) {
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
		if board.EraseKeys() {
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
		if !ok || bound <= counter.Bound() {
			c.Println("the bound only goes up: give a number above the stored bound")
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

// pollConsole runs the USB controller and the console.
func pollConsole() {
	if usbOK {
		usbCtl.Poll(&usbDev)
		con.Poll(board.Now())
	}
}

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
	c.PrintUint(uint64(board.Now() / proto.Second))
	c.Print(" s rdp ")
	c.PrintUint(uint64(board.ReadProtection()))
	if benchBuild == "1" {
		c.Print(" bench build")
	}
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
		c.PrintField("ecc_faults", board.ECCFaults)
		c.Println("")
	}
	c.Print("usb:")
	c.PrintField("resets", usbCtl.Resets)
	c.PrintField("setups", usbCtl.Setups)
	c.PrintField("outs", usbCtl.Outs)
	c.PrintField("ins", usbCtl.Ins)
	c.PrintField("stalls", usbDev.Stats.Stalls)
	c.PrintField("rx_dropped", usbDev.Stats.RxDropped)
	c.Println("")
}

func cmdUID(c *console.Console, _ [][]byte) {
	c.PrintBytes(uid[:])
	c.Println("")
}

// flushStack stores the replay marks still waiting for their interval
// (spec 5: written at a shutdown too).
func flushStack() {
	if stack.Started() {
		stack.Flush(board.Now())
	}
}

func cmdReboot(c *console.Console, _ [][]byte) {
	flushStack()
	c.Println("rebooting")
	end := board.Now() + 50*proto.Millisecond // let the reply go out
	for board.Now() < end {
		pollConsole()
	}
	arm.SystemReset()
}

// faultLoop never returns: the console stays up and the LED blinks n + 1
// short flashes, a pause, again.
func faultLoop(n int) {
	start := board.Now()
	for {
		pollConsole()
		const flash, pause = 300 * proto.Millisecond, 1 * proto.Second
		period := proto.Time(n+1)*flash + pause
		phase := (board.Now() - start) % period
		led.Set(phase < proto.Time(n+1)*flash && phase%flash < flash/2)
	}
}
