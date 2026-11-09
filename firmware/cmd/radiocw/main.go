// Command radiocw sends a continuous wave from radio 0 of the NUCLEO bench
// for a spectrum analyzer: 433.5 MHz, lowest power,
// 60 s, then standby and a reset pulse so the carrier surely stops.
//
// It uses only fixed-length SX126x commands (no register access), so it
// works even when the chip's NSS does not end variable-length commands.
// It tries the crystal first; if the oscillator does not start it retries
// with a TCXO powered at 1.8 V from DIO3.
// Output on the ST-LINK serial port (USART2, 115200):
//
//	T try tcxo=<bool>
//	E <step> errors=<n> status=<byte> mode=<n>
//	C cw on|FAIL ... | C t=<s> mode=<n>
//	R done
package main

import (
	"machine"
	"time"

	"cairnmesh/firmware/hw/spihw"
)

// Radio 0 is the first module.
const (
	pinNSS   = machine.PB10
	pinReset = machine.PB11
	pinBusy  = machine.PB13

	freqHz  = 433500000
	seconds = 60
	// GetStatus chip mode (bits 6:4) of TX, and the oscillator error bit.
	modeTx       = 6
	errXoscStart = 1 << 5
)

var bus = spihw.SPI1

func waitBusy() bool {
	start := time.Now()
	for time.Since(start) < 200*time.Millisecond {
		if !pinBusy.Get() {
			return true
		}
	}
	return false
}

func cmd(w, r []byte) bool {
	if !waitBusy() {
		return false
	}
	pinNSS.Low()
	err := bus.Tx(w, r)
	pinNSS.High()
	time.Sleep(10 * time.Microsecond) // BUSY rises up to 600 ns after NSS
	return err == nil
}

func status() (st, mode byte) {
	var r [2]byte
	cmd([]byte{0xC0, 0}, r[:])
	return r[1], (r[1] >> 4) & 7
}

func deviceErrors() uint16 {
	var r [4]byte
	cmd([]byte{0x17, 0, 0, 0}, r[:])
	return uint16(r[2])<<8 | uint16(r[3])
}

func report(step string) uint16 {
	e := deviceErrors()
	st, mode := status()
	println("E", step, "errors=", e, "status=", st, "mode=", mode)
	return e
}

func reset() {
	pinReset.Low()
	time.Sleep(time.Millisecond)
	pinReset.High()
	waitBusy()
}

// start sets the chip up and turns the carrier on; it returns true when the
// chip reports TX mode.
func start(tcxo bool) bool {
	println("T try tcxo=", tcxo)
	reset()
	cmd([]byte{0x80, 0x00}, nil) // SetStandby(STDBY_RC)
	if tcxo {
		// SetDIO3AsTCXOCtrl(1.8 V, 5 ms = 320 steps of 15.625 µs), then Calibrate(all).
		cmd([]byte{0x97, 0x02, 0x00, 0x01, 0x40}, nil)
		cmd([]byte{0x89, 0x7F}, nil)
		waitBusy()
	}
	cmd([]byte{0x96, 0x00}, nil) // SetRegulatorMode(LDO): works with or without the DC-DC inductor
	cmd([]byte{0x8A, 0x01}, nil) // SetPacketType(LoRa)
	f := uint32(uint64(freqHz) << 25 / 32000000)
	cmd([]byte{0x86, byte(f >> 24), byte(f >> 16), byte(f >> 8), byte(f)}, nil) // SetRfFrequency
	cmd([]byte{0x98, 0x6B, 0x6F}, nil)                                          // CalibrateImage(430–440 MHz)
	waitBusy()
	cmd([]byte{0x95, 0x02, 0x02, 0x00, 0x01}, nil) // SetPaConfig: the +14 dBm table of the SX1262
	cmd([]byte{0x8E, 0xF7, 0x04}, nil)             // SetTxParams(-9 dBm, 200 µs ramp): lowest power
	cmd([]byte{0x9D, 0x01}, nil)                   // SetDio2AsRfSwitchCtrl
	cmd([]byte{0x07, 0x00, 0x00}, nil)             // ClearDeviceErrors
	if e := report("setup"); e&errXoscStart != 0 {
		return false
	}
	cmd([]byte{0xD1}, nil) // SetTxContinuousWave
	time.Sleep(10 * time.Millisecond)
	e := report("cw")
	_, mode := status()
	return mode == modeTx && e&errXoscStart == 0
}

func main() {
	time.Sleep(time.Second) // time for the capture to attach
	println("R start freq_hz=", freqHz, "seconds=", seconds)
	pinNSS.Configure(machine.PinConfig{Mode: machine.PinOutput})
	pinNSS.High()
	pinReset.Configure(machine.PinConfig{Mode: machine.PinOutput})
	pinReset.High()
	pinBusy.Configure(machine.PinConfig{Mode: machine.PinInput})
	bus.Configure(spihw.Config{SCK: machine.PA5, SDO: machine.PA7, SDI: machine.PA6,
		Frequency: 1000000, Mode: 0})

	on := start(false)
	if !on {
		on = start(true)
	}
	if on {
		println("C cw on")
		for t := 0; t < seconds; t += 5 {
			_, mode := status()
			println("C t=", t, "mode=", mode)
			time.Sleep(5 * time.Second)
		}
	} else {
		println("C FAIL the chip did not enter TX")
	}
	cmd([]byte{0x80, 0x00}, nil) // SetStandby
	reset()                      // a reset stops the carrier even if SPI failed
	_, mode := status()
	println("C off mode=", mode)
	println("R done")
	for {
		time.Sleep(time.Hour)
	}
}
