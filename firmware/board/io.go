//go:build stm32u083

package board

import (
	"machine"

	"cairnmesh/firmware/ext"
	"cairnmesh/firmware/hw/adchw"
	"cairnmesh/firmware/hw/extihw"
)

// Inputs for ext.Watch (ext.Source) on this board.

// DigitalIn samples a pin: 1 when high (low with Invert, for an active-low
// input such as the NUCLEO's B1 button on PC13), else 0. Configure the pin
// as an input first.
type DigitalIn struct {
	Pin    machine.Pin
	Invert bool
}

func (d *DigitalIn) Sample() int32 {
	if d.Pin.Get() != d.Invert {
		return 1
	}
	return 0
}

var adcReady bool

// ConfigureADC starts the ADC once; the analog sources below need it.
func ConfigureADC() error {
	if adcReady {
		return nil
	}
	if err := adchw.Configure(); err != nil {
		return err
	}
	adcReady = true
	return nil
}

// AnalogIn samples an ADC channel (0..19), raw 0..4095. A conversion that
// times out repeats the last good value and counts in Errors.
type AnalogIn struct {
	Channel uint8
	Errors  uint32
	last    int32
}

func (a *AnalogIn) Sample() int32 {
	if v, ok := adchw.Read(a.Channel); ok {
		a.last = int32(v)
	} else {
		a.Errors++
	}
	return a.last
}

// Supply samples VDDA in millivolts (from VREFINT): a battery watch.
type Supply struct{ vref AnalogIn }

func (s *Supply) Sample() int32 {
	s.vref.Channel = adchw.ChannelVrefint
	return adchw.SupplyMillivolts(uint16(s.vref.Sample()))
}

// Temperature samples the chip's temperature in 0.01 °C.
type Temperature struct{ supply, ts AnalogIn }

func (t *Temperature) Sample() int32 {
	t.supply.Channel, t.ts.Channel = adchw.ChannelVrefint, adchw.ChannelTemperature
	vdda := adchw.SupplyMillivolts(uint16(t.supply.Sample()))
	return adchw.CentiCelsius(uint16(t.ts.Sample()), vdda)
}

// WatchEdges sets latch from the pin's interrupt on the edges in change
// (an ext.Latch as a Watch's Source sees every edge at once, even between
// samples). The pin must be configured as an input; one pin per line
// number (PA3 and PB3 share line 3). Call it at boot: the callback is
// allocated once.
func WatchEdges(pin machine.Pin, change extihw.Change, latch *ext.Latch) {
	extihw.SetInterrupt(pin, change, func(machine.Pin) { latch.Set() })
}
