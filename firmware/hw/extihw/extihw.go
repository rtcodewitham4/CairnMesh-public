// Package extihw adds pin change interrupts for the STM32U083. TinyGo has no
// Pin.SetInterrupt for the U0 family (machine_stm32_exti_syscfg.go excludes
// stm32u0), and the SX126x radio signals "packet done" on its DIO1 pin.
//
// On this chip the port selection (EXTICR1..4) lives in the EXTI block, not
// in SYSCFG, and rising and falling edges have separate pending registers
// (RPR1, FPR1), like the G0. See RM0503, "Extended interrupts and events
// controller".
package extihw

import (
	"device/stm32"
	"machine"
	"runtime/interrupt"
	"runtime/volatile"
)

// Change selects the edges that raise the interrupt.
type Change uint8

const (
	Rising  Change = 1
	Falling Change = 2
	Toggle         = Rising | Falling
)

var (
	callbacks [16]func(machine.Pin)
	pins      [16]machine.Pin
	irqs      [3]interrupt.Interrupt
)

func init() {
	// interrupt.New needs constant arguments, so each IRQ has its own call.
	irqs[0] = interrupt.New(stm32.IRQ_EXTI0_1, handle)
	irqs[1] = interrupt.New(stm32.IRQ_EXTI2_3, handle)
	irqs[2] = interrupt.New(stm32.IRQ_EXTI4_15, handle)
}

func irqFor(line uint8) interrupt.Interrupt {
	switch {
	case line < 2:
		return irqs[0]
	case line < 4:
		return irqs[1]
	}
	return irqs[2]
}

// handle runs in interrupt context. Callbacks must be short and must not
// allocate.
func handle(interrupt.Interrupt) {
	pending := (stm32.EXTI.RPR1.Get() | stm32.EXTI.FPR1.Get()) & 0xFFFF
	// Clear only what is handled now; a new edge sets the bit again.
	stm32.EXTI.RPR1.Set(pending)
	stm32.EXTI.FPR1.Set(pending)
	for line := uint8(0); pending != 0; line++ {
		if pending&1 != 0 && callbacks[line] != nil {
			callbacks[line](pins[line])
		}
		pending >>= 1
	}
}

// SetInterrupt calls cb from the interrupt handler when pin changes. One pin
// per line number: PA3 and PB3 share line 3. A nil cb disables the line.
// Configure the pin as an input (or output) first.
func SetInterrupt(pin machine.Pin, change Change, cb func(machine.Pin)) {
	line := uint8(pin % 16)
	port := uint32(pin / 16) // 0 = A, 1 = B, 2 = C, 3 = D, 5 = F
	bit := uint32(1) << line
	exti := stm32.EXTI

	exti.IMR1.ClearBits(bit)
	if cb == nil {
		callbacks[line] = nil
		return
	}

	cr := [4]*volatile.Register32{&exti.EXTICR1, &exti.EXTICR2, &exti.EXTICR3, &exti.EXTICR4}[line/4]
	shift := uint32(line%4) * 8
	cr.Set(cr.Get()&^(0xFF<<shift) | port<<shift)

	if change&Rising != 0 {
		exti.RTSR1.SetBits(bit)
	} else {
		exti.RTSR1.ClearBits(bit)
	}
	if change&Falling != 0 {
		exti.FTSR1.SetBits(bit)
	} else {
		exti.FTSR1.ClearBits(bit)
	}
	exti.RPR1.Set(bit)
	exti.FPR1.Set(bit)

	pins[line], callbacks[line] = pin, cb
	exti.IMR1.SetBits(bit)
	irq := irqFor(line)
	irq.SetPriority(0x40) // above the UART (0xc0): the radio IRQ is urgent
	irq.Enable()
}

// Trigger raises the interrupt of a line by software (EXTI_SWIER1). The
// handler sees it as a rising edge. Use it to test or to measure latency.
func Trigger(line uint8) {
	stm32.EXTI.SWIER1.Set(1 << line)
}
