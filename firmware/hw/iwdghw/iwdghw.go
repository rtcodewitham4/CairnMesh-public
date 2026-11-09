// Package iwdghw drives the independent watchdog (IWDG) of the STM32U083:
// once started, the chip resets unless Feed comes within the timeout, and
// nothing but a reset stops it.
//
// TinyGo's machine.Watchdog writes the prescaler and reload before starting
// the IWDG; on this IWDG they only take while it runs (its LSI clock starts
// with it), so this package follows RM0503 instead: start, unlock, set,
// wait for the status register, feed.
package iwdghw

import "device/stm32"

const (
	keyStart  = 0xCCCC
	keyUnlock = 0x5555
	keyFeed   = 0xAAAA

	prDiv256 = 6 // 32 kHz / 256: 8 ms a count
	lsiHz    = 32000
	maxCount = 0xFFF

	// srUpdating: PVU, RVU, WVU, EWU (a register still being updated).
	srUpdating = 0xF
	// updateSpins bounds the wait for the update (a few LSI cycles).
	updateSpins = 1000000
)

// Start starts the watchdog with a timeout of about ms milliseconds (8 ms
// steps, at most about 32 s; the LSI is ±several %). While a debugger
// halts the core the watchdog stops too.
func Start(ms uint32) {
	stm32.RCC.SetDBGCFGR_DBGEN(1)
	stm32.DBGMCU.APB1FZR.SetBits(stm32.DBGMCU_APB1FZR_DBG_IWDG_STOP)

	count := ms * (lsiHz / 1000) / 256
	count = min(max(count, 1), maxCount)
	stm32.IWDG.KR.Set(keyStart)
	stm32.IWDG.KR.Set(keyUnlock)
	stm32.IWDG.PR.Set(prDiv256)
	stm32.IWDG.RLR.Set(count)
	for i := 0; i < updateSpins && stm32.IWDG.SR.Get()&srUpdating != 0; i++ {
	}
	Feed()
}

// Feed restarts the countdown.
func Feed() {
	stm32.IWDG.KR.Set(keyFeed)
}

// ResetByWatchdog reports whether the last reset came from the watchdog,
// then clears the reset flags.
func ResetByWatchdog() bool {
	by := stm32.RCC.CSR.HasBits(stm32.RCC_CSR_IWDGRSTF)
	stm32.RCC.CSR.SetBits(stm32.RCC_CSR_RMVF)
	return by
}
