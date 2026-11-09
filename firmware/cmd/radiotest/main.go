// Command radiotest checks the SX1262 driver (radio/sx126x) on the NUCLEO
// dual-radio bench (step 3), at 48 MHz:
//
//   - the bus: each radio reads its sync word busReads times at two SPI
//     clocks (CheckBus); untaken transfers are sent again by the driver
//     (Stats.BusRetries), a read still wrong after that is a failure;
//   - the time of one query command and of a return to receive;
//   - radio 0 sends to radio 1, then radio 1 to radio 0, at the lowest
//     power; each radio's DIO1 must raise its EXTI interrupt once per
//     TxDone and twice per packet received (HeaderValid, then RxDone).
//
// Output on the ST-LINK serial port (USART2, 115200):
//
//	R start cpu_hz=<n> spi_hz=<n> packets=<n>
//	I<r> ok=<bool> faults=<n>
//	B spi_hz=<n> bad0=<n> bad1=<n> retries0=<n> retries1=<n> of <n>
//	T query_us=<avg> retune_us=<avg>
//	P <tx> -> <rx> seq=<n> lost|bad|send refused      (only for failures)
//	D <tx> -> <rx> sent=<n> ok=<n> lost=<n> bad=<n> send_us=<avg> tx_us=<avg> rssi=<min> .. <max> snr_min=<dB> ok=<bool>
//	S<r> <stats> edges=<n> want=<n> ok=<bool>
//	R done pass=<n> fail=<n>
package main

import (
	"machine"
	"time"

	"cairnmesh/firmware/board"
	"cairnmesh/firmware/hw/cyc"
	"cairnmesh/firmware/hw/spihw"
	"cairnmesh/firmware/radio/sx126x"
)

const (
	spiHz    = 4000000 // 3 MHz from 48 MHz
	packets  = 50
	busReads = 200000 // per radio and SPI clock
)

// busClocks are the SPI clocks of the bus check (6 and 3 MHz from 48 MHz);
// the rest of the test runs at spiHz.
var busClocks = [...]uint32{8000000, 4000000}

var network = sx126x.Config{
	FrequencyHz: 433500000, SpreadingFactor: 7, BandwidthHz: 125000, CodingRate: 5,
	PowerDBm: -9, // the lowest: the bench's antennas are close
}

var pass, fail int

func tally(ok bool) {
	if ok {
		pass++
	} else {
		fail++
	}
}

// actualHz is the SPI clock the controller makes of want.
func actualHz(want uint32) uint32 {
	return cyc.Hz() >> (spihw.BaudDivisorBits(cyc.Hz(), want) + 1)
}

// busCheck runs CheckBus on both radios at each of busClocks.
func busCheck() {
	for _, hz := range busClocks {
		spihw.SPI1.SetBaudBits(spihw.BaudDivisorBits(cyc.Hz(), hz))
		var bad, retries [2]uint32
		for i := range board.Radios {
			d := &board.Radios[i]
			before := d.Stats.BusRetries
			bad[i] = uint32(d.CheckBus(busReads))
			retries[i] = d.Stats.BusRetries - before
		}
		println("B spi_hz=", actualHz(hz), "bad0=", bad[0], "bad1=", bad[1],
			"retries0=", retries[0], "retries1=", retries[1], "of", busReads)
		tally(bad == [2]uint32{})
	}
	spihw.SPI1.SetBaudBits(spihw.BaudDivisorBits(cyc.Hz(), spiHz))
}

// timing reports the average time of one query command (ChannelRSSI) and
// of a return to receive through standby (Retune to the same settings).
func timing() {
	d := &board.Radios[0]
	const n = 100
	start := time.Now()
	for i := 0; i < n; i++ {
		d.ChannelRSSI()
	}
	query := time.Since(start).Microseconds() / n
	start = time.Now()
	for i := 0; i < n; i++ {
		d.Retune(network.FrequencyHz, network.SpreadingFactor)
	}
	retune := time.Since(start).Microseconds() / n
	println("T query_us=", query, "retune_us=", retune)
}

// link sends packets from radio tx to radio rx and reports.
func link(tx, rx int) {
	t, r := &board.Radios[tx], &board.Radios[rx]
	var msg [17]byte
	copy(msg[:], "CAIRNMESH-RADIO-")
	ok, lost, bad := 0, 0, 0
	var txTotal, sendTotal int64
	rssiMin, rssiMax, snrMin := int16(0), int16(-200), int8(127)
	for seq := 0; seq < packets; seq++ {
		msg[16] = byte(seq)
		start := time.Now()
		if !t.Send(msg[:]) {
			println("P", tx, "->", rx, "seq=", seq, "send refused")
			lost++
			continue
		}
		sendTotal += time.Since(start).Microseconds()
		for t.Transmitting() && time.Since(start) < time.Second {
		}
		txTotal += time.Since(start).Microseconds()
		var got []byte
		for wait := time.Now(); got == nil && time.Since(wait) < 100*time.Millisecond; {
			got = r.Receive()
		}
		switch {
		case got == nil:
			lost++
			println("P", tx, "->", rx, "seq=", seq, "lost")
		case string(got) != string(msg[:]):
			bad++
			println("P", tx, "->", rx, "seq=", seq, "bad len=", len(got))
		default:
			ok++
			p := r.Packet()
			rssiMin, rssiMax, snrMin = min(rssiMin, p.RSSI), max(rssiMax, p.RSSI), min(snrMin, p.SNR)
		}
		time.Sleep(50 * time.Millisecond)
	}
	println("D", tx, "->", rx, "sent=", packets, "ok=", ok, "lost=", lost, "bad=", bad,
		"send_us=", sendTotal/packets, "tx_us=", txTotal/packets, "rssi=", rssiMin, "..", rssiMax,
		"snr_min=", snrMin, "ok=", ok == packets)
	tally(ok == packets)
}

func main() {
	time.Sleep(time.Second) // time for the capture to attach
	if cyc.To48MHz() {
		// The runtime set TIM16 and the UART up for 4 MHz (see README).
		machine.TIM16.Device.PSC.Set(48/4 - 1)
		cyc.SetUART2Baud(115200)
	}
	println("R start cpu_hz=", cyc.Hz(), "spi_hz=", actualHz(spiHz), "packets=", packets)
	board.SetupRadios(spiHz)
	for i := range board.Radios {
		ok := board.Radios[i].Init(network)
		println("I", i, "ok=", ok, "faults=", board.Radios[i].Stats.Faults)
		tally(ok)
	}
	if fail == 0 {
		busCheck()
		timing()
		link(0, 1)
		link(1, 0)
	}
	for i := range board.Radios {
		d := &board.Radios[i]
		s := d.Stats
		// One edge per TxDone; per packet received one at its valid header
		// and one at its end (a header error has only the end).
		want := s.TxDone + s.TxTimeouts + 2*(s.Received+s.CRCErrors) + s.HeaderErrors
		ok := d.Edges() == want && s.Faults == 0
		println("S", i, "sent=", s.Sent, "txdone=", s.TxDone, "txtimeout=", s.TxTimeouts,
			"received=", s.Received, "crc=", s.CRCErrors, "hdr=", s.HeaderErrors, "dropped=", s.Dropped,
			"retries=", s.BusRetries, "faults=", s.Faults, "edges=", d.Edges(), "want=", want, "ok=", ok)
		tally(ok)
	}
	println("R done pass=", pass, "fail=", fail)
	for {
		time.Sleep(time.Hour)
	}
}
