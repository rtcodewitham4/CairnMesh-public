//go:build cairn_wle5

// Command uarthello is the smallest program for an E77 (STM32WLE5CC) on the
// bench: it proves the toolchain, the clock and the serial wiring without
// touching the radio, the RF switch or any flash page.
//
// Console: USART2, PA2 = TX, PA3 = RX (E77 base board header H2: 1 GND, 2 TX,
// 3 RX, 4 NRST), 115200 8N1, set up by the cairn-wle5 runtime before main.
// The clock is the target's own: PLL on HSI16 -> SYSCLK = PCLK1 = 48 MHz
// (patches/runtime_cairn_wle5.go.txt), so BRR = 48e6 / 115200 = 417 (115107
// baud, 0.08 % off; HSI16 itself is +-1 % at 25 degC, RM0461 6.2.2).
//
//	cairnmesh uarthello <n>    once a second, n counts from 1
//	every byte received is sent straight back (no prefix)
//
// Why not the stock stm32wle5 / lorae5 targets: their runtime turns on the
// HSE32 TCXO through PB0 and waits for HSERDY before main; on the E77 the TCXO
// is only powered after the radio has been told its voltage, so they may hang
// before the first line. cairn-wle5 runs from HSI16 and never waits for HSE.
//
// Build: make wle5-build CMD=uarthello      (build/wle5-uarthello.elf)
package main

import (
	"machine"
	"time"

	"cairnmesh/firmware/cmd/uarthello/hellotext"
)

func main() {
	var line [hellotext.MaxLen]byte
	next := time.Now()
	var count uint32
	for {
		// Echo whatever has arrived. The driver buffers 128 bytes in the
		// interrupt; draining it every pass keeps up with 115200 baud.
		for machine.Serial.Buffered() > 0 {
			if b, err := machine.Serial.ReadByte(); err == nil {
				machine.Serial.WriteByte(b)
			}
		}
		if time.Since(next) >= 0 {
			next = next.Add(time.Second)
			count++
			machine.Serial.Write(hellotext.Line(line[:0], count))
		}
		time.Sleep(time.Millisecond)
	}
}
