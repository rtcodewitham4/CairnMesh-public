//go:build stm32u083

package board

import (
	"device/arm"
	"device/stm32"
	"runtime/interrupt"
	"unsafe"

	"cairnmesh/firmware/hw/cyc"
	"cairnmesh/firmware/proto"
)

// ReadProtection is the flash read protection level (RM0503 3.5.1): 0
// (RDP byte 0xAA, none), 1 (any other value) or 2 (0xCC, no debug, no
// bootloader). Deployed chips must be at level 1 at least.
func ReadProtection() int {
	switch stm32.FLASH.OPTR.Get() & stm32.FLASH_OPTR_RDP_Msk {
	case 0xAA:
		return 0
	case 0xCC:
		return 2
	}
	return 1
}

// ResetIfStartedByBootloader resets the chip when the firmware was started
// by the bootloader's Go command (after a DFU download) rather than by a
// reset: Go jumps in with the bootloader's clocks and peripherals as they
// were (AN2606: they are not reset), the system clock on the PLL, the USB
// on, which the firmware's clock setup does not expect (on the bench the
// node came up deaf). A system reset starts it cleanly: BOOT0 is low and
// the flash is not empty. Call it first in main.
func ResetIfStartedByBootloader() {
	rcc := stm32.RCC
	if rcc.CFGR.Get()&stm32.RCC_CFGR_SWS_Msk != 0 || rcc.APBENR1.HasBits(stm32.RCC_APBENR1_USBEN) {
		arm.SystemReset()
	}
}

// systemMemory is where the STM32U083's system bootloader lives (RM0503
// memory map); its vector table starts there.
const systemMemory = 0x1FFF0000

// EnterBootloader detaches from USB and jumps to the system bootloader,
// which enumerates as a USB DFU device (AN2606 section 91, bootloader V13,
// PA11 / PA12). It never returns: the bootloader runs until a reset (or
// its Go command). AN2606 4.x: peripheral clocks off, the clock back to its
// reset state, interrupts off and cleared, and, on a USB part, interrupts
// enabled again before the jump. At read protection level 1 the bootloader
// cannot read or write the flash; at level 2 it is disabled.
func EnterBootloader() {
	stm32.USB.SetBCDR_DPPU_DPD(0) // the host sees the console go
	end := Now() + 100*proto.Millisecond
	for Now() < end {
	}

	mask := interrupt.Disable()
	arm.SYST.SYST_CSR.Set(0)
	for i := range arm.NVIC.ICER {
		arm.NVIC.ICER[i].Set(0xFFFFFFFF)
		arm.NVIC.ICPR[i].Set(0xFFFFFFFF)
	}
	cyc.To4MHz()
	rcc := stm32.RCC
	// Reset the peripherals the firmware used (USB, timers, UARTs, GPIO...),
	// then gate their clocks. The AHB (flash interface) stays as it is.
	rcc.APBRSTR1.Set(0xFFFFFFFF)
	rcc.APBRSTR1.Set(0)
	rcc.APBRSTR2.Set(0xFFFFFFFF)
	rcc.APBRSTR2.Set(0)
	rcc.IOPRSTR.Set(0xFFFFFFFF)
	rcc.IOPRSTR.Set(0)
	rcc.APBENR1.Set(0)
	rcc.APBENR2.Set(0)
	rcc.IOPENR.Set(0)
	rcc.AHBENR.Set(stm32.RCC_AHBENR_FLASHEN)
	// EXTI is not among the reset peripherals: back to its reset state by
	// hand. Left configured (the B1 button on line 13), the bootloader sat
	// in its EXTI4_15 handler forever and never enumerated (bench).
	exti := stm32.EXTI
	exti.IMR1.Set(0xFFF80000)
	exti.EMR1.Set(0)
	exti.RTSR1.Set(0)
	exti.FTSR1.Set(0)
	exti.RPR1.Set(0xFFFFFFFF)
	exti.FPR1.Set(0xFFFFFFFF)
	exti.EXTICR1.Set(0)
	exti.EXTICR2.Set(0)
	exti.EXTICR3.Set(0)
	exti.EXTICR4.Set(0)

	arm.SCB.VTOR.Set(systemMemory)
	interrupt.Restore(mask)
	sp := *(*uint32)(unsafe.Pointer(uintptr(systemMemory)))
	pc := *(*uint32)(unsafe.Pointer(uintptr(systemMemory + 4)))
	// TinyGo runs goroutines on the process stack (CONTROL.SPSEL = 1): the
	// bootloader expects the main stack, so CONTROL goes back to 0 with MSP
	// set to its stack (seen on the bench: without it the bootloader ran on
	// the goroutine's stack and never enumerated).
	arm.AsmFull("msr msp, {sp}\n\tmsr control, {zero}\n\tisb\n\tbx {pc}",
		map[string]interface{}{"sp": sp, "zero": uint32(0), "pc": pc})
	for {
	}
}
