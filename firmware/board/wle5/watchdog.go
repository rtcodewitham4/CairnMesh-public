//go:build cairn_wle5

package wle5

import (
	"device/stm32"

	"cairnmesh/firmware/board/wle5/safety"
)

// Watchdog for the programs that leave the radio transmitting for a while
// (cmd/wle5cw). The IWDG (RM0461, 0x40003000) runs from the LSI and, once
// started, nothing but a reset stops it: the program must call FeedWatchdog
// from every loop it can be in afterwards.
//
// A system reset does NOT reset the sub-GHz radio (RM0461 6.1.4: only RFRST
// and Shutdown mode do), so a carrier would go on through the watchdog reset.
// Setup therefore puts the radio into reset first thing (RFRST), which ends
// any transmission left by the previous run within the boot time of the chip.

const (
	iwdgStart  = 0xCCCC
	iwdgUnlock = 0x5555
	iwdgFeed   = 0xAAAA
	iwdgUpdate = 0x7 // SR: PVU, RVU, WVU
	// updateSpins bounds the wait for the register update (a few LSI cycles).
	updateSpins = 1000000
)

// StartWatchdog starts the IWDG with a timeout of about ms milliseconds (8 ms
// steps, LSI error several percent). It stops counting while a debugger halts
// the core (DBGMCU_APB1FZR1.DBG_IWDG_STOP).
func StartWatchdog(ms uint32) {
	stm32.DBGMCU.APB1FZR1.SetBits(stm32.DBGMCU_APB1FZR1_DBG_IWDG_STOP)
	stm32.IWDG.KR.Set(iwdgStart)
	stm32.IWDG.KR.Set(iwdgUnlock)
	stm32.IWDG.PR.Set(safety.WatchdogPrescaler)
	stm32.IWDG.RLR.Set(safety.WatchdogReload(ms))
	for i := 0; i < updateSpins && stm32.IWDG.SR.Get()&iwdgUpdate != 0; i++ {
	}
	stm32.IWDG.KR.Set(iwdgFeed)
}

// FeedWatchdog restarts the count.
func FeedWatchdog() { stm32.IWDG.KR.Set(iwdgFeed) }

// WatchdogReset reports whether the last reset was the IWDG's (RCC_CSR.IWDGRSTF).
func WatchdogReset() bool { return stm32.RCC.CSR.HasBits(stm32.RCC_CSR_IWDGRSTF) }

// ClearResetFlags clears the reset flags (RCC_CSR.RMVF) so the next boot
// reads its own cause.
func ClearResetFlags() { stm32.RCC.CSR.SetBits(stm32.RCC_CSR_RMVF) }
