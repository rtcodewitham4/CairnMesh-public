//go:build cairn_wle5

package rnghw

import "device/stm32"

// startClock on the STM32WLE5 (RM0461 RCC_CCIPR.RNGSEL, 20.3.6, 20.6.2): there
// is no HSI48; the RNG's kernel clock is one of PLLQ / LSI / LSE / MSI.
// The runtime (patches/runtime_cairn_wle5.go.txt) runs the PLL at 96 MHz with
// Q = 48 MHz and PLLQEN set, which is the clock ST validated the RNG with
// ("rng_clk = 48 MHz, CED cleared", 20.6.2) and above AHB/32 (the clock
// checker's condition, 20.3.6). So RNGSEL = 00 (PLLQ, also the reset value) and
// the bus clock on (RCC_AHB3ENR.RNGEN). It refuses (ErrClock) when the PLL or
// its Q output is off, instead of letting the RNG flag a clock error later.
// RNG_CR is left at its reset value, as on the U083. NOT VERIFIED ON A BOARD:
// the entropy of this configuration has not been measured.
func startClock() error {
	rcc := stm32.RCC
	if !rcc.CR.HasBits(stm32.RCC_CR_PLLRDY) || !rcc.PLLCFGR.HasBits(stm32.RCC_PLLCFGR_PLLQEN) {
		return ErrClock
	}
	rcc.CCIPR.ReplaceBits(stm32.RCC_CCIPR_RNGSEL_PLLQ, stm32.RCC_CCIPR_RNGSEL_Msk>>stm32.RCC_CCIPR_RNGSEL_Pos, stm32.RCC_CCIPR_RNGSEL_Pos)
	rcc.AHB3ENR.SetBits(stm32.RCC_AHB3ENR_RNGEN)
	_ = rcc.AHB3ENR.Get()
	return nil
}
