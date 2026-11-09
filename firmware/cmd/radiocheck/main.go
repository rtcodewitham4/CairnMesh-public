// Command radiocheck is the power-on check of the two SX1262 modules on the
// NUCLEO-U083RC bench (step 1). It never transmits.
//
// Both chips share SPI1, so both NSS lines are driven high from the start.
// The check then
//   - probes the bus: with both NSS high MISO must be released (it follows
//     the MCU's pull resistor), with one NSS low only that chip drives it;
//   - for each radio: resets it, waits for BUSY, reads the status and the
//     device errors, reads the LoRa sync word (reset value 0x1424), writes
//     and reads it back, and reads it 2000 times;
//   - writes a different marker into each chip and reads both back, so a
//     chip answering for the other one shows;
//   - then reads both markers forever and reports each second.
//
// Output on the ST-LINK serial port (USART2, 115200):
//
//	R start
//	N nss0=<level> nss1=<level> pullup=<level> pulldown=<level> -> released|driven ok=<bool>
//	B<r> busy_low_us=<us> | B<r> FAIL ...
//	S<r> status=<byte> mode=<n> ok=<bool>
//	E<r> errors=<n> ok=<bool>
//	G<r> sync=<byte> <byte> ok=<bool>
//	W<r> read <byte> <byte> ok=<bool>
//	L<r> reads=<n> bad=<n>
//	X marker0=<byte> <byte> marker1=<byte> <byte> ok=<bool>
//	R done pass=<n> fail=<n>
//	M<r> good=<n> bad=<n> busy=<n>
package main

import (
	"machine"
	"time"

	"cairnmesh/firmware/hw/spihw"
)

// radio is one module's control pins.
type radio struct {
	nss, reset, busy, dio1 machine.Pin
}

var radios = [2]radio{
	{nss: machine.PB10, reset: machine.PB11, busy: machine.PB13, dio1: machine.PB12},
	{nss: machine.PB14, reset: machine.PB15, busy: machine.PB2, dio1: machine.PA8},
}

// SX126x commands (DS_SX1261/2, chapter 13).
const (
	opGetStatus    = 0xC0
	opReadRegister = 0x1D
	opWriteReg     = 0x0D
	opGetErrors    = 0x17
	regSyncWord    = 0x0740
)

var bus = spihw.SPI1

func (r *radio) waitBusy() bool {
	for i := 0; i < 100000; i++ {
		if !r.busy.Get() {
			return true
		}
	}
	return false
}

// cmd runs one command with NSS low; rd, if set, receives as many bytes as w sends.
func (r *radio) cmd(w, rd []byte) bool {
	if !r.waitBusy() {
		return false
	}
	r.nss.Low()
	err := bus.Tx(w, rd)
	r.nss.High()
	// BUSY rises up to 600 ns after NSS (DS_SX1261/2, 8.3.1): wait well
	// past that, or the next waitBusy sees the old low level.
	for i := 0; i < 20; i++ {
		r.busy.Get()
	}
	return err == nil
}

func (r *radio) readReg(addr uint16, out []byte) bool {
	var w, rd [8]byte
	w[0], w[1], w[2] = opReadRegister, byte(addr>>8), byte(addr)
	n := 4 + len(out) // opcode, address, status, data
	if !r.cmd(w[:n], rd[:n]) {
		return false
	}
	copy(out, rd[4:n])
	return true
}

func (r *radio) writeReg(addr uint16, data []byte) bool {
	var w [8]byte
	w[0], w[1], w[2] = opWriteReg, byte(addr>>8), byte(addr)
	n := 3 + copy(w[3:], data)
	return r.cmd(w[:n], nil)
}

// pullLevels reads MISO with the pull-up and then the pull-down.
func pullLevels() (up, down bool) {
	miso := machine.PA6
	miso.Configure(machine.PinConfig{Mode: machine.PinInputPullup})
	time.Sleep(time.Millisecond)
	up = miso.Get()
	miso.Configure(machine.PinConfig{Mode: machine.PinInputPulldown})
	time.Sleep(time.Millisecond)
	down = miso.Get()
	return up, down
}

// busProbe checks that MISO is released with both NSS high and driven with
// one low. It runs before SPI1 takes PA6, since it makes PA6 a plain input.
func busProbe() bool {
	all := true
	for _, sel := range []int{-1, 0, 1, -1} {
		for i := range radios {
			radios[i].nss.Set(i != sel)
		}
		time.Sleep(time.Millisecond)
		up, down := pullLevels()
		released := up && !down
		ok := released == (sel < 0)
		verdict := "driven"
		if released {
			verdict = "released"
		}
		println("N nss0=", sel != 0, "nss1=", sel != 1, "pullup=", up, "pulldown=", down, "->", verdict, "ok=", ok)
		all = all && ok
	}
	for i := range radios {
		radios[i].nss.High()
	}
	return all
}

// check runs the single-chip checks on radio n; it returns passes and fails.
func check(n int) (pass, fail int) {
	r := &radios[n]
	tally := func(ok bool) {
		if ok {
			pass++
		} else {
			fail++
		}
	}

	r.reset.Low()
	time.Sleep(time.Millisecond)
	r.reset.High()
	start := time.Now()
	busyLow := false
	for time.Since(start) < 100*time.Millisecond {
		if !r.busy.Get() {
			busyLow = true
			break
		}
	}
	if busyLow {
		println("B", n, "busy_low_us=", time.Since(start).Microseconds())
	} else {
		println("B", n, "FAIL busy stays high 100 ms after reset")
	}
	tally(busyLow)

	var rd [4]byte
	ok := r.cmd([]byte{opGetStatus, 0}, rd[:2])
	st := rd[1]
	mode := (st >> 4) & 7
	// After reset the chip is in STDBY_RC (mode 2); 0xFF means MISO floats high.
	ok = ok && mode == 2 && st != 0xFF
	println("S", n, "status=", st, "mode=", mode, "ok=", ok)
	tally(ok)

	ok = r.cmd([]byte{opGetErrors, 0, 0, 0}, rd[:4])
	errs := uint16(rd[2])<<8 | uint16(rd[3])
	ok = ok && errs == 0
	println("E", n, "errors=", errs, "ok=", ok)
	tally(ok)

	var sync [2]byte
	ok = r.readReg(regSyncWord, sync[:]) && sync[0] == 0x14 && sync[1] == 0x24
	println("G", n, "sync=", sync[0], sync[1], "(want 20 36) ok=", ok)
	tally(ok)

	var back [2]byte
	ok = r.writeReg(regSyncWord, []byte{0x34, 0x44}) && r.readReg(regSyncWord, back[:]) &&
		back[0] == 0x34 && back[1] == 0x44
	println("W", n, "read", back[0], back[1], "(want 52 68) ok=", ok)
	r.writeReg(regSyncWord, []byte{0x14, 0x24})
	tally(ok)

	bad := 0
	const reads = 2000
	for i := 0; i < reads; i++ {
		if !r.readReg(regSyncWord, sync[:]) || sync[0] != 0x14 || sync[1] != 0x24 {
			bad++
		}
	}
	println("L", n, "reads=", reads, "bad=", bad)
	tally(bad == 0)
	return pass, fail
}

// markers are written one into each chip: each must read back its own.
var markers = [2][2]byte{{0x34, 0x44}, {0x56, 0x66}}

func crossCheck() bool {
	for i := range radios {
		radios[i].writeReg(regSyncWord, markers[i][:])
		var g [2]byte
		radios[0].readReg(regSyncWord, g[:])
		println("X after writing radio", i, "radio 0 reads", g[0], g[1], "busy0=", radios[0].busy.Get(), "busy1=", radios[1].busy.Get())
	}
	var got [2][2]byte
	ok := true
	for i := range radios {
		ok = radios[i].readReg(regSyncWord, got[i][:]) && got[i] == markers[i] && ok
	}
	println("X marker0=", got[0][0], got[0][1], "marker1=", got[1][0], got[1][1],
		"(want 52 68, 86 102) ok=", ok)
	return ok
}

// watch reads both markers in turn forever and reports each second.
func watch() {
	var got [2]byte
	for {
		var good, bad, busy [2]int
		var last [2][2]byte
		start := time.Now()
		for time.Since(start) < time.Second {
			for i := range radios {
				switch {
				case !radios[i].readReg(regSyncWord, got[:]):
					busy[i]++
				case got == markers[i]:
					good[i]++
				default:
					bad[i]++
					last[i] = got
				}
			}
		}
		for i := range radios {
			println("M", i, "good=", good[i], "bad=", bad[i], "busy=", busy[i], "last_bad=", last[i][0], last[i][1])
		}
	}
}

// pinProbe reads each control pin as an input with the pull-up and then
// the pull-down, driving nothing: a chip output (BUSY, DIO1) holds its
// level, an input left alone (NSS) follows the pull. NRESET has a pull-up
// inside the chip. It tells which wire went to which module pin.
//
//	Q<r> <name> pullup=<level> pulldown=<level> -> held|follows
func pinProbe() {
	names := [4]string{"nss", "reset", "busy", "dio1"}
	for i := range radios {
		r := &radios[i]
		for k, p := range [4]machine.Pin{r.nss, r.reset, r.busy, r.dio1} {
			p.Configure(machine.PinConfig{Mode: machine.PinInputPullup})
			time.Sleep(time.Millisecond)
			up := p.Get()
			p.Configure(machine.PinConfig{Mode: machine.PinInputPulldown})
			time.Sleep(time.Millisecond)
			down := p.Get()
			p.Configure(machine.PinConfig{Mode: machine.PinInput})
			verdict := "held"
			if up && !down {
				verdict = "follows"
			}
			println("Q", i, names[k], "pullup=", up, "pulldown=", down, "->", verdict)
		}
	}
}

// calibrateProbe sends Calibrate(all) and times BUSY high, which needs no
// MISO: a chip that got the command over SCK, MOSI and NSS stays busy for
// a few milliseconds.
//
//	C<r> busy_high_us=<us> (0: the command did not arrive)
func calibrateProbe(n int) {
	r := &radios[n]
	r.reset.Low()
	time.Sleep(time.Millisecond)
	r.reset.High()
	// Which of the two chip outputs goes high while the chip boots: BUSY does.
	var busyHigh, dio1High int
	for start := time.Now(); time.Since(start) < 20*time.Millisecond; {
		if r.busy.Get() {
			busyHigh++
		}
		if r.dio1.Get() {
			dio1High++
		}
	}
	println("C", n, "after reset: high samples busy_pin=", busyHigh, "dio1_pin=", dio1High)
	r.waitBusy()
	r.nss.Low()
	bus.Tx([]byte{0x89, 0x7F}, nil)
	r.nss.High()
	start := time.Now()
	high := false
	for time.Since(start) < 50*time.Millisecond {
		if r.busy.Get() {
			high = true
		} else if high {
			break
		}
	}
	us := int64(0)
	if high {
		us = time.Since(start).Microseconds()
	}
	println("C", n, "busy_high_us=", us)
}

func main() {
	time.Sleep(time.Second) // time for the capture to attach
	println("R start")
	pinProbe()
	for i := range radios {
		r := &radios[i]
		r.nss.Configure(machine.PinConfig{Mode: machine.PinOutput})
		r.nss.High()
		r.reset.Configure(machine.PinConfig{Mode: machine.PinOutput})
		r.reset.High()
		r.busy.Configure(machine.PinConfig{Mode: machine.PinInput})
		r.dio1.Configure(machine.PinConfig{Mode: machine.PinInput})
	}
	pass, fail := 0, 0
	if busProbe() {
		pass++
	} else {
		fail++
	}
	bus.Configure(spihw.Config{SCK: machine.PA5, SDO: machine.PA7, SDI: machine.PA6,
		Frequency: 1000000, Mode: 0})
	for i := range radios {
		calibrateProbe(i)
	}

	for i := range radios {
		p, f := check(i)
		pass += p
		fail += f
	}
	if crossCheck() {
		pass++
	} else {
		fail++
	}
	println("R done pass=", pass, "fail=", fail)
	watch()
}
