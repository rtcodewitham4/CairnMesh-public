//go:build cairn_wle5

// Command wle5cw sends one unmodulated carrier from the STM32WLE5's radio for
// a current meter or a spectrum analyzer: 433.5 MHz, WLE5_CW_DBM dBm (default
// 10, never above 10), 10 s, then standby, and it stays in receive (no
// transmit) afterwards.
//
// An antenna or a 50 ohm load MUST be on the antenna port, and the build must
// say so: without WLE5_ANT_OK=1 (linked in as main.antOK, safety.AntennaConfirmed)
// the program prints how to confirm and never touches the radio. It waits 5 s
// after the console line "CW in 5 s" so the supply and the meter can settle, and
// it only transmits when the power setting was accepted and the radio
// initialised (the TCXO started, no radio error).
//
// Stopping. SetTxContinuousWave has no timeout in the chip: the carrier stays
// until the next command. Three layers end it. 1) The driver sends Set_Standby
// when the 10 s are over (sx126x.Busy). 2) A hard cap, safety.CarrierHardCapSeconds
// (12 s): if the loop is still running then, wle5.RadioOff puts the RF switch to
// idle and the radio into reset (RCC_CSR.RFRST) without the SPI bus, which a stuck
// or failing driver cannot prevent. 3) The IWDG (3 s, started just before the
// carrier, fed by every loop): if the MCU hangs the chip resets, wle5.Setup holds
// the radio in reset again first thing (a system reset does not reset the radio,
// RM0461 6.1.4), and a start that finds RCC_CSR.IWDGRSTF set does not transmit
// again. The watchdog cannot be stopped once started; every loop feeds it.
//
// Power. The ceiling is cwpower.Max = 10 dBm (first transmission); a higher
// or malformed WLE5_CW_DBM is
// refused at start: it prints the line below and never touches the radio.
// What PowerDBm = 10 means on this chip (RM0461 Rev 11, Set_PaConfig / Set_TxParams
// and Table 27; AN5457 Rev 3 Table 2), as sx126x.setPower sends it:
//   - Set_PaConfig(0x04, 0x07, 0x00, 0x01): PaDutyCycle 0x04, HpMax 0x07, PaSel
//     0 = the HP PA (RFO_HP, the output both the E77-400M22S, "21.5 dBm
//     RFO_HP", and PCB4 use); the last byte is fixed 0x01. This is the +22 dBm
//     row of RM0461 Table 27 and of AN5457 ("not above 0x04"). RM0461 Rev 11
//     contradicts itself: the Set_PaConfig caution says PaDutyCycle must be
//     below 0x4 in HP mode. The driver follows the table and AN5457; the
//     output at 0x04 is for the spectrum analyzer to confirm.
//   - Set_TxParams(0x0A, 0x04): with the HP PA the power byte is a signed dBm,
//     0xF7 (-9) .. 0x16 (+22), so 0x0A is +10 dBm at the PA (200 us ramp).
//     It is the "sub-optimal" use of the +22 dBm settings (AN5457 5.1.2: a
//     different power makes the setting sub-optimal): the current is higher
//     than the 10 dBm optimum, the level is the setting's, within the PA's
//     tolerance. The optimum for +10 dBm is the LP PA (0x01, 0x00, PaSel 1,
//     power 0x0D) on RFO_LP, which these boards do not use.
//   - The level at the antenna port is also the board's loss and match; only
//     a spectrum analyzer tells.
//
// RF switch. sx126x.Radio.cmd drives wle5.RFSwitch before the command that
// needs it: Set_Standby -> Off, Set_TxContinuousWave and Set_Tx -> Tx,
// Set_Rx -> Rx. E77 (EBYTE manual V1.6, 3.1 note 1): TX is PA6 (TXEN) = 1 and
// PA7 (RXEN) = 0; RX is PA6 = 0, PA7 = 1; Off is both 0; the pin that goes
// low is cleared first, so both are never 1. The carrier's order is
// Set_Standby (Off), power and frequency, then Set_TxContinuousWave (Tx),
// and at the end Set_Standby (Off), restore, Set_Rx (Rx). PCB4 is a single
// control line: PA6 high = TX path, low = RX path, PA7 is the switch's supply.
//
// Console on USART2 (PA2/PA3), 115200 8N1:
//
//	wle5cw: clock <Hz>
//	wle5cw: power <n> dBm | wle5cw: power "<text>" refused: <why> (then it never starts the radio)
//	wle5cw: no antenna ... not transmitting         (build without WLE5_ANT_OK=1; the radio is not touched)
//	wle5cw: restarted by the watchdog ...           (then it never transmits)
//	wle5cw: hard cap, radio reset                   (the carrier outlasted its cap; RadioOff)
//	wle5cw: radio ready | wle5cw: radio: <error>   (then it never transmits)
//	wle5cw: CW in 5 s
//	wle5cw: CW on | wle5cw: CW refused
//	wle5cw: t=<s>                                  once a second
//	wle5cw: done
//
// Build: make wle5-build CMD=wle5cw WLE5_ANT_OK=1 WLE5_TCXO_MV=<mV> [WLE5_CW_DBM=<dBm>] [WLE5_BOARD=pcb4]
package main

import (
	"time"

	"machine"

	"cairnmesh/firmware/board/wle5"
	"cairnmesh/firmware/board/wle5/safety"
	"cairnmesh/firmware/cmd/wle5cw/cwpower"
	"cairnmesh/firmware/proto"
	"cairnmesh/firmware/radio"
	"cairnmesh/firmware/radio/sx126x"
)

const (
	freqHz  = 433_500_000
	seconds = 10

	// watchdogMs: longest a loop below may go without feeding. The slowest
	// legitimate pass is a few SPI commands (well under 100 ms).
	watchdogMs = 3000
)

// cwDBM is the requested power in dBm, set at link time:
// -ldflags "-X main.cwDBM=5" (make WLE5_CW_DBM=5). Empty: cwpower.Default.
var cwDBM string

// antOK is "1" when the build says an antenna or a 50 ohm load is connected
// (make WLE5_ANT_OK=1, -X main.antOK=1).
var antOK string

func main() {
	time.Sleep(200 * time.Millisecond)
	println("wle5cw: clock", machine.CPUFrequency(), "Hz")
	// First of all: the RF switch to idle and the radio into reset, so a carrier
	// left by the previous program or run ends (a system reset does not reset
	// the radio, RM0461 6.1.4). InitRadio releases it.
	wle5.Setup()
	powerDB, err := cwpower.Parse(cwDBM)
	if err != nil {
		println("wle5cw: power \"" + cwDBM + "\" refused: " + err.Error())
		for { // the radio is not set up, so nothing can transmit
			time.Sleep(time.Second)
		}
	}
	powerDB = min(powerDB, cwpower.Max) // belt and braces
	println("wle5cw: power", powerDB, "dBm")
	if !safety.AntennaConfirmed(antOK) {
		println("wle5cw: no antenna / 50 ohm load confirmation: not transmitting. Connect one, then build with WLE5_ANT_OK=1")
		holdOff()
	}
	if wle5.WatchdogReset() {
		wle5.ClearResetFlags()
		println("wle5cw: restarted by the watchdog: not transmitting")
		holdOff()
	}
	wle5.ClearResetFlags()
	cfg := sx126x.Config{
		FrequencyHz:     freqHz,
		SpreadingFactor: 7,
		BandwidthHz:     125_000,
		CodingRate:      5,
		PowerDBm:        powerDB,
	}
	if err := wle5.InitRadio(cfg); err != nil {
		println("wle5cw: radio:", err.Error(), "faults", wle5.Radio.Stats.Faults)
		hold()
	}
	println("wle5cw: radio ready")
	wle5.LED(1, true)
	println("wle5cw: CW in 5 s")
	time.Sleep(5 * time.Second)

	wle5.StartWatchdog(watchdogMs)
	start := time.Now()
	now := func() proto.Time { return proto.Time(time.Since(start).Microseconds()) }
	hardCap := proto.Time(safety.CarrierHardCapSeconds(seconds)) * proto.Second
	sig := radio.Signal{
		Kind:        radio.Carrier,
		FrequencyHz: freqHz,
		PowerDBm:    powerDB,
		Duration:    seconds * proto.Second,
	}
	if !wle5.Radio.Emit(&sig, now()) {
		println("wle5cw: CW refused")
		hold()
	}
	println("wle5cw: CW on")
	wle5.LED(2, true)
	last := -1
	for {
		wle5.FeedWatchdog()
		if now() > hardCap {
			wle5.RadioOff()
			wle5.LED(2, false)
			println("wle5cw: hard cap, radio reset")
			holdOff()
		}
		if !wle5.Radio.Busy(now()) {
			break
		}
		if s := int(now() / proto.Second); s != last {
			last = s
			println("wle5cw: t=", s)
		}
		time.Sleep(20 * time.Millisecond)
	}
	wle5.LED(2, false)
	println("wle5cw: done")
	hold()
}

// hold stays in receive, never transmits, and keeps the radio polled.
func hold() {
	for {
		wle5.FeedWatchdog()
		wle5.Radio.Check()
		time.Sleep(500 * time.Millisecond)
	}
}

// holdOff never returns and never touches the radio (it is in reset, or was
// never set up); it keeps feeding the watchdog, which cannot be stopped.
func holdOff() {
	for {
		wle5.FeedWatchdog()
		time.Sleep(100 * time.Millisecond)
	}
}
