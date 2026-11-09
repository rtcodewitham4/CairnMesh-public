//go:build cairn_wle5

package aeshw

import "device/stm32"

// enableClock: on the STM32WLE5 the AES block is on AHB3 (RCC_AHB3ENR.AESEN,
// RCC_AHB3RSTR.AESRST; RM0461 6.4). The block itself is the same IP as the
// STM32U083's (registers CR, SR, DINR, DOUTR, KEYR0..3 at the same offsets),
// so the rest of the driver is shared. NOT VERIFIED ON A BOARD: Device.SelfTest
// (FIPS-197) is what the first boot of cmd/wle5node runs.
func enableClock() {
	stm32.RCC.AHB3ENR.SetBits(stm32.RCC_AHB3ENR_AESEN)
	_ = stm32.RCC.AHB3ENR.Get() // wait for the peripheral clock
	stm32.RCC.AHB3RSTR.SetBits(stm32.RCC_AHB3RSTR_AESRST)
	stm32.RCC.AHB3RSTR.ClearBits(stm32.RCC_AHB3RSTR_AESRST)
}
