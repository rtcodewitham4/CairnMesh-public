// Command selftest checks on the board that the hardware AES and package
// proto reproduce the wire vectors of tests/vectors/lora_v4.json (wire
// format v4), and measures the cost of opening and sealing at 48 MHz. It
// also checks the ADC driver (hw/adchw) on the internal channels: the
// supply from VREFINT and the chip temperature must be plausible.
// Output on the ST-LINK serial port (USART2, 115200):
//
//	R start <version>
//	V <name> ok|FAIL open=<cycles> seal=<cycles>
//	A adc ok|FAIL vdda_mv=<mV> temp_c100=<0.01 °C> ...
//	R done pass=<n> fail=<n>
//
// Regenerate the table after the vector file changed: make vectors.
package main

import (
	"machine"
	"time"

	"cairnmesh/firmware/board"
	"cairnmesh/firmware/hw/adchw"
	"cairnmesh/firmware/hw/cyc"
	"cairnmesh/firmware/proto"
)

//go:generate sh -c "go run ../../tools/genvectors ../../../tests/vectors/lora_v4.json > vectors_gen.go"

type wireVector struct {
	name               string
	plain, below, wire []byte
	fcnt               uint32
}

const vectorNode = 0x01

var (
	rx, tx proto.Secure
	buf    [proto.MaxFrame]byte
)

func equal(a, b []byte) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}

func main() {
	time.Sleep(time.Second) // time for the capture to attach
	if cyc.To48MHz() {
		// The runtime set TIM16 and the UART up for 4 MHz (see README).
		machine.TIM16.Device.PSC.Set(48/4 - 1)
		cyc.SetUART2Baud(115200)
	}
	cyc.Start()
	println("R start", vectorVersion, "mhz", cyc.Hz()/1000000)

	c, ok := board.NewCipher()
	println("aes self-test", ok)
	network := proto.CCM{C: c, Key: &networkKey}
	pass, fail := 0, 0
	for i := range wireVectors {
		v := &wireVectors[i]
		n, fcnt, ok := proto.OpenNetwork(network, buf[:], v.wire)
		good := ok && fcnt == v.fcnt && equal(buf[:n], v.below)
		n = proto.SealNetwork(network, buf[:], v.below, v.fcnt, proto.Transmitter(v.wire))
		good = good && equal(buf[:n], v.wire)

		// A receiver holding node 0x01's key opens it to the plain frame.
		rx = proto.Secure{App: true}
		rx.Init(c, &networkKey, 0, nil)
		rx.Keys.Add(vectorNode, 0, &appKey)
		t := cyc.Now()
		n, why := rx.OpenFrame(buf[:], v.wire)
		open := cyc.Since(t)
		good = good && why == proto.Accepted && equal(buf[:n], v.plain)

		// Sealing the plain frame again (counter fcnt) gives the same bytes,
		// except a REPLY, whose application layer hangs on an earlier exchange.
		tx = proto.Secure{App: true}
		tx.Init(c, &networkKey, v.fcnt-1, nil)
		tx.Keys.Add(vectorNode, 0, &appKey)
		t = cyc.Now()
		n, ok = tx.SealFrame(buf[:], v.plain)
		seal := cyc.Since(t)
		if v.plain[0]&proto.KindMask != proto.KindReply {
			good = good && ok && equal(buf[:n], v.wire)
		}

		if good {
			pass++
			println("V", v.name, "ok open=", open, "seal=", seal)
		} else {
			fail++
			println("V", v.name, "FAIL")
		}
	}

	rx = proto.Secure{App: true}
	rx.Init(c, &networkKey, 0, nil)
	rx.Keys.Add(vectorNode, 0, &appKey)
	if _, why := rx.OpenFrame(buf[:], appReplay); why == proto.AppFailed {
		pass++
		println("V app_replay ok")
	} else {
		fail++
		println("V app_replay FAIL", why)
	}
	if checkADC() {
		pass++
	} else {
		fail++
	}
	println("R done pass=", pass, "fail=", fail, "aes_failures=", c.Failures)
	for {
		time.Sleep(time.Hour)
	}
}

// checkADC reads VREFINT and the temperature sensor: VDDA must come out
// between 3.0 and 3.6 V (the NUCLEO runs at 3.3 V) and the chip between 10
// and 60 °C.
func checkADC() bool {
	if err := adchw.Configure(); err != nil {
		println("A adc FAIL", err.Error())
		return false
	}
	t := cyc.Now()
	vref, ok1 := adchw.Read(adchw.ChannelVrefint)
	conv := cyc.Since(t)
	ts, ok2 := adchw.Read(adchw.ChannelTemperature)
	vdda := adchw.SupplyMillivolts(vref)
	temp := adchw.CentiCelsius(ts, vdda)
	cal, c30, c130 := adchw.Calibration()
	good := ok1 && ok2 && vdda > 3000 && vdda < 3600 && temp > 1000 && temp < 6000
	verdict := "ok"
	if !good {
		verdict = "FAIL"
	}
	println("A adc", verdict, "vdda_mv=", vdda, "temp_c100=", temp, "vrefint=", vref, "ts=", ts,
		"cal=", cal, c30, c130, "cycles=", conv)
	return good
}
