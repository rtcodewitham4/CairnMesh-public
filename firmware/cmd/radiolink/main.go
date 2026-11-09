// Command radiolink is the first link test of the NUCLEO dual-radio bench
// (step 3): radio 0 sends LoRa packets at the lowest
// power and radio 1 receives them. Both DIO1 lines are used: TxDone on the
// sender, RxDone on the receiver.
//
// 433.5 MHz, SF7, 125 kHz, CR 4/5, 8-symbol preamble, explicit header, CRC
// on, crystal (no TCXO), DIO2 drives the RF switch, LDO regulator. The
// sender uses the +14 dBm PA table at -9 dBm requested (about -17 dBm out).
//
// Output on the ST-LINK serial port (USART2, 115200):
//
//	R start tx=<r> rx=<r> packets=<n>
//	S<r> setup errors=<n> status=<byte> mode=<n>
//	P seq=<n> txdone_us=<us> rx=ok|crc|hdr|none irq=<hex> len=<n> match=<bool> rssi=<dBm> snr=<dB>
//	R done sent=<n> ok=<n> bad=<n> lost=<n> txdone=<n> rssi_min=<dBm> rssi_max=<dBm>
package main

import (
	"machine"
	"time"

	"cairnmesh/firmware/hw/spihw"
)

// radio is one module's control pins.
type radio struct {
	nss, reset, busy, dio1 machine.Pin
}

var radios = [2]radio{
	{nss: machine.PB10, reset: machine.PB11, busy: machine.PB13, dio1: machine.PB12},
	{nss: machine.PB14, reset: machine.PB15, busy: machine.PB2, dio1: machine.PA8},
}

const (
	txRadio = 0
	rxRadio = 1
	packets = 50
	period  = 200 * time.Millisecond
	freqHz  = 433500000

	// IRQ bits (DS_SX1261/2, table 13-29).
	irqTxDone    = 1 << 0
	irqRxDone    = 1 << 1
	irqHeaderErr = 1 << 5
	irqCrcErr    = 1 << 6
	irqTimeout   = 1 << 9
	irqMask      = irqTxDone | irqRxDone | irqHeaderErr | irqCrcErr | irqTimeout
)

var bus = spihw.SPI1

func (r *radio) waitBusy() bool {
	for i := 0; i < 100000; i++ {
		if !r.busy.Get() {
			return true
		}
	}
	return false
}

// cmd runs one command with NSS low; rd, if set, receives as many bytes as w sends.
func (r *radio) cmd(w, rd []byte) bool {
	if !r.waitBusy() {
		return false
	}
	r.nss.Low()
	err := bus.Tx(w, rd)
	r.nss.High()
	// BUSY rises up to 600 ns after NSS (DS_SX1261/2, 8.3.1).
	for i := 0; i < 20; i++ {
		r.busy.Get()
	}
	return err == nil
}

func (r *radio) status() (st, mode byte) {
	var rd [2]byte
	r.cmd([]byte{0xC0, 0}, rd[:])
	return rd[1], (rd[1] >> 4) & 7
}

func (r *radio) errors() uint16 {
	var rd [4]byte
	r.cmd([]byte{0x17, 0, 0, 0}, rd[:])
	return uint16(rd[2])<<8 | uint16(rd[3])
}

func (r *radio) irq() uint16 {
	var rd [4]byte
	r.cmd([]byte{0x12, 0, 0, 0}, rd[:])
	return uint16(rd[2])<<8 | uint16(rd[3])
}

func (r *radio) clearIrq() { r.cmd([]byte{0x02, 0xFF, 0xFF}, nil) }

// setup resets the chip and leaves it in STDBY_RC with the LoRa link set
// up; payload is the packet length.
func (r *radio) setup(payload byte) {
	r.reset.Low()
	time.Sleep(time.Millisecond)
	r.reset.High()
	r.waitBusy()
	r.cmd([]byte{0x80, 0x00}, nil) // SetStandby(STDBY_RC)
	r.cmd([]byte{0x96, 0x00}, nil) // SetRegulatorMode(LDO)
	r.cmd([]byte{0x8A, 0x01}, nil) // SetPacketType(LoRa)
	f := uint32(uint64(freqHz) << 25 / 32000000)
	r.cmd([]byte{0x86, byte(f >> 24), byte(f >> 16), byte(f >> 8), byte(f)}, nil) // SetRfFrequency
	r.cmd([]byte{0x98, 0x6B, 0x6F}, nil)                                          // CalibrateImage(430–440 MHz)
	r.waitBusy()
	r.cmd([]byte{0x95, 0x02, 0x02, 0x00, 0x01}, nil)          // SetPaConfig: the +14 dBm table
	r.cmd([]byte{0x8E, 0xF7, 0x04}, nil)                      // SetTxParams(-9 dBm, 200 µs ramp): lowest power
	r.cmd([]byte{0x9D, 0x01}, nil)                            // SetDio2AsRfSwitchCtrl
	r.cmd([]byte{0x8F, 0x00, 0x00}, nil)                      // SetBufferBaseAddress(tx 0, rx 0)
	r.cmd([]byte{0x8B, 0x07, 0x04, 0x01, 0x00}, nil)          // SetModulationParams(SF7, 125 kHz, 4/5, LDRO off)
	r.cmd([]byte{0x8C, 0, 8, 0x00, payload, 0x01, 0x00}, nil) // SetPacketParams(preamble 8, explicit, len, CRC on, IQ normal)
	// SetDioIrqParams: every IRQ in irqMask to DIO1.
	r.cmd([]byte{0x08, irqMask >> 8, irqMask & 0xFF, irqMask >> 8, irqMask & 0xFF, 0, 0, 0, 0}, nil)
	r.cmd([]byte{0x07, 0x00, 0x00}, nil) // ClearDeviceErrors
	r.clearIrq()
}

// waitDio1 waits for DIO1 high and returns the wait in µs, or -1 on timeout.
func (r *radio) waitDio1(limit time.Duration) int64 {
	start := time.Now()
	for time.Since(start) < limit {
		if r.dio1.Get() {
			return time.Since(start).Microseconds()
		}
	}
	return -1
}

func main() {
	time.Sleep(time.Second) // time for the capture to attach
	println("R start tx=", txRadio, "rx=", rxRadio, "packets=", packets)
	for i := range radios {
		r := &radios[i]
		r.nss.Configure(machine.PinConfig{Mode: machine.PinOutput})
		r.nss.High()
		r.reset.Configure(machine.PinConfig{Mode: machine.PinOutput})
		r.reset.High()
		r.busy.Configure(machine.PinConfig{Mode: machine.PinInput})
		r.dio1.Configure(machine.PinConfig{Mode: machine.PinInput})
	}
	bus.Configure(spihw.Config{SCK: machine.PA5, SDO: machine.PA7, SDI: machine.PA6,
		Frequency: 1000000, Mode: 0})

	var msg [17]byte
	copy(msg[:], "CAIRNMESH-BENCH-")
	tx, rx := &radios[txRadio], &radios[rxRadio]
	for _, n := range []int{txRadio, rxRadio} {
		r := &radios[n]
		r.setup(byte(len(msg)))
		st, mode := r.status()
		println("S", n, "setup errors=", r.errors(), "status=", st, "mode=", mode)
	}
	rx.cmd([]byte{0x82, 0xFF, 0xFF, 0xFF}, nil) // SetRx(continuous)

	sent, ok, bad, lost, txdone := 0, 0, 0, 0, 0
	rssiMin, rssiMax := 0, -200
	var w [2 + len(msg)]byte
	for seq := 0; seq < packets; seq++ {
		next := time.Now().Add(period)
		msg[16] = byte(seq)
		w[0], w[1] = 0x0E, 0x00 // WriteBuffer(offset 0)
		copy(w[2:], msg[:])
		tx.cmd(w[:], nil)
		tx.clearIrq()
		tx.cmd([]byte{0x83, 0x00, 0xFA, 0x00}, nil) // SetTx(timeout 1 s)
		sent++
		txUs := tx.waitDio1(time.Second)
		if txUs >= 0 && tx.irq()&irqTxDone != 0 {
			txdone++
		}
		tx.clearIrq()

		verdict, irq, n, match, rssi, snr := "none", uint16(0), 0, false, 0, 0
		if rx.waitDio1(100*time.Millisecond) >= 0 {
			irq = rx.irq()
			switch {
			case irq&irqCrcErr != 0:
				verdict = "crc"
				bad++
			case irq&irqHeaderErr != 0:
				verdict = "hdr"
				bad++
			case irq&irqRxDone != 0:
				verdict = "ok"
				var bs [4]byte
				rx.cmd([]byte{0x13, 0, 0, 0}, bs[:]) // GetRxBufferStatus
				n = int(bs[2])
				var rw, rd [3 + len(msg)]byte
				rw[0], rw[1] = 0x1E, bs[3] // ReadBuffer(start)
				if n <= len(msg) {
					rx.cmd(rw[:3+n], rd[:3+n])
				}
				match = n == len(msg) && [len(msg)]byte(rd[3:]) == msg
				var ps [5]byte
				rx.cmd([]byte{0x14, 0, 0, 0, 0}, ps[:]) // GetPacketStatus
				rssi, snr = -int(ps[2])/2, int(int8(ps[3]))/4
				if match {
					ok++
					rssiMin, rssiMax = min(rssiMin, rssi), max(rssiMax, rssi)
				} else {
					bad++
				}
			}
			rx.clearIrq()
		} else {
			lost++
		}
		println("P seq=", seq, "txdone_us=", txUs, "rx=", verdict, "irq=", irq, "len=", n,
			"match=", match, "rssi=", rssi, "snr=", snr)
		for time.Now().Before(next) {
		}
	}
	tx.cmd([]byte{0x80, 0x00}, nil) // SetStandby
	rx.cmd([]byte{0x80, 0x00}, nil)
	println("R done sent=", sent, "ok=", ok, "bad=", bad, "lost=", lost, "txdone=", txdone,
		"rssi_min=", rssiMin, "rssi_max=", rssiMax)
	for {
		time.Sleep(time.Hour)
	}
}
