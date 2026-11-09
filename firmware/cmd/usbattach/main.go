// usbattach brings up the USB FS device controller of the STM32U083 far
// enough to attach to a host. The host must see a new USB device (it will
// fail enumeration, because this program does not answer control transfers).
// It checks the HSI48 clock, the USB clock, the controller reset and the DP
// pull-up, then reports every 500 ms what the host does on the wire: bus
// resets (ISTR.RESET), start-of-frame packets (ISTR.SOF), errors, the frame
// number and the line states (FNR.RXDP / RXDM), so the wiring can be checked
// without the host's kernel log. Copied from stm32u083-demo (2026-10-22);
// added: RCC_CCIPR.CLK48SEL = HSI48 (its reset value 00 gives the 48 MHz
// consumers no clock at all, see hw/rnghw).
package main

import (
	"device/stm32"
	"machine"
	"time"
)

func main() {
	uart := machine.UART2
	uart.Configure(machine.UARTConfig{TX: machine.PA2, RX: machine.PA3, BaudRate: 115200})
	println("USB attach test")

	// HSI48 is the USB FS clock source.
	stm32.RCC.CRRCR.SetBits(stm32.RCC_CRRCR_HSI48ON)
	ready := false
	for i := 0; i < 2000000; i++ {
		if stm32.RCC.CRRCR.HasBits(stm32.RCC_CRRCR_HSI48RDY) {
			ready = true
			break
		}
	}
	println("HSI48 ready:", ready, "CRRCR", int(stm32.RCC.CRRCR.Get()))
	stm32.RCC.CCIPR.ReplaceBits(3, stm32.RCC_CCIPR_CLK48SEL_Msk>>stm32.RCC_CCIPR_CLK48SEL_Pos, stm32.RCC_CCIPR_CLK48SEL_Pos)
	// VDDUSB is isolated after reset: validate it (RM0503, PWR_CR2.USV).
	stm32.RCC.APBENR1.SetBits(stm32.RCC_APBENR1_PWREN)
	stm32.PWR.CR2.SetBits(stm32.PWR_CR2_USV)

	// USB clock enable and controller reset.
	stm32.RCC.APBENR1.SetBits(stm32.RCC_APBENR1_USBEN)
	stm32.RCC.APBRSTR1.SetBits(stm32.RCC_APBRSTR1_USBRST)
	stm32.RCC.APBRSTR1.ClearBits(stm32.RCC_APBRSTR1_USBRST)

	// Leave power down mode and clear the interrupt flags.
	stm32.USB.CNTR.Set(0)
	stm32.USB.ISTR.Set(0)
	stm32.USB.DADDR.Set(0)

	// Enable the internal DP pull-up to attach to the host.
	stm32.USB.SetBCDR_DPPU_DPD(1)
	println("attached. CNTR", int(stm32.USB.CNTR.Get()), "BCDR", int(stm32.USB.BCDR.Get()))

	const (
		istrERR   = 1 << 13
		istrWKUP  = 1 << 12
		istrSUSP  = 1 << 11
		istrRESET = 1 << 10
		istrSOF   = 1 << 9
	)
	resets, sofs, errs, susps, wkups := 0, 0, 0, 0, 0
	for i := 0; ; i++ {
		for j := 0; j < 5000; j++ { // 500 ms in 100 µs steps: count the flags as they come
			istr := stm32.USB.ISTR.Get()
			if istr&istrRESET != 0 {
				resets++
			}
			if istr&istrSOF != 0 {
				sofs++
			}
			if istr&istrERR != 0 {
				errs++
			}
			if istr&istrSUSP != 0 {
				susps++
			}
			if istr&istrWKUP != 0 {
				wkups++
			}
			stm32.USB.ISTR.Set(0) // writing 0 clears the flags
			time.Sleep(100 * time.Microsecond)
		}
		fnr := stm32.USB.FNR.Get()
		println("t", i, "resets", resets, "sofs", sofs, "errs", errs, "susp", susps, "wkup", wkups,
			"fn", int(fnr&0x7FF), "rxdp", int(fnr>>15&1), "rxdm", int(fnr>>14&1))
	}
}
