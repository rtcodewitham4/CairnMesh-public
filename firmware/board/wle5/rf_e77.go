//go:build cairn_wle5 && !wle5_pcb4

package wle5

import "machine"

// E77-400M22S (EBYTE user manual V1.6, 3.1 note 1): the module's RF switch is
// driven by PA6 = RF_TXEN and PA7 = RF_RXEN. TX: TXEN=1, RXEN=0. RX: TXEN=0,
// RXEN=1. Off: both 0. Both 1 is forbidden, so the pin being turned off is
// always cleared first. LEDs: PB5 link, PB4 rx, PB3 tx (PB3 is also SWO).
const (
	pinTXEN = machine.PA6
	pinRXEN = machine.PA7
)

var leds = []machine.Pin{machine.PB5, machine.PB4, machine.PB3}

// dcdc: whether the E77 has the SMPS inductor is not known; the LDO is used.
const dcdc = false

func setupSwitch(out machine.PinConfig) {
	pinTXEN.Configure(out)
	pinRXEN.Configure(out)
	RFSwitch{}.Off()
}

// RFSwitch implements sx126x.Switch for the module.
type RFSwitch struct{}

func (RFSwitch) Tx() {
	pinRXEN.Low()
	pinTXEN.High()
}

func (RFSwitch) Rx() {
	pinTXEN.Low()
	pinRXEN.High()
}

func (RFSwitch) Off() {
	pinTXEN.Low()
	pinRXEN.Low()
}
