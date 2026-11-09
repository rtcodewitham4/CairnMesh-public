// Command radioiso measures the coupling between the two radios of the
// NUCLEO dual-radio bench (step 4, the part that needs
// no third radio): one radio sends a carrier on channel 0 (433.5 MHz), the
// other measures its channel RSSI at offsets from it, before and during the
// carrier. At offset 0 the carrier's level gives the coupling (isolation =
// power − RSSI); at the other offsets the rise of the noise floor is what
// the carrier costs the receiver there (reciprocal mixing, the transmitter's
// noise). Blocking by compression, without a rise, needs a remote weak
// frame (a third radio) and is not measured here.
//
// Powers −9, 0, +10 and +22 dBm nominal (user 2026-10-26: full power as the
// reference). With about 20 dB between the antennas on the desk, +22 dBm
// puts about +2 dBm into the other radio, under its +10 dBm maximum. The
// carrier lasts carrierMs per point and is always ended.
//
// Output on the ST-LINK serial port (USART2, 115200), levels in 0.1 dBm:
//
//	R start samples=<n> order=<s> spi_hz=<n>
//	I<r> ok=<bool>
//	B<r> bad=<n> retries=<n> of <n>        the bus, nothing sending
//	M tx=<r> rx=<r> p_dbm=<n> offset_khz=<n> floor=<mean> floor_max=<max> on=<mean> on_max=<max> rise=<0.1 dB> retries=<tx> <rx> faults=<tx> <rx>
//	V tx=<r> victim=<r> p_dbm=<n> state=rx|standby|quiet bad=<n> retries=<n> faults=<n> of <n>
//	R done faults0=<n> faults1=<n>
//
// With LDFLAGS="-X main.mode=victim" it only checks the bus of the radio not
// sending while the other sends a carrier at each of victimPowers, with the victim receiving
// or in standby (receiver off), and with no carrier: whether the carrier
// upsets it through its own receiver or through its wires.
package main

import (
	"machine"
	"math"
	"time"

	"cairnmesh/firmware/board"
	"cairnmesh/firmware/hw/cyc"
	"cairnmesh/firmware/hw/spihw"
	"cairnmesh/firmware/proto"
	"cairnmesh/firmware/radio"
	"cairnmesh/firmware/radio/sx126x"
)

const (
	carrierHz = 433500000
	samples   = 64 // RSSI readings per level, 1 ms apart
	busReads  = 20000
	// victimReads: about 0.4 s at 3 MHz, well within the carrier.
	victimReads = 5000
	carrierMs   = 200
)

var network = sx126x.Config{
	FrequencyHz: carrierHz, SpreadingFactor: 7, BandwidthHz: 125000, CodingRate: 5,
	PowerDBm: -9, RxBoost: true,
}

var powers = [...]int8{-9, 0, 10, 22}

// offsetsKHz: the receiver's channel from the carrier; 10500 is session
// channel 1 (444 MHz, cmd/gateway).
var offsetsKHz = [...]int32{0, 250, 500, 1000, 2000, 5000, 10000, 10500, 15000, 20000, -10000}

// spiKHz is the SPI clock asked for (LDFLAGS="-X main.spiKHz=1000"); the
// controller makes 48 MHz / 2^n of it: 4000 gives 3 MHz, 1000 750 kHz.
var spiKHz = "4000"

// order is which radio sends first ("01" or "10"; LDFLAGS="-X main.order=10").
var order = "01"

// victimPowers: the carrier's powers in victim mode, from the lowest.
var victimPowers = [...]int8{-9, 0, 6, 10, 13, 16, 19, 22}

// mode: "sweep" (the levels) or "victim" (the bus under a carrier).
var mode = "sweep"

var t0 = time.Now()

func now() proto.Time { return proto.Time(time.Since(t0).Microseconds()) }

// level reads the channel RSSI samples times: mean (in power) and max, 0.1 dBm.
func level(d *sx126x.Radio) (mean, max int) {
	sum, top := 0.0, -1000.0
	for i := 0; i < samples; i++ {
		v := float64(d.ChannelRSSI())
		sum += math.Pow(10, v/10)
		top = math.Max(top, v)
		time.Sleep(time.Millisecond)
	}
	return int(math.Round(100 * math.Log10(sum/samples))), int(top * 10)
}

func measure(tx, rx int, p int8, offset int32) {
	dt, dr := &board.Radios[tx], &board.Radios[rx]
	rt, rr, ft, fr := dt.Stats.BusRetries, dr.Stats.BusRetries, dt.Stats.Faults, dr.Stats.Faults
	if !dr.Retune(uint32(carrierHz+offset*1000), 7) {
		println("M tx=", tx, "rx=", rx, "offset_khz=", offset, "retune FAIL")
		return
	}
	time.Sleep(5 * time.Millisecond)
	floor, floorMax := level(dr)
	sig := radio.Signal{Kind: radio.Carrier, FrequencyHz: carrierHz, PowerDBm: p, Duration: carrierMs * proto.Millisecond}
	if !dt.Emit(&sig, now()) {
		println("M tx=", tx, "rx=", rx, "carrier FAIL")
		return
	}
	time.Sleep(5 * time.Millisecond)
	on, onMax := level(dr)
	for dt.Busy(now()) {
		time.Sleep(time.Millisecond)
	}
	println("M tx=", tx, "rx=", rx, "p_dbm=", p, "offset_khz=", offset, "floor=", floor, "floor_max=", floorMax,
		"on=", on, "on_max=", onMax, "rise=", on-floor,
		"retries=", dt.Stats.BusRetries-rt, dr.Stats.BusRetries-rr, "faults=", dt.Stats.Faults-ft, dr.Stats.Faults-fr)
}

// victim checks the bus of radio v while radio tx sends a carrier at p dBm
// (none when quiet), v receiving or in standby.
func victim(tx, v int, p int8, state string) {
	dt, dv := &board.Radios[tx], &board.Radios[v]
	if state == "standby" && !dv.Standby() {
		println("V standby FAIL")
		return
	}
	if state != "quiet" {
		sig := radio.Signal{Kind: radio.Carrier, FrequencyHz: carrierHz, PowerDBm: p, Duration: 10 * proto.Second}
		if !dt.Emit(&sig, now()) {
			println("V carrier FAIL")
			dv.Listen()
			return
		}
		time.Sleep(5 * time.Millisecond)
	}
	r, f := dv.Stats.BusRetries, dv.Stats.Faults
	bad := dv.CheckBus(victimReads)
	r, f = dv.Stats.BusRetries-r, dv.Stats.Faults-f
	for dt.Busy(now() + 10*proto.Second) {
	}
	dv.Listen()
	println("V tx=", tx, "victim=", v, "p_dbm=", p, "state=", state, "bad=", bad, "retries=", r, "faults=", f, "of", victimReads)
}

func main() {
	time.Sleep(time.Second) // time for the capture to attach
	if cyc.To48MHz() {
		// The runtime set TIM16 and the UART up for 4 MHz (see README).
		machine.TIM16.Device.PSC.Set(48/4 - 1)
		cyc.SetUART2Baud(115200)
	}
	khz := uint32(0)
	for _, c := range spiKHz {
		khz = 10*khz + uint32(c-'0')
	}
	board.SetupRadios(khz * 1000)
	println("R start samples=", samples, "order=", order, "spi_hz=", cyc.Hz()>>(spihw.BaudDivisorBits(cyc.Hz(), khz*1000)+1))
	ok := true
	for i := range board.Radios {
		ok = board.Radios[i].Init(network) && ok
		println("I", i, "ok=", board.Radios[i].Healthy())
	}
	if ok {
		// The bus with nothing sending: reads of the sync word.
		for i := range board.Radios {
			d := &board.Radios[i]
			r := d.Stats.BusRetries
			bad := d.CheckBus(busReads)
			println("B", i, "bad=", bad, "retries=", d.Stats.BusRetries-r, "of", busReads)
		}
		for _, c := range order {
			tx := int(c - '0')
			if mode == "victim" {
				victim(tx, 1-tx, 0, "quiet")
				for _, p := range victimPowers {
					victim(tx, 1-tx, p, "rx")
					victim(tx, 1-tx, p, "standby")
				}
				continue
			}
			for _, p := range powers {
				for _, off := range offsetsKHz {
					measure(tx, 1-tx, p, off)
				}
			}
		}
	}
	println("R done faults0=", board.Radios[0].Stats.Faults, "faults1=", board.Radios[1].Stats.Faults)
	for {
		time.Sleep(time.Hour)
	}
}
