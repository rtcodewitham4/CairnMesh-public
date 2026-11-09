//go:build cairn_wle5

// Command wle5probe is the bench diagnosis for the WLE5 radio bring-up that
// cmd/wle5check fails at (Init stops at "calibrate": after Calibrate(0x7F)
// PWR_SR2.RFBUSYMS never falls). It takes Init apart: every step is one
// command, and after each one it prints what the chip and the busy logic did,
// then repeats the sequence with one difference at a time (the matrix below).
// It NEVER transmits: no SetTx, no PA setup, no carrier, no RF switch change.
// TCXO voltage is 1800 mV (Set_TcxoMode code 2, 320 steps = 5 ms) in every
// variant; no other voltage is tried.
//
// Console: USART2 PA2/PA3, 115200 8N1. One round is the whole matrix; rounds
// repeat every 5 s, so a reader that attaches late gets the next one.
//
//	wle5probe round=<n> state=begin
//	wle5probe step v=<variant> s=<step> n=<name> ...      (one per command)
//	wle5probe sum  v=<variant> name=<name> mode=<ms|and|s> ...   (one per variant)
//	wle5probe round=<n> state=end
//
// step fields (all numbers decimal unless 0x):
//
//	op=0x..   first byte sent             pre=ok|timeout  waited for "ready" by this variant's
//	preus=<us>                            busy definition (20 ms limit); the command is sent
//	                                      either way, to see whether the chip answers
//	rx0= rx1= first two bytes back (status bytes: 0xa2 = standby RC, mode in bits 6:4)
//	sh= sl=   RFBUSYS: seen high 0|1, microseconds after NSS rose until it fell (-1: still
//	          high after 30 ms, 0: never seen high)
//	mh= ml=   the same for RFBUSYMS
//	sr2= cr=  PWR_SR2 and RCC_CR after the command
//	+ per step: errs=0x.... (GetDeviceErrors), rd=0x.... (sync word), res=ready|timeout (hse)
//
// sum fields: status (last GetStatus byte, -1 none), sync (-1 none), errs (-1 none),
// cals / calms (Calibrate: microseconds until RFBUSYS / RFBUSYMS fell, -1 stuck, -2 not run),
// hse (notrun|ready|timeout|byponly), ok = 1 when the sync word read 0x1424 and errs is 0.
//
// Matrix (v: name; what differs, always after reset + TCXO command unless stated):
//
//	0 base_ms   Init's order, busy = RFBUSYMS (what failed on the bench)
//	1 base_and  the same, busy = RFBUSYS && RFBUSYMS (ST HAL's rule, recalled not verified)
//	2 base_s    the same, busy = RFBUSYS alone
//	3 nocal     (a) no Calibrate at all, then read the sync word
//	4-8 cal01 cal02 cal04 cal38 cal40   (b) Calibrate with RC64K / RC13M / PLL / ADC / image only
//	9 delay     (c) 50 ms between HSE ready and Calibrate(0x7F)
//	10 notcxo   (d) no TCXO command, no HSE: only reset, status, sync read, write, read back
//	11 notcxo_ms (d) with the old busy definition
//	12 xosc     (e) SetStandby(XOSC) before Calibrate(0x7F)
//	13 byponly  HSEBYPPWR only, no HSEON (what the first version did), then Calibrate(0x7F)
//
// Variants 1 onward use busy = RFBUSYS && RFBUSYMS unless the name says ms or s.
// Bench result 2026-11-06 (12 of 14 ok): RFBUSYMS never fell; RFBUSYS did (6-9 ms
// for Calibrate); subghz.Busy now defaults to the AND rule. Kept for diagnosis.
package main

import (
	"machine"
	"time"

	"cairnmesh/firmware/board/wle5"
	"cairnmesh/firmware/cmd/wle5check/checktext"
	"cairnmesh/firmware/hw/subghz"
)

var (
	w    checktext.Writer
	rawW [8]byte
	rawR [8]byte

	curV    int
	curStep int
)

// result of the running variant, for the sum line.
var res struct {
	status, sync, errs int
	calS, calMS        int64
	hse                string
}

func out(b []byte) { machine.Serial.Write(b) }

func b2i(b bool) int64 {
	if b {
		return 1
	}
	return 0
}

// edge follows one busy signal after a command: seen high, and when it fell.
type edge struct {
	hi bool
	lo int64 // us after NSS rose; -1 still high at the end; 0 never seen high
}

func (e *edge) track(high bool, us int64) {
	if high {
		e.hi, e.lo = true, -1
	} else if e.hi && e.lo < 0 {
		e.lo = us
	}
}

// observe watches RFBUSYS and RFBUSYMS until both are low (and 50 us have
// passed, so a late rise is seen) or limit is over.
func observe(limit time.Duration) (s, ms edge) {
	t0 := time.Now()
	for {
		el := time.Since(t0)
		us := el.Microseconds()
		bs, bm := subghz.RFBusyS(), subghz.RFBusyMS()
		s.track(bs, us)
		ms.track(bm, us)
		if !bs && !bm && us > 50 {
			return
		}
		if el >= limit {
			return
		}
	}
}

// ready waits until the variant's busy definition says not busy.
func ready(limit time.Duration) (ok bool, us int64) {
	t0 := time.Now()
	for time.Since(t0) < limit {
		if !(subghz.Busy{}).Get() {
			return true, time.Since(t0).Microseconds()
		}
	}
	return !(subghz.Busy{}).Get(), time.Since(t0).Microseconds()
}

func common(name string) {
	curStep++
	w.Open("wle5probe step")
	w.Dec("v", int64(curV))
	w.Dec("s", int64(curStep))
	w.Str("n", name)
}

func tail(s, ms edge) {
	w.Dec("sh", b2i(s.hi))
	w.Dec("sl", s.lo)
	w.Dec("mh", b2i(ms.hi))
	w.Dec("ml", ms.lo)
	sr2, cr := wle5.DebugRegs()
	w.Hex("sr2", sr2, 8)
	w.Hex("cr", cr, 8)
}

// step sends data as one command and prints the line; extra adds fields.
func step(name string, extra func(r []byte), data ...byte) []byte {
	n := len(data)
	rawW = [8]byte{}
	copy(rawW[:], data)
	rdy, preUs := ready(20 * time.Millisecond)
	p := &wle5.Radio.Pins
	p.NSS.Set(false)
	err := wle5.Radio.Bus.Tx(rawW[:n], rawR[:n])
	p.NSS.Set(true)
	s, ms := observe(30 * time.Millisecond)
	lastS, lastMS = s.lo, ms.lo
	common(name)
	w.Hex("op", uint32(data[0]), 2)
	if rdy {
		w.Str("pre", "ok")
	} else {
		w.Str("pre", "timeout")
	}
	w.Dec("preus", preUs)
	if err != nil {
		w.Str("spi", "error")
	}
	w.Hex("rx0", uint32(rawR[0]), 2)
	w.Hex("rx1", uint32(rawR[1]), 2)
	tail(s, ms)
	if extra != nil {
		extra(rawR[:n])
	}
	out(w.End())
	return rawR[:n]
}

func resetRadio() {
	wle5.StopHSE()
	p := &wle5.Radio.Pins
	p.NSS.Set(true)
	p.Reset.Set(false)
	time.Sleep(time.Millisecond)
	p.Reset.Set(true)
	s, ms := observe(100 * time.Millisecond)
	common("reset")
	w.Bool("rfrst_timeout", subghz.ResetTimedOut)
	tail(s, ms)
	out(w.End())
	res.hse = "notrun"
}

func status() {
	step("status", func(r []byte) { w.Hex("st", uint32(r[1]), 2); res.status = int(r[1]) }, 0xC0, 0)
}

func errors() {
	step("errors", func(r []byte) {
		e := uint16(r[2])<<8 | uint16(r[3])
		w.Hex("errs", uint32(e), 4)
		w.Errors("e", e)
		res.errs = int(e)
	}, 0x17, 0, 0, 0)
}

func clearErrors() { step("clearerr", nil, 0x07, 0, 0) }

func syncRead(name string) {
	step(name, func(r []byte) {
		v := uint16(r[4])<<8 | uint16(r[5])
		w.Hex("rd", uint32(v), 4)
		res.sync = int(v)
	}, 0x1D, 0x07, 0x40, 0, 0, 0)
}

func syncWriteBack() {
	step("syncwrite", nil, 0x0D, 0x07, 0x40, 0x34, 0x44)
	syncRead("syncread2") // expect 0x3444
	step("syncrestore", nil, 0x0D, 0x07, 0x40, 0x14, 0x24)
	syncRead("syncread3") // expect 0x1424
}

func standby(xosc bool) {
	cfg := byte(0)
	name := "standby_rc"
	if xosc {
		cfg, name = 1, "standby_xosc"
	}
	step(name, nil, 0x80, cfg)
}

// tcxoCmd: Set_TcxoMode 1800 mV (code 2), timeout 320 x 15.625 us = 5 ms.
func tcxoCmd() { step("tcxo_cmd", nil, 0x97, 0x02, 0x00, 0x01, 0x40) }

// hse runs the CPU side of the TCXO start: HSEBYPPWR, HSEON, wait HSERDY.
func hse() {
	t0 := time.Now()
	ok := wle5.StartHSE()
	us := time.Since(t0).Microseconds()
	common("hse")
	w.Str("res", wle5.HSE.String())
	w.Dec("us", us)
	sr2, cr := wle5.DebugRegs()
	w.Hex("sr2", sr2, 8)
	w.Hex("cr", cr, 8)
	out(w.End())
	if ok {
		res.hse = "ready"
	} else {
		res.hse = "timeout"
	}
}

func hseBypassOnly() {
	wle5.EnableTCXOSupply()
	common("hse")
	w.Str("res", "byponly")
	sr2, cr := wle5.DebugRegs()
	w.Hex("sr2", sr2, 8)
	w.Hex("cr", cr, 8)
	out(w.End())
	res.hse = "byponly"
}

func calibrate(param byte) {
	step("calibrate", func(r []byte) {
		w.Hex("param", uint32(param), 2)
	}, 0x89, param)
	// The step line holds the edges; keep them for the sum line.
	res.calS, res.calMS = lastS, lastMS
}

// lastS / lastMS are the fall times of the last step, for the sum line.
var lastS, lastMS int64

func begin() {
	resetRadio()
	status()
	syncRead("syncread0") // reset value, expect 0x1424
}

func tcxoUp() {
	standby(false)
	tcxoCmd()
	hse()
}

type variant struct {
	name string
	mode subghz.BusyMode
	run  func()
}

func modeName(m subghz.BusyMode) string {
	switch m {
	case subghz.BusyAnd:
		return "and"
	case subghz.BusyS:
		return "s"
	}
	return "ms"
}

// fullInit is the order of sx126x.Init up to the end of the calibration.
func fullInit(param byte) {
	begin()
	tcxoUp()
	clearErrors()
	calibrate(param)
	errors()
	status()
	syncRead("syncread1")
}

var variants = []variant{
	{"base_ms", subghz.BusyMS, func() { fullInit(0x7F) }},
	{"base_and", subghz.BusyAnd, func() { fullInit(0x7F) }},
	{"base_s", subghz.BusyS, func() { fullInit(0x7F) }},
	{"nocal", subghz.BusyAnd, func() {
		begin()
		tcxoUp()
		clearErrors()
		errors()
		status()
		syncRead("syncread1")
	}},
	{"cal01", subghz.BusyAnd, func() { fullInit(0x01) }},
	{"cal02", subghz.BusyAnd, func() { fullInit(0x02) }},
	{"cal04", subghz.BusyAnd, func() { fullInit(0x04) }},
	{"cal38", subghz.BusyAnd, func() { fullInit(0x38) }},
	{"cal40", subghz.BusyAnd, func() { fullInit(0x40) }},
	{"delay", subghz.BusyAnd, func() {
		begin()
		tcxoUp()
		time.Sleep(50 * time.Millisecond)
		clearErrors()
		calibrate(0x7F)
		errors()
		status()
		syncRead("syncread1")
	}},
	{"notcxo", subghz.BusyAnd, func() {
		begin()
		syncWriteBack()
		errors()
		status()
	}},
	{"notcxo_ms", subghz.BusyMS, func() {
		begin()
		syncWriteBack()
		errors()
		status()
	}},
	{"xosc", subghz.BusyAnd, func() {
		begin()
		tcxoUp()
		standby(true)
		status()
		errors()
		clearErrors()
		calibrate(0x7F)
		errors()
		status()
		syncRead("syncread1")
	}},
	{"byponly", subghz.BusyAnd, func() {
		begin()
		standby(false)
		tcxoCmd()
		hseBypassOnly()
		clearErrors()
		calibrate(0x7F)
		errors()
		status()
		syncRead("syncread1")
	}},
}

func run(id int, v variant) {
	curV, curStep = id, 0
	res.status, res.sync, res.errs = -1, -1, -1
	res.calS, res.calMS = -2, -2
	lastS, lastMS = -2, -2
	res.hse = "notrun"
	subghz.Mode = v.mode
	v.run()
	w.Open("wle5probe sum")
	w.Dec("v", int64(id))
	w.Str("name", v.name)
	w.Str("mode", modeName(v.mode))
	w.Dec("status", int64(res.status))
	w.Dec("sync", int64(res.sync))
	w.Dec("errs", int64(res.errs))
	w.Dec("cals", res.calS)
	w.Dec("calms", res.calMS)
	w.Str("hse", res.hse)
	w.Dec("ok", b2i(res.sync == 0x1424 && res.errs == 0))
	out(w.End())
}

func main() {
	time.Sleep(200 * time.Millisecond)
	wle5.Setup() // pins, SUBGHZSPI, switch idle; the radio is not touched
	for round := int64(1); ; round++ {
		w.Open("wle5probe")
		w.Dec("round", round)
		w.Str("state", "begin")
		out(w.End())
		for id, v := range variants {
			run(id, v)
		}
		subghz.Mode = subghz.BusyMS
		w.Open("wle5probe")
		w.Dec("round", round)
		w.Str("state", "end")
		out(w.End())
		time.Sleep(5 * time.Second)
	}
}
