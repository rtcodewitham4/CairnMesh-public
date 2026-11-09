// Package rnghw drives the true random number generator of the STM32U083 and
// the STM32WLE5 (the same block; the kernel clock differs: clock_stm32u083.go,
// clock_cairn_wle5.go).
// TinyGo has no RNG driver for the U0 family (machine_stm32_rng.go excludes
// stm32u0), and crypto/rand has no entropy source there. The secure link
// needs random keys and nonces.
//
// On the U083 the RNG runs from HSI48. The reset value of RCC_CCIPR.CLK48SEL (00) gives
// the RNG no clock at all (SR.CECS at once). Measured on the board, with
// HSI48 and the PLL switched on and off: 01 still works without HSI48 (MSI),
// 10 never works with the PLL off (PLLQ), 11 stops when HSI48 stops, so 11
// is HSI48. The configuration bits of RNG_CR are left at their reset value.
// See RM0503, "True random number generator".
//
// The STM32WLE5 has the same RNG block but no HSI48: its clock setup is in
// clock_cairn_wle5.go (PLLQ). Configure and Uint32 are shared.
package rnghw

import (
	"device/stm32"
	"errors"
)

const (
	crRNGEN   = 1 << 2
	crCONDRST = 1 << 30

	srDRDY = 1 << 0
	srCECS = 1 << 1
	srSECS = 1 << 2
	srCEIS = 1 << 5
	srSEIS = 1 << 6
)

var (
	ErrClock   = errors.New("rng: clock error")
	ErrSeed    = errors.New("rng: seed error")
	ErrTimeout = errors.New("rng: timeout")
)

// Configure starts the RNG's kernel clock (startClock: HSI48 on the U083, the
// PLL's Q output on the WLE5, which has no HSI48) and the RNG.
func Configure() error {
	if err := startClock(); err != nil {
		return err
	}
	stm32.RNG.CR.SetBits(crRNGEN)
	return nil
}

// Uint32 returns one 32-bit random word.
func Uint32() (uint32, error) {
	rng := stm32.RNG
	for i := 0; ; i++ {
		sr := rng.SR.Get()
		if sr&(srSECS|srSEIS) != 0 {
			recoverSeed()
			return 0, ErrSeed
		}
		if sr&(srCECS|srCEIS) != 0 {
			rng.SR.ClearBits(srCEIS)
			return 0, ErrClock
		}
		if sr&srDRDY != 0 {
			return rng.DR.Get(), nil
		}
		if i > 100000 {
			return 0, ErrTimeout
		}
	}
}

// recoverSeed follows the seed error recovery sequence: reset the
// conditioning logic, clear SEIS and wait until SECS is clear.
func recoverSeed() {
	rng := stm32.RNG
	rng.CR.SetBits(crCONDRST)
	rng.CR.ClearBits(crCONDRST)
	rng.SR.ClearBits(srSEIS)
	for i := 0; i < 100000 && rng.SR.HasBits(srSECS); i++ {
	}
}

// Reader reads random bytes; it implements io.Reader.
type Reader struct{}

func (Reader) Read(p []byte) (int, error) {
	for i := 0; i < len(p); i += 4 {
		w, err := Uint32()
		if err != nil {
			return i, err
		}
		for j := 0; j < 4 && i+j < len(p); j++ {
			p[i+j] = byte(w >> (8 * j))
		}
	}
	return len(p), nil
}
