//go:build stm32u083

package rnghw

import "device/stm32"

const clk48HSI48 = 3 // RCC_CCIPR.CLK48SEL, see the package comment

// startClock starts HSI48, selects it for the RNG (and USB) and enables the
// RNG's bus clock.
func startClock() error {
	rcc := stm32.RCC
	rcc.CRRCR.SetBits(stm32.RCC_CRRCR_HSI48ON)
	for i := 0; !rcc.CRRCR.HasBits(stm32.RCC_CRRCR_HSI48RDY); i++ {
		if i > 100000 {
			return ErrClock
		}
	}
	rcc.CCIPR.ReplaceBits(clk48HSI48, stm32.RCC_CCIPR_CLK48SEL_Msk>>stm32.RCC_CCIPR_CLK48SEL_Pos, stm32.RCC_CCIPR_CLK48SEL_Pos)
	rcc.AHBENR.SetBits(stm32.RCC_AHBENR_RNGEN)
	_ = rcc.AHBENR.Get()
	return nil
}
