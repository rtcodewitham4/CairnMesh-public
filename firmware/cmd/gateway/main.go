// Command gateway is the dual-radio gateway as a thin bridge (package
// gateway, spec 6.1) on the NUCLEO bench: the PC's
// Python gateway (cairnmesh/bridge.py) talks to it over the ST-LINK
// serial port (USART2), one bridge message per SLIP + CRC16 frame.
//
// The serial port carries nothing else: no println. Messages up are queued
// and written a byte at a time when the UART can take one, so a long RX
// message never blocks the loop while the PC sends (the UART's receive
// ring holds 128 B). The gateway keeps no keys: the PC seals and opens the
// frames.
//
// The independent watchdog resets the board when the loop stops for
// watchdogMs; STATUS tells the PC when a boot followed such a reset.
// Radios that fail are reset by the bridge (package gateway, health).
//
// Build options (-ldflags "-X main.baudRate=... -X main.powerDBm=..."):
// the bench defaults are 115200 bit/s and -9 dBm (close antennas).
package main

import (
	"device/stm32"
	"machine"
	"runtime"

	"cairnmesh/firmware/board"
	"cairnmesh/firmware/gateway"
	"cairnmesh/firmware/hw/cyc"
	"cairnmesh/firmware/hw/iwdghw"
	"cairnmesh/firmware/link"
	"cairnmesh/firmware/radio/sx126x"
)

var (
	baudRate = "115200"
	powerDBm = "-9"
)

const spiHz = 4000000 // 3 MHz from 48 MHz (bus check)

// watchdogMs: far above the longest the loop blocks (a radio reset with
// BUSY stuck high, tens of ms).
const watchdogMs = 2000

// Channel 0 and the session channels (spec 15.3): at least 10 MHz apart,
// within 410–510 MHz. The frequencies are provisional until the isolation
// is measured on the bench (step 4).
const (
	channel0Hz     = 433500000
	sessionBaseHz  = 444000000 // channel 1
	sessionStepHz  = 250000
	lastChannel    = 31
	defaultSF      = 7
	defaultBandwHz = 125000
)

func frequency(ch byte) (uint32, bool) {
	switch {
	case ch == 0:
		return channel0Hz, true
	case ch <= lastChannel:
		return sessionBaseHz + uint32(ch-1)*sessionStepHz, true
	}
	return 0, false
}

var (
	uart   = machine.UART2
	dec    link.Decoder
	bridge gateway.Bridge
	radios [2]gateway.Radio

	// out is the queue of encoded bytes for the PC.
	out           [4096]byte
	outHead, outN int
	enc           [link.MaxEncoded]byte
	// dropped counts messages that did not fit in out.
	dropped uint32
)

// up queues one message for the PC; a message that does not fit is dropped
// whole (the PC's retries cover it).
func up(msg []byte) {
	n := link.SlipEncode(enc[:], msg)
	if outN+n > len(out) {
		dropped++
		return
	}
	for i := 0; i < n; i++ {
		out[(outHead+outN+i)%len(out)] = enc[i]
	}
	outN += n
}

// drain writes queued bytes while the UART can take them.
func drain() {
	for outN > 0 && stm32.USART2.ISR.HasBits(stm32.USART_ISR_TXE) {
		stm32.USART2.TDR.Set(uint32(out[outHead]))
		outHead = (outHead + 1) % len(out)
		outN--
	}
}

func main() {
	watchdogBoot := iwdghw.ResetByWatchdog()
	iwdghw.Start(watchdogMs)
	baud := uint32(atoi(baudRate))
	uart.Configure(machine.UARTConfig{TX: machine.PA2, RX: machine.PA3, BaudRate: baud})
	if cyc.To48MHz() {
		// The runtime set TIM16 and the UART up for 4 MHz (see README).
		machine.TIM16.Device.PSC.Set(48/4 - 1)
		cyc.SetUART2Baud(baud)
	}
	board.SetupRadios(spiHz)
	network := sx126x.Config{FrequencyHz: channel0Hz, SpreadingFactor: defaultSF,
		BandwidthHz: defaultBandwHz, CodingRate: 5, PowerDBm: int8(atoi(powerDBm)), RxBoost: true}
	for i := range board.Radios {
		// A radio that fails stays in the list: STATUS shows it unhealthy,
		// its TX are refused and the bridge tries to reset it.
		board.Radios[i].Init(network)
		radios[i] = &board.Radios[i]
	}
	bridge = gateway.Bridge{Radios: radios[:], Up: up, Frequency: frequency, SF: defaultSF}
	bridge.Init()
	if watchdogBoot {
		bridge.Stats.WatchdogBoot = 1
	}
	testbedStart()
	for {
		testbedPoll()
		for uart.Buffered() > 0 {
			b, _ := uart.ReadByte()
			if msg := dec.Feed(b); msg != nil {
				bridge.Handle(msg, board.Now())
			}
		}
		bridge.Poll(board.Now())
		drain()
		iwdghw.Feed()
		runtime.Gosched()
	}
}

func atoi(s string) int {
	v, sign := 0, 1
	for i, c := range s {
		if i == 0 && c == '-' {
			sign = -1
			continue
		}
		v = v*10 + int(c-'0')
	}
	return sign * v
}
