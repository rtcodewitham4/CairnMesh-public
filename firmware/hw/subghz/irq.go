//go:build cairn_wle5

package subghz

import (
	"device/stm32"
	"runtime/interrupt"
	"sync/atomic"
)

// The radio's interrupt (RM0461 Table 78: position 50, "Radio IRQ, Busy",
// radio IRQs through EXTI line 44 and RFBUSY through EXTI line 45; Table 81:
// line 44 is a direct line, line 45 configurable). TinyGo 0.42.0's WLE5 SVD
// already names it IRQ_Radio_IRQ_Busy = 50 and the vector table points
// Radio_IRQ_Busy_IRQHandler at it, so no TinyGo change is needed. RFBUSY (line
// 45) shares the vector; its mask bit (EXTI_IMR2.IM45) is never set here.
//
// NOT VERIFIED ON A BOARD: line 44's mask bit (EXTI_IMR2, SVD name C1IMR2,
// bit 12, "wake-up with interrupt mask") is set as well; whether a direct line
// needs it in Run mode is not stated in RM0461. Setting it is harmless.
const imr2Radio = 1 << 12 // IM44

var (
	radioIntr interrupt.Interrupt
	latch     Latch
	irqMode   atomic.Bool
	isrCount  atomic.Uint32
)

func init() {
	// interrupt.New needs a constant IRQ number.
	radioIntr = interrupt.New(stm32.IRQ_Radio_IRQ_Busy, radioHandler)
}

// radioHandler runs in interrupt context: remember, and mask the line until
// the driver has cleared the radio's status (see Latch).
func radioHandler(interrupt.Interrupt) {
	radioIntr.Disable()
	isrCount.Add(1)
	latch.Fire()
}

// EnableIRQ switches the driver's DIO1 (IRQ.Get) from asking the radio over
// SUBGHZSPI to reading what the interrupt handler left. Call it after the
// radio's Init (which sets the interrupt mask in the radio, SetDioIrqParams,
// and clears the status). A first Get reports one interrupt to catch anything
// raised earlier.
func EnableIRQ() {
	stm32.EXTI.C1IMR2.SetBits(imr2Radio)
	latch.Prime()
	irqMode.Store(true)
	radioIntr.Enable()
}

// DisableIRQ goes back to asking the radio.
func DisableIRQ() {
	irqMode.Store(false)
	radioIntr.Disable()
	stm32.EXTI.C1IMR2.ClearBits(imr2Radio)
}

// IRQCount is the number of times the handler has run.
func IRQCount() uint32 { return isrCount.Load() }

// takeIRQ is IRQ.Get in interrupt mode.
func takeIRQ() bool {
	fired, rearm := latch.Take()
	if rearm {
		radioIntr.Enable()
	}
	return fired
}
