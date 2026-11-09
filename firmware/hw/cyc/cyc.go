//go:build stm32u083

// Package cyc counts CPU cycles with SysTick and switches the system clock
// between 4 MHz and 48 MHz for benchmarks.
//
// The Cortex-M0+ has no DWT cycle counter, but SysTick counts down at the
// CPU clock. The TinyGo runtime of this chip uses TIM16 for time, so SysTick
// is free. It is 24 bits wide: one measured interval must stay below
// 2^24 cycles (4.2 s at 4 MHz, 0.35 s at 48 MHz).
package cyc

import (
	"device/arm"
	"device/stm32"
)

const mask = 1<<24 - 1

// Start runs SysTick from the CPU clock without its interrupt.
func Start() {
	arm.SYST.SYST_CSR.Set(0)
	arm.SYST.SYST_RVR.Set(mask)
	arm.SYST.SYST_CVR.Set(0)
	arm.SYST.SYST_CSR.Set(arm.SYST_CSR_ENABLE | arm.SYST_CSR_CLKSOURCE)
}

// Now returns the counter. It counts down.
func Now() uint32 { return arm.SYST.SYST_CVR.Get() }

// Since returns the cycles since t.
func Since(t uint32) uint32 { return (t - arm.SYST.SYST_CVR.Get()) & mask }

// Stat summarizes per-iteration cycle counts.
type Stat struct {
	N        uint32
	Min, Max uint32
	Sum      uint64
}

func (s *Stat) Add(c uint32) {
	if s.N == 0 || c < s.Min {
		s.Min = c
	}
	if c > s.Max {
		s.Max = c
	}
	s.N++
	s.Sum += uint64(c)
}

func (s Stat) Avg() uint32 {
	if s.N == 0 {
		return 0
	}
	return uint32(s.Sum / uint64(s.N))
}

var overhead uint32

// Calibrate measures the cost of an empty call through Measure, which
// Measure then subtracts from every sample.
func Calibrate() {
	overhead = 0
	s := Measure(64, func() {})
	overhead = s.Min
}

// Overhead returns the calibrated cost of the measurement itself.
func Overhead() uint32 { return overhead }

// Measure calls f n times and returns the cycles of each call.
//
//go:noinline
func Measure(n int, f func()) Stat {
	var s Stat
	for i := 0; i < n; i++ {
		t := Now()
		f()
		c := Since(t)
		if c > overhead {
			c -= overhead
		} else {
			c = 0
		}
		s.Add(c)
	}
	return s
}

// Clock control. RM0503: MSIRANGE 6 = 4 MHz (reset), 11 = 48 MHz. 48 MHz
// needs voltage range 1 and one flash wait state.
const (
	msiRange4MHz  = 6
	msiRange48MHz = 11
	vosRange1     = 1 << stm32.PWR_CR1_VOS_Pos
	usartISRTC    = 1 << 6
)

var hz uint32 = 4_000_000

// Hz returns the current system clock.
func Hz() uint32 { return hz }

// FlashACR returns the flash access control register, for the report.
func FlashACR() uint32 { return stm32.FLASH.ACR.Get() }

// VOS returns PWR_CR1.VOS: 1 is range 1 (up to 48 MHz), 2 is range 2.
func VOS() uint32 {
	stm32.RCC.APBENR1.SetBits(stm32.RCC_APBENR1_PWREN)
	return (stm32.PWR.CR1.Get() & stm32.PWR_CR1_VOS_Msk) >> stm32.PWR_CR1_VOS_Pos
}

func waitUARTIdle() {
	for i := 0; i < 1_000_000 && stm32.USART2.ISR.Get()&usartISRTC == 0; i++ {
	}
}

func setMSI(r uint32) {
	cr := stm32.RCC.CR.Get() &^ stm32.RCC_CR_MSIRANGE_Msk
	stm32.RCC.CR.Set(cr | r<<stm32.RCC_CR_MSIRANGE_Pos | stm32.RCC_CR_MSIRGSEL)
	for !stm32.RCC.CR.HasBits(stm32.RCC_CR_MSIRDY) {
	}
}

func setLatency(ws uint32) {
	acr := stm32.FLASH.ACR.Get() &^ stm32.FLASH_ACR_LATENCY_Msk
	stm32.FLASH.ACR.Set(acr | ws)
	for stm32.FLASH.ACR.Get()&stm32.FLASH_ACR_LATENCY_Msk != ws {
	}
}

// To48MHz raises the system clock to 48 MHz. The UART and the TinyGo timer
// keep their 4 MHz settings, so do not print, sleep or read the time until
// To4MHz returns. It returns false when the voltage range does not allow it.
func To48MHz() bool {
	if VOS() != 1 {
		stm32.PWR.CR1.Set(stm32.PWR.CR1.Get()&^stm32.PWR_CR1_VOS_Msk | vosRange1)
		for stm32.PWR.SR2.HasBits(stm32.PWR_SR2_VOSF) {
		}
		if VOS() != 1 {
			return false
		}
	}
	waitUARTIdle()
	setLatency(1) // raise wait states before the clock
	setMSI(msiRange48MHz)
	hz = 48_000_000
	return true
}

// To4MHz returns to the reset clock.
func To4MHz() {
	setMSI(msiRange4MHz)
	setLatency(0) // lower wait states after the clock
	hz = 4_000_000
}

// SetUART2Baud reprograms USART2 for the current clock. Use it to prove the
// clock frequency: text sent at 48 MHz arrives readable only if the clock
// really is 48 MHz.
func SetUART2Baud(baud uint32) {
	waitUARTIdle()
	u := stm32.USART2
	u.CR1.ClearBits(stm32.USART_CR1_UE)
	u.BRR.Set(hz / baud)
	u.CR1.SetBits(stm32.USART_CR1_UE)
}
