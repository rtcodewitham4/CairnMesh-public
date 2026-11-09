// Package adchw drives the 12-bit ADC of the STM32U083. TinyGo's machine
// package has no ADC for the U0 family; modules that watch an analog input
// (ext.Watch through board.AnalogIn) need one.
//
// One conversion at a time, software started, polled (about 15 µs at the
// longest sampling time). The ADC runs from PCLK/4 (CFGR2.CKMODE = 10,
// synchronous: no kernel clock to select), 1 MHz at 4 MHz and 12 MHz at
// 48 MHz. Sampling time 160.5 cycles for every channel, long enough for the
// internal channels (temperature sensor, VREFINT). See RM0503, "Analog-to-
// digital converter".
//
// The factory calibration values (VREFINT at 3.0 V, temperature sensor at
// 30 and 130 °C) are read from system memory; the addresses were checked on
// the board (2026-10-22: 1659, 1037, 1373).
package adchw

import (
	"device/stm32"
	"errors"
	"time"
	"unsafe"
)

// Internal channels (CHSELR bit numbers). Found on the board (2026-10-22)
// by switching CCR.TSEN and CCR.VREFEN one at a time: one lower than on
// the G0 (12, 13).
const (
	ChannelTemperature = 11
	ChannelVrefint     = 12
)

// Factory calibration in system memory.
const (
	vrefintCalAddr = 0x1FFF6EA4 // VREFINT at VDDA = 3.0 V, 30 °C
	tsCal1Addr     = 0x1FFF6E68 // temperature sensor at 30 °C, VDDA = 3.0 V
	tsCal2Addr     = 0x1FFF6E8A // at 130 °C
	calMillivolts  = 3000
)

const (
	ckmodePCLK4 = 2 // CFGR2.CKMODE
	smp160      = 7 // SMPR.SMP1: 160.5 cycles
	timeout     = 200000
)

var (
	ErrCalibration = errors.New("adc: calibration timeout")
	ErrReady       = errors.New("adc: not ready")
)

// Configure clocks the ADC, starts its regulator, calibrates it, enables it
// and turns on the internal channels.
func Configure() error {
	stm32.RCC.APBENR2.SetBits(stm32.RCC_APBENR2_ADCEN)
	_ = stm32.RCC.APBENR2.Get()
	adc := stm32.ADC
	adc.CFGR2.ReplaceBits(ckmodePCLK4, stm32.ADC_CFGR2_CKMODE_Msk>>stm32.ADC_CFGR2_CKMODE_Pos, stm32.ADC_CFGR2_CKMODE_Pos)
	adc.CR.SetBits(stm32.ADC_CR_ADVREGEN)
	time.Sleep(50 * time.Microsecond) // regulator start-up, 20 µs at most

	adc.CR.SetBits(stm32.ADC_CR_ADCAL)
	for i := 0; adc.CR.HasBits(stm32.ADC_CR_ADCAL); i++ {
		if i > timeout {
			return ErrCalibration
		}
	}
	time.Sleep(10 * time.Microsecond) // a few ADC cycles before ADEN

	adc.CFGR1.SetBits(stm32.ADC_CFGR1_OVRMOD) // a late read gets the newest value
	adc.SMPR.ReplaceBits(smp160, 7, stm32.ADC_SMPR_SMP1_Pos)
	adc.CCR.SetBits(stm32.ADC_CCR_VREFEN | stm32.ADC_CCR_TSEN)

	adc.ISR.Set(stm32.ADC_ISR_ADRDY)
	adc.CR.SetBits(stm32.ADC_CR_ADEN)
	for i := 0; !adc.ISR.HasBits(stm32.ADC_ISR_ADRDY); i++ {
		if i > timeout {
			return ErrReady
		}
	}
	time.Sleep(50 * time.Microsecond) // temperature sensor and VREFINT start-up
	return nil
}

// Read converts one channel (0..19); ok is false on a timeout. The pin of
// an external channel must be in analog mode (its reset state on this chip).
func Read(channel uint8) (value uint16, ok bool) {
	adc := stm32.ADC
	if adc.CHSELR.Get() != 1<<channel {
		adc.ISR.Set(stm32.ADC_ISR_CCRDY)
		adc.CHSELR.Set(1 << channel)
		for i := 0; !adc.ISR.HasBits(stm32.ADC_ISR_CCRDY); i++ {
			if i > timeout {
				return 0, false
			}
		}
	}
	adc.ISR.Set(stm32.ADC_ISR_EOC | stm32.ADC_ISR_EOS | stm32.ADC_ISR_OVR)
	adc.CR.SetBits(stm32.ADC_CR_ADSTART)
	for i := 0; !adc.ISR.HasBits(stm32.ADC_ISR_EOC); i++ {
		if i > timeout {
			return 0, false
		}
	}
	return uint16(adc.DR.Get() & 0xFFF), true
}

func cal(addr uintptr) int32 { return int32(*(*uint16)(unsafe.Pointer(addr))) }

// Calibration returns the factory values: VREFINT, temperature at 30 and
// at 130 °C (raw, at VDDA = 3.0 V).
func Calibration() (vrefint, ts30, ts130 int32) {
	return cal(vrefintCalAddr), cal(tsCal1Addr), cal(tsCal2Addr)
}

// SupplyMillivolts is VDDA from a VREFINT reading.
func SupplyMillivolts(vrefintRaw uint16) int32 {
	if vrefintRaw == 0 {
		return 0
	}
	return calMillivolts * cal(vrefintCalAddr) / int32(vrefintRaw)
}

// Millivolts converts a raw reading at supply VDDA (millivolts).
func Millivolts(raw uint16, supply int32) int32 { return int32(raw) * supply / 4095 }

// CentiCelsius is the chip temperature in 0.01 °C from a temperature sensor
// reading at supply VDDA (millivolts).
func CentiCelsius(tsRaw uint16, supply int32) int32 {
	_, c30, c130 := Calibration()
	if c130 == c30 {
		return 0
	}
	at3V := int32(tsRaw) * supply / calMillivolts // the reading the calibration would have seen
	return 3000 + (at3V-c30)*10000/(c130-c30)
}
