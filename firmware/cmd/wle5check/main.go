//go:build cairn_wle5

// Command wle5check is the radio power-on check of a WLE5 module (E77-400M22S
// or PCB4) on the bench, the counterpart of cmd/radiocheck for the U083 +
// SX1262 gateway board. It never transmits: no PA, no SetTx, no carrier. The
// RF switch is in the idle position (E77: both low; PCB4: CTRL low, which is
// the receive path) until the driver's SetRx puts it in receive (E77: RXEN =
// PA7 high, TXEN = PA6 low; PCB4: CTRL = PA6 low).
//
// Console: USART2, PA2 = TX, PA3 = RX, 115200 8N1 (as cmd/uarthello). Every
// 3 s a pass runs and prints, CR LF ended (format and rules: checktext):
//
//	wle5check start cycle=<n> cpu_hz=<hz>
//	wle5check busy   PASS|FAIL  BUSY fell after the radio reset (RFRST), in time
//	wle5check status PASS|FAIL  Get_Status after the reset (twice, status= and first=): bit 0
//	                            clear and mode 2, standby RC 13 MHz; the command status
//	                            (cmd=, first_cmd=) is only printed there. After a good
//	                            Init, Get_Status (post=, post_cmd=) must be clean in
//	                            receive. Why: the command status keeps the radio's own
//	                            boot-time calibration failure (DS_SX1261_2 9.2.1: with a
//	                            TCXO the power-up calibration fails; 13.3.6: XOSC_START_ERR
//	                            is expected at POR) and Get_Status does not update it:
//	                            bench 2026-11-06, first pass after NRST reads 0xaa (cmd=5)
//	                            twice, no command of ours before it; it clears with Init's
//	                            TCXO / calibration commands (0xa2 from the second pass on)
//	wle5check tcxo   PASS|FAIL  Init: Set_TcxoMode 1800 mV (DIO3), then Radio.AfterTCXO
//	                            (HSEBYPPWR, HSEON, wait HSERDY: hse=ready|timeout|stuck),
//	                            calibration, receive; the radio in receive (needs
//	                            HSE32) and no clock or PLL error; cr = RCC_CR
//	wle5check init   FAIL       only when Init failed: step=<where> sr2=0x<PWR_SR2> cr=0x<RCC_CR>
//	wle5check sync   PASS|FAIL  LoRa sync word reads 0x1424 after the reset and after
//	                            Init, then 2000 more reads agree (SUBGHZSPI integrity)
//	wle5check errors PASS|FAIL  Get_Error is empty
//	wle5check calib  PASS|FAIL  the five calibrations (RC64K, RC13M, PLL, ADC, image)
//	                            report no error: Get_Error is the only read-out of them
//	wle5check rssi   PASS|FAIL  Get_RssiInst in receive is a plausible noise floor
//	wle5check PASS|FAIL pass=<n> fail=<n>
//
// LEDs (E77 base board only): PB5 toggles per pass; PB4 on when the last pass
// was clean; PB3 (the TX LED) stays off.
//
// TCXO voltage: WLE5_TCXO_MV at build time, else 1800 mV, which is used for
// the first power-on; the tcxo line says which one was used.
//
// Build: make wle5-build CMD=wle5check [WLE5_TCXO_MV=<mV>] [WLE5_BOARD=pcb4]
// One shot on the bench: make wle5-flash-check CMD=wle5check
package main

import (
	"machine"
	"time"

	"cairnmesh/firmware/board/wle5"
	"cairnmesh/firmware/cmd/wle5check/checktext"
	"cairnmesh/firmware/hw/subghz"
	"cairnmesh/firmware/radio/sx126x"
)

// defaultTCXOmV is used when the build gives no WLE5_TCXO_MV:
// 1.8 V is in the range of every TCXO the module may carry.
const defaultTCXOmV = 1800

const (
	passEvery  = 3 * time.Second
	busyLimit  = 100 * time.Millisecond
	busReads   = 2000
	settleTime = 20 * time.Millisecond // receive settles before RSSI is read
)

// SX126x / WL commands and registers (RM0461 4.8, 4.9).
const (
	opGetStatus     = 0xC0
	opGetDevErrors  = 0x17
	opGetRssiInst   = 0x15
	opReadRegister  = 0x1D
	regSyncWordHigh = 0x0740
)

var rawW, rawR [8]byte

// raw runs one command of n bytes from rawW with NSS low, the answer in rawR.
// It uses the driver's bus and pins, so it can run before and after Init.
func raw(n int) bool {
	p := &wle5.Radio.Pins
	ready := false
	for i := 0; i < 200000; i++ {
		if !p.Busy.Get() {
			ready = true
			break
		}
	}
	if !ready {
		return false
	}
	p.NSS.Set(false)
	err := wle5.Radio.Bus.Tx(rawW[:n], rawR[:n])
	p.NSS.Set(true)
	// BUSY rises up to 600 ns after NSS: look at it a few times before the next command.
	for i := 0; i < 20; i++ {
		p.Busy.Get()
	}
	return err == nil
}

func command(op byte, n int) bool {
	rawW = [8]byte{op}
	return raw(n)
}

func status() (byte, bool) {
	ok := command(opGetStatus, 2)
	return rawR[1], ok
}

func deviceErrors() (uint16, bool) {
	ok := command(opGetDevErrors, 4)
	return uint16(rawR[2])<<8 | uint16(rawR[3]), ok
}

func rssiRaw() (byte, bool) {
	ok := command(opGetRssiInst, 3)
	return rawR[2], ok
}

func syncWord() (hi, lo byte, ok bool) {
	rawW = [8]byte{opReadRegister, regSyncWordHigh >> 8, regSyncWordHigh & 0xFF}
	ok = raw(6)
	return rawR[4], rawR[5], ok
}

func out(b []byte) { machine.Serial.Write(b) }

// pass runs the whole check once and prints it; it returns true when clean.
func pass(w *checktext.Writer, cycle uint32) bool {
	w.NewPass()
	out(w.Start(cycle, uint32(machine.CPUFrequency())))

	// Before Init: reset the radio and watch BUSY, ask for the status, read the
	// sync word register, which must still hold its reset value.
	p := &wle5.Radio.Pins
	p.NSS.Set(true)
	p.Reset.Set(false)
	time.Sleep(time.Millisecond)
	start := time.Now()
	p.Reset.Set(true)
	busy0 := p.Busy.Get()
	fell := false
	for time.Since(start) < busyLimit {
		if !p.Busy.Get() {
			fell = true
			break
		}
	}
	lowUs := time.Since(start).Microseconds()
	// RFBUSYMS stays high on this chip (bench 2026-11-06); Busy needs RFBUSYS too.
	msHigh := subghz.RFBusyMS()
	resetTimedOut := subghz.ResetTimedOut
	stFirst, firstOK := status()
	st0, stOK := status()
	stOK = stOK && firstOK && checktext.ModeOK(stFirst, checktext.ModeStandbyRC) &&
		checktext.ModeOK(st0, checktext.ModeStandbyRC)
	rHi, rLo, rdOK := syncWord()
	rdOK = rdOK && checktext.SyncOK(rHi, rLo)

	// Init: the driver's own sequence, the node firmware's way in.
	mv, linked := wle5.TCXOMillivolts()
	src := "link"
	if !linked {
		mv, src = defaultTCXOmV, "default"
	}
	err := wle5.InitRadio(sx126x.Config{
		FrequencyHz:     433_500_000,
		SpreadingFactor: 7,
		BandwidthHz:     125_000,
		CodingRate:      5,
		PowerDBm:        10, // set in the chip, never used: nothing is sent
		TCXOMillivolts:  mv,
	})
	time.Sleep(settleTime)

	// After Init: everything below reads the chip in receive.
	st1, st1OK := status()
	rssi, rssiOK := rssiRaw()
	errs, errsOK := deviceErrors()
	iHi, iLo, iOK := syncWord()
	bad := -1 // not run: after a failed Init every read would wait out BUSY
	if err == nil {
		bad = wle5.Radio.CheckBus(busReads)
	}
	stats := wle5.Radio.Stats
	sr2, cr := wle5.DebugRegs()

	w.Begin("busy", checktext.BusyOK(lowUs, fell, resetTimedOut))
	w.Dec("low_us", lowUs)
	w.Bool("first_high", busy0)
	w.Bool("rfrst_timeout", resetTimedOut)
	w.Bool("ms_high", msHigh)
	out(w.End())

	// After a good Init the command status must be clean. A failed Init is
	// reported by the tcxo and init lines, not twice.
	postOK := err != nil || (st1OK && checktext.StatusOK(st1, checktext.ModeRx))
	w.Begin("status", stOK && postOK)
	w.Hex("status", uint32(st0), 2)
	w.Dec("mode", int64(checktext.StatusMode(st0)))
	w.Dec("cmd", int64(checktext.StatusCmd(st0)))
	w.Hex("first", uint32(stFirst), 2)
	w.Dec("first_cmd", int64(checktext.StatusCmd(stFirst)))
	w.Hex("post", uint32(st1), 2)
	w.Dec("post_cmd", int64(checktext.StatusCmd(st1)))
	out(w.End())

	w.Begin("tcxo", err == nil && st1OK && errsOK && checktext.TCXOOK(true, st1, errs))
	w.Dec("mv", int64(mv))
	w.Str("src", src)
	w.Bool("init", err == nil)
	w.Hex("status", uint32(st1), 2)
	w.Dec("mode", int64(checktext.StatusMode(st1)))
	w.Dec("faults", int64(stats.Faults))
	w.Dec("retries", int64(stats.BusRetries))
	w.Str("hse", wle5.HSE.String())
	w.Hex("cr", cr, 8)
	out(w.End())

	if err != nil {
		step := wle5.Radio.FailStep
		if step == "" {
			step = "setup" // the driver was not reached (no TCXO voltage)
		}
		w.Begin("init", false)
		w.Str("step", step)
		w.Hex("sr2", sr2, 8)
		w.Hex("cr", cr, 8)
		out(w.End())
	}

	w.Begin("sync", rdOK && iOK && checktext.SyncOK(iHi, iLo) && bad == 0)
	w.Hex("reset", uint32(rHi)<<8|uint32(rLo), 4)
	w.Hex("init", uint32(iHi)<<8|uint32(iLo), 4)
	w.Dec("reads", busReads)
	w.Dec("bad", int64(bad))
	out(w.End())

	w.Begin("errors", errsOK && checktext.ErrorsOK(errs))
	w.Hex("raw", uint32(errs), 4)
	w.Errors("e", errs)
	out(w.End())

	w.Begin("calib", errsOK && checktext.CalibOK(errs))
	w.Hex("bits", uint32(errs&checktext.ErrCalibMask), 2)
	out(w.End())

	w.Begin("rssi", rssiOK && st1OK && checktext.RSSIOK(rssi, st1))
	w.Half("dbm", rssi)
	w.Dec("raw", int64(rssi))
	out(w.End())

	out(w.Summary())
	return w.Fail == 0
}

func main() {
	time.Sleep(200 * time.Millisecond)
	wle5.Setup()
	var w checktext.Writer
	led := false
	for cycle := uint32(1); ; cycle++ {
		led = !led
		wle5.LED(0, led)
		wle5.LED(1, pass(&w, cycle))
		wle5.LED(2, false) // TX LED: this program never transmits
		time.Sleep(passEvery)
	}
}
