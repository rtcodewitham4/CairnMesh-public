// Package crchw computes the link CRC (CRC16-CCITT-FALSE: poly 0x1021,
// init 0xFFFF, no reflection) with the CRC unit of the STM32U083, the same
// value as link.CRC16. See RM0503, "Cyclic redundancy check calculation
// unit".
package crchw

import (
	"device/stm32"
	"runtime/volatile"
	"unsafe"
)

const (
	crRESET      = 1 << 0
	crPolysize16 = 1 << 3 // POLYSIZE = 01
)

// Configure enables the clock and sets the polynomial.
func Configure() {
	stm32.RCC.AHBENR.SetBits(stm32.RCC_AHBENR_CRCEN)
	_ = stm32.RCC.AHBENR.Get()
	c := stm32.CRC
	c.POL.Set(0x1021)
	c.INIT.Set(0xFFFF)
	c.CR.Set(crPolysize16)
}

// CRC16 returns the CRC of data. The unit is shared: do not call it from an
// interrupt handler while the main code also uses it.
func CRC16(data []byte) uint16 {
	c := stm32.CRC
	c.CR.Set(crPolysize16 | crRESET)
	// 8-bit writes feed one byte each; a 32-bit write would feed four.
	dr8 := (*volatile.Register8)(unsafe.Pointer(&c.DR))
	for _, b := range data {
		dr8.Set(b)
	}
	return uint16(c.DR.Get())
}
