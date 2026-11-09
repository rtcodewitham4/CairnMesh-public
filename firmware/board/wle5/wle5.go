//go:build cairn_wle5

// Package wle5 is the board support of the CairnMesh node on an STM32WLE5CCU6
// module: the clock facts, the module's RF switch and the radio inside the
// chip. It is the WLE5 counterpart of package board (U083), whose AES, RNG,
// flash and watchdog drivers are not ported yet.
//
// NOT VERIFIED ON A BOARD (2026-11-05): written from RM0461 Rev 11, DS13105
// Rev 12 and the EBYTE E77-M user manual V1.6.
package wle5

import (
	"errors"
	"machine"
	"strconv"
	"time"

	"device/stm32"

	"cairnmesh/firmware/board/wle5/safety"
	"cairnmesh/firmware/hw/subghz"
	"cairnmesh/firmware/radio/sx126x"
)

// tcxoMV is the TCXO supply the radio is told to give on PB0-VDDTCXO, in mV,
// set at link time: -ldflags "-X cairnmesh/firmware/board/wle5.tcxoMV=1700"
// (make WLE5_TCXO_MV=1700). It has no default on purpose: the module's TCXO
// part is not known (E77 manual V1.6 gives no voltage) and the radio would
// feed it whatever is set. Look the part up (or measure PB0 with a current-
// limited supply, CONFIRM below) before the first power-on.
var tcxoMV string

// ErrNoTCXOVoltage is returned by Radio setup when tcxoMV was not set.
var ErrNoTCXOVoltage = errors.New("wle5: TCXO voltage not set (make WLE5_TCXO_MV=...)")

// Radio is the on-chip radio; Setup wires it, then call InitRadio.
var Radio sx126x.Radio

// Setup configures the board's pins, turns the SUBGHZSPI on and wires Radio.
// It does not touch the radio: InitRadio does (reset, TCXO command, then PB0
// to VDDTCXO through HSEBYPPWR as RM0461 6.2.1 orders: the voltage is set in
// the radio first; startHSE also sets HSEON, which the radio does not need
// (see below), and the CPU runs on HSI16/PLL, see runtime_cairn_wle5.go).
func Setup() {
	// The radio is not reset by a system reset (RM0461 6.1.4): hold it in reset
	// from the first line, so nothing a previous run (or the watchdog) left on
	// the air goes on while this program starts. InitRadio releases it.
	stm32.RCC.CSR.SetBits(stm32.RCC_CSR_RFRST)
	out := machine.PinConfig{Mode: machine.PinOutput}
	setupSwitch(out)
	for _, led := range leds {
		led.Configure(out)
		led.Low()
	}
	subghz.Init(machine.CPUFrequency())
	Radio.Bus = subghz.Bus{}
	Radio.Pins = subghz.Pins()
	Radio.Switch = RFSwitch{}
	Radio.PowerCap = capPower
	// PB0 becomes the TCXO supply, and HSE32 is turned on, only after the
	// radio has been told the voltage.
	Radio.AfterTCXO = startHSE
}

// capPower is the board's power ceiling (safety.MaxPowerDBm = 10 dBm unless
// the build has the tag wle5_allow_high_power). It is on Radio.PowerCap, so it
// limits Init's PowerDBm and every Emit, not only what a main asks for.
func capPower(dbm int8) int8 { return safety.ClampPower(dbm, allowHighPower) }

// EffectivePower is the power the radio really sends when a program asks for
// dbm: the board's cap applied. For status lines; the cap itself is enforced on
// Radio.PowerCap, not by anything a caller does with this value.
func EffectivePower(dbm int8) int8 { return capPower(dbm) }

// RadioOff ends any transmission without the SPI bus: the RF switch to idle,
// then the radio held in reset (RCC_CSR.RFRST). The radio is unusable until
// InitRadio; Radio.Check and the driver must not be used after it.
func RadioOff() {
	RFSwitch{}.Off()
	stm32.RCC.CSR.SetBits(stm32.RCC_CSR_RFRST)
}

// EnableRadioInterrupt makes the radio driver use the radio's interrupt
// (NVIC 50, hw/subghz) instead of asking the radio for its status on every
// poll. Call it once after InitRadio succeeded. It is NOT needed again after
// Radio.Recover: the interrupt mode, the NVIC enable and EXTI line 44 live in
// the MCU and survive a radio reset, and Recover's Init sets the radio's own
// interrupt mask again (SetDioIrqParams) and clears its status; an event
// raised during the reset is lost, which the next Check catches (cmd/wle5link
// recovers without calling this). Chip-level, not board-level: no pin is involved.
func EnableRadioInterrupt() { subghz.EnableIRQ() }

// RadioInterrupts is how many times the radio's interrupt handler has run.
func RadioInterrupts() uint32 { return subghz.IRQCount() }

// HSEState is how the last startHSE went.
type HSEState uint8

const (
	HSENotRun  HSEState = iota // the hook has not run (no TCXO command yet)
	HSEReady                   // HSERDY came up
	HSETimeout                 // HSERDY did not come up in hseTimeout
	HSEStuck                   // HSERDY did not go down when HSEON was cleared
)

func (s HSEState) String() string {
	switch s {
	case HSEReady:
		return "ready"
	case HSETimeout:
		return "timeout"
	case HSEStuck:
		return "stuck"
	}
	return "notrun"
}

// HSE is the result of the last startHSE, for the bench check to print.
var HSE HSEState

// hseTimeout bounds the wait for HSERDY. The radio's own TCXO start timeout is
// 5 ms (Init's Set_TcxoMode, 320 steps of 15.625 us); this is four times that.
const hseTimeout = 20 * time.Millisecond

func waitHSE(ready bool) bool {
	for start := time.Now(); time.Since(start) < hseTimeout; {
		if stm32.RCC.CR.HasBits(stm32.RCC_CR_HSERDY) == ready {
			return true
		}
	}
	return stm32.RCC.CR.HasBits(stm32.RCC_CR_HSERDY) == ready
}

// stopHSE puts HSE32 and the TCXO supply back to off (RM0461 RCC_CR bit 21:
// HSEBYPPWR is writable only while HSEON = HSERDY = 0), so the next Init starts
// as the first. The CPU runs on the PLL from HSI16 and does not use HSE32.
func stopHSE() {
	stm32.RCC.CR.ClearBits(stm32.RCC_CR_HSEON)
	if !waitHSE(false) {
		HSE = HSEStuck
		return
	}
	stm32.RCC.CR.ClearBits(stm32.RCC_CR_HSEBYPPWR)
	HSE = HSENotRun
}

// startHSE is Radio.AfterTCXO, RM0461 6.2.1 "External source (HSE32 TCXO)":
// the voltage is already set in the radio (Set_TcxoMode); now HSEBYPPWR
// switches PB0 to VDDTCXO, then HSEON turns HSE32 on for the CPU and HSERDY
// says the TCXO clock is there. The first bench failure (2026-11-06, BUSY
// "stuck") was NOT missing HSEON: it was the RFBUSYMS mask that never falls
// (see hw/subghz Busy). The probe matrix showed HSEBYPPWR alone ("byponly")
// calibrates and reads back just as well, and RM0461 says the radio has its
// own HSE32 enable. So HSEON is not needed for the radio; it is kept for now
// because it gives a timed HSERDY check of the TCXO, but it keeps the TCXO
// powered, so the low-power work should drop it (HSEBYPPWR only).
// It returns false on timeout; Init then stops instead of waiting on BUSY.
func startHSE() bool {
	stm32.RCC.CR.SetBits(stm32.RCC_CR_HSEBYPPWR)
	stm32.RCC.CR.SetBits(stm32.RCC_CR_HSEON)
	if waitHSE(true) {
		HSE = HSEReady
		return true
	}
	HSE = HSETimeout
	return false
}

// StartHSE, StopHSE and EnableTCXOSupply are the pieces of the AfterTCXO hook
// for the bench probe (cmd/wle5probe), which tries them one at a time.
func StartHSE() bool { return startHSE() }
func StopHSE()       { stopHSE() }

// EnableTCXOSupply sets HSEBYPPWR only (the first version of the hook).
func EnableTCXOSupply() {
	stm32.RCC.CR.SetBits(stm32.RCC_CR_HSEBYPPWR)
	HSE = HSENotRun
}

// DebugRegs returns PWR_SR2 and RCC_CR for a failure line.
func DebugRegs() (sr2, cr uint32) { return stm32.PWR.SR2.Get(), stm32.RCC.CR.Get() }

// LED sets indicator n (0 = link, 1 = rx, 2 = tx on the E77); nothing when the
// board has no such LED.
func LED(n int, on bool) {
	if n >= 0 && n < len(leds) {
		leds[n].Set(on)
	}
}

// TCXOMillivolts is the link-time TCXO setting; ok is false when it was not
// set (or is not a number).
func TCXOMillivolts() (mv uint16, ok bool) {
	v, err := strconv.ParseUint(tcxoMV, 10, 16)
	if err != nil || v == 0 {
		return 0, false
	}
	return uint16(v), true
}

// InitRadio runs the driver's Init for the 433 MHz network (cfg gives the
// modem), with the TCXO voltage from cfg.TCXOMillivolts if the caller set it,
// else from the link-time setting (none: ErrNoTCXOVoltage). dcdc is per board:
// PCB4 has the SMPS inductor, the E77 is not known to.
func InitRadio(cfg sx126x.Config) error {
	if cfg.TCXOMillivolts == 0 {
		mv, ok := TCXOMillivolts()
		if !ok {
			return ErrNoTCXOVoltage
		}
		cfg.TCXOMillivolts = mv
	}
	stopHSE() // a second Init starts from HSE off, as the first
	cfg.PowerDBm = capPower(cfg.PowerDBm)
	cfg.NoDio2RfSwitch = true
	cfg.DCDC = dcdc
	if !Radio.Init(cfg) {
		return errors.New("wle5: radio did not initialise")
	}
	return nil
}
