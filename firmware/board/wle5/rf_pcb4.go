//go:build cairn_wle5 && wle5_pcb4

package wle5

import "machine"

// PCB4 module (netlist, 2026-11-04;
// the pin numbers match DS13105 UFQFPN48: 14 = PA6, 15 = PA7, 30 = PB0-VDD_TCXO):
//
//	U2 (TSNP-6, the RF switch, BGS12WN6):
//	  pin 1 RF_TX, 2 GND, 3 RF_RX, 4 VDD = FE_VDD = PA7, 5 RF_ANT,
//	  6 CTRL = FE_CTRL = PA6 through R2 100 ohm (C8 1 nF to GND).
//	So PA7 is the switch's SUPPLY, not a control line: it must be high or the
//	switch does not work. PA6 is the only control input: "pulled high before
//	transmitting and low after"); a single input has no off
//	state, Off and Rx are the same level.
//
// BGS12WN6 datasheet Rev 2.2 checked: CTRL=0 -> RFIN-RF1 (RF_RX), CTRL=1 -> RFIN-RF2
// (RF_TX); IDD <= 120 uA so PA7 can supply it. The switch has no off state:
// CTRL low is the RX path.
// The module has no LEDs; the console is U1 pins 9/10 (PA2/PA3, USART2) on edge
// pads U4.8/U4.7; SWDIO/SWCLK on U3.3/U3.2; BOOT0 (PH3, pulled low by R1) on U4.1
// and NRST on U4.2 are exposed too, so the ROM bootloader is reachable.
const (
	pinCTRL = machine.PA6
	pinVDD  = machine.PA7
)

var leds []machine.Pin

// dcdc: the module has the SMPS inductor (L1, VLXSMPS pin 47 to VFBSMPS pins 29/45).
const dcdc = true

func setupSwitch(out machine.PinConfig) {
	pinVDD.Configure(out)
	pinVDD.High()
	pinCTRL.Configure(out)
	RFSwitch{}.Off()
}

// RFSwitch implements sx126x.Switch for PCB4.
type RFSwitch struct{}

func (RFSwitch) Tx()  { pinCTRL.High() }
func (RFSwitch) Rx()  { pinCTRL.Low() }
func (RFSwitch) Off() { pinCTRL.Low() }
