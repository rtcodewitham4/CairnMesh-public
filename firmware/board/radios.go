//go:build stm32u083

package board

import (
	"machine"

	"cairnmesh/firmware/hw/cyc"
	"cairnmesh/firmware/hw/extihw"
	"cairnmesh/firmware/hw/spihw"
	"cairnmesh/firmware/radio/sx126x"
)

// radioPins are the two SX1262 of the NUCLEO bench;
// the gateway board keeps them. The DIO1 lines are on
// different EXTI lines (12 and 8). SPI1's SCK is PA5, the NUCLEO's LED:
// a program with radios cannot use the LED.
var radioPins = [2]struct{ nss, reset, busy, dio1 machine.Pin }{
	{machine.PB10, machine.PB11, machine.PB13, machine.PB12},
	{machine.PB14, machine.PB15, machine.PB2, machine.PA8},
}

// Radios are the two SX1262; SetupRadios wires them, then Init each.
var Radios [2]sx126x.Radio

// SetupRadios drives both NSS high (the chips share SPI1), sets SPI1 up at
// spiHz at most (the SX1262 takes up to 16 MHz) and counts DIO1 rising
// edges on each radio (sx126x.Radio.Edges). Call it after any clock switch
// (cyc.To48MHz): the divisor follows cyc.Hz, not machine.CPUFrequency,
// which stays at 4 MHz.
func SetupRadios(spiHz uint32) {
	out := machine.PinConfig{Mode: machine.PinOutput}
	in := machine.PinConfig{Mode: machine.PinInput}
	for i, p := range radioPins {
		p.nss.Configure(out)
		p.nss.High()
		p.reset.Configure(out)
		p.reset.High()
		p.busy.Configure(in)
		p.dio1.Configure(in)
		Radios[i].Bus = spihw.SPI1
		Radios[i].Pins = sx126x.Pins{NSS: p.nss, Reset: p.reset, Busy: p.busy, DIO1: p.dio1}
	}
	spihw.SPI1.Configure(spihw.Config{SCK: machine.PA5, SDO: machine.PA7, SDI: machine.PA6,
		Frequency: spiHz, Mode: 0})
	spihw.SPI1.SetBaudBits(spihw.BaudDivisorBits(cyc.Hz(), spiHz))
	extihw.SetInterrupt(radioPins[0].dio1, extihw.Rising, dio1Radio0)
	extihw.SetInterrupt(radioPins[1].dio1, extihw.Rising, dio1Radio1)
}

func dio1Radio0(machine.Pin) { Radios[0].OnDIO1() }
func dio1Radio1(machine.Pin) { Radios[1].OnDIO1() }
