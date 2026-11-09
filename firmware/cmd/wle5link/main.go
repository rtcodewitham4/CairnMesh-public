//go:build cairn_wle5

// Command wle5link is the two-way link test of a WLE5 node (E77-400M22S or
// PCB4) against the U083 + SX1262 bench board. It
// listens all the time (SetRx continuous, the radio's interrupt wakes the
// driver: no polling over SUBGHZSPI) and sends a test frame every few seconds.
//
// Air parameters: the same as the U083 side (cmd/radiolink, cmd/radiotest,
// cmd/gateway channel 0) and nothing else changed: 433.5 MHz, SF7, 125 kHz,
// coding rate 4/5, 8-symbol preamble, explicit header, CRC on, standard IQ,
// LoRa sync word 0x1424 (the chip's private-network default, which radiolink
// leaves untouched and the driver writes). The test frame is radiolink's:
// "CAIRNMESH-BENCH-" plus one sequence byte, 17 bytes. No new wire field.
//
// Power. Hard ceiling 10 dBm (cwpower.Max); WLE5_LINK_DBM
// above that, below -9 dBm or not a number is refused at start: the radio is
// never touched. Default 10. With the bench boards close together a lower
// value (0 or -9) is kinder to the U083 side (SX1262 input maximum +10 dBm).
// An antenna or a 50 ohm load MUST be on the antenna port, and the build must say
// so: WLE5_ANT_OK=1 (safety.AntennaConfirmed). Without it the program receives
// and prints but never sends a frame.
//
// Boards. This program names no pin. The RF switch, the LEDs and the console
// UART are the board layer's (board/wle5: rf_e77.go or rf_pcb4.go, selected by
// WLE5_BOARD; the console pins are in TinyGo's cairn-wle5 board file); the
// program only calls wle5.Setup, wle5.InitRadio, wle5.LED and println. Changing
// the pin map to the latest design means changing those files, not this one.
//
// Console (USART2, 115200 8N1), CR LF ended:
//
//	wle5link: clock <Hz>
//	wle5link: power <n> dBm | wle5link: power "<text>" refused: <why> (then it never starts the radio)
//	wle5link: period <n> s | wle5link: period "<text>" refused (then it never starts the radio)
//	wle5link: radio ready ... irq=on | wle5link: radio: <error>
//	wle5link: rx n=<len> rssi=<dBm> snr=<dB> hex=<first 16 bytes>
//	wle5link: tx ok | wle5link: tx timeout | wle5link: tx refused
//	wle5link: sum rx=<n> crc_err=<n> tx=<n>       every 10 s
//	wle5link: irq n=<handler runs>                after each sum
//
// rx counts frames the driver read from the radio; crc_err packets the radio
// received with a bad CRC; tx frames the radio reported TxDone for. LEDs
// (board layer, 0 = link toggles with each sum, 1 = rx toggles per frame,
// 2 = tx on while a frame is on air).
//
// Build: make wle5-build CMD=wle5link WLE5_ANT_OK=1 WLE5_TCXO_MV=<mV> [WLE5_LINK_DBM=<dBm>]
// [WLE5_LINK_SEC=<s>] [WLE5_BOARD=pcb4]
package main

import (
	"machine"
	"time"

	"cairnmesh/firmware/board/wle5"
	"cairnmesh/firmware/board/wle5/safety"
	"cairnmesh/firmware/cmd/wle5cw/cwpower"
	"cairnmesh/firmware/cmd/wle5link/linktext"
	"cairnmesh/firmware/radio/sx126x"
)

const (
	freqHz = 433_500_000

	// defaultTCXOmV is used when the build gives no WLE5_TCXO_MV (as wle5check).
	defaultTCXOmV = 1800

	firstTxDelay = 3 * time.Second
	sumEvery     = 10 * time.Second
	checkEvery   = 5 * time.Second
	idle         = time.Millisecond
)

// Set at link time (make WLE5_LINK_DBM= / WLE5_LINK_SEC=).
var (
	txDBM     string
	periodSec string
	antOK     string // "1": an antenna or a 50 ohm load is connected (WLE5_ANT_OK=1)
)

var line [160]byte

// emit writes one line (the text in b, without line end) to the console.
func emit(b []byte) {
	b = append(b, '\r', '\n')
	machine.Serial.Write(b)
}

func main() {
	time.Sleep(200 * time.Millisecond)
	println("wle5link: clock", machine.CPUFrequency(), "Hz")
	wle5.Setup() // RF switch idle, radio held in reset until InitRadio (RM0461 6.1.4)
	powerDB, err := cwpower.Parse(txDBM)
	if err != nil {
		println("wle5link: power \"" + txDBM + "\" refused: " + err.Error())
		hold()
	}
	powerDB = min(powerDB, cwpower.Max) // belt and braces
	println("wle5link: power", powerDB, "dBm")
	sec, ok := linktext.ParsePeriod(periodSec)
	if !ok {
		println("wle5link: period \"" + periodSec + "\" refused")
		hold()
	}
	println("wle5link: period", sec, "s")

	// Without WLE5_ANT_OK=1 the program only listens: no frame is ever sent.
	txOn := safety.AntennaConfirmed(antOK)
	if !txOn {
		println("wle5link: no antenna / 50 ohm load confirmation: receive only, nothing is sent. Connect one, then build with WLE5_ANT_OK=1")
	}
	mv, linked := wle5.TCXOMillivolts()
	if !linked {
		mv = defaultTCXOmV
	}
	cfg := sx126x.Config{
		FrequencyHz:     freqHz,
		SpreadingFactor: 7,
		BandwidthHz:     125_000,
		CodingRate:      5,
		PowerDBm:        powerDB,
		TCXOMillivolts:  mv,
	}
	if err := wle5.InitRadio(cfg); err != nil {
		println("wle5link: radio:", err.Error(), "step", wle5.Radio.FailStep, "faults", wle5.Radio.Stats.Faults)
		hold()
	}
	wle5.EnableRadioInterrupt()
	println("wle5link: radio ready 433500000 Hz SF7 BW125000 CR4/5 pre8 sync0x1424 crc on, tcxo", mv, "mV, irq=on")

	start := time.Now()
	now := func() int64 { return time.Since(start).Microseconds() }
	sched := linktext.Schedule{PeriodUs: int64(sec) * 1_000_000}
	sched.Start(now(), firstTxDelay.Microseconds())
	var (
		tr        linktext.Tracker
		frame     [linktext.FrameLen]byte
		seq       byte
		nextSum   = now() + sumEvery.Microseconds()
		nextCheck = now() + checkEvery.Microseconds()
		linkLED   bool
		rxLED     bool
	)
	for {
		radio := &wle5.Radio
		if p := radio.Receive(); p != nil {
			info := radio.Packet()
			emit(linktext.AppendRx(line[:0], len(p), info.RSSI, info.SNR, p))
			rxLED = !rxLED
			wle5.LED(1, rxLED)
		}
		if tr.Waiting() {
			switch tr.Update(radio.Stats.TxDone, radio.Stats.TxTimeouts) {
			case linktext.Done:
				wle5.LED(2, false)
				emit(linktext.AppendTxOK(line[:0]))
			case linktext.Timeout:
				wle5.LED(2, false)
				emit(linktext.AppendTxFail(line[:0], "timeout"))
			}
		}
		t := now()
		if txOn && !tr.Waiting() && sched.Due(t) && !radio.Receiving() {
			linktext.Frame(&frame, seq)
			seq++
			sched.Sent(t)
			if radio.Send(frame[:]) {
				tr.Start(radio.Stats.TxDone, radio.Stats.TxTimeouts)
				wle5.LED(2, true)
			} else {
				emit(linktext.AppendTxFail(line[:0], "refused"))
			}
		}
		if t >= nextCheck {
			nextCheck = t + checkEvery.Microseconds()
			if !tr.Waiting() && !radio.Check() {
				println("wle5link: radio check failed, recover")
				if radio.Recover() {
					println("wle5link: recovered")
				} else {
					println("wle5link: recover failed step", radio.FailStep)
				}
				tr = linktext.Tracker{}
			}
		}
		if t >= nextSum {
			nextSum = t + sumEvery.Microseconds()
			emit(linktext.AppendSum(line[:0], radio.Stats.Received, radio.Stats.CRCErrors, radio.Stats.TxDone))
			println("wle5link: irq n=", wle5.RadioInterrupts())
			linkLED = !linkLED
			wle5.LED(0, linkLED)
		}
		time.Sleep(idle)
	}
}

// hold never returns and never touches the radio.
func hold() {
	for {
		time.Sleep(time.Second)
	}
}
