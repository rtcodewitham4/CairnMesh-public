//go:build stm32u083

package aeshw

import "device/stm32"

// enableClock: the AES block is on the AHB of the STM32U083 (RM0503).
func enableClock() {
	stm32.RCC.AHBENR.SetBits(stm32.RCC_AHBENR_AESEN)
	_ = stm32.RCC.AHBENR.Get() // wait for the peripheral clock
	stm32.RCC.AHBRSTR.SetBits(stm32.RCC_AHBRSTR_AESRST)
	stm32.RCC.AHBRSTR.ClearBits(stm32.RCC_AHBRSTR_AESRST)
}
