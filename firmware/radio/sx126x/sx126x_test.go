package sx126x

import (
	"bytes"
	"testing"

	"cairnmesh/firmware/proto"
	"cairnmesh/firmware/radio"
)

// chip is a fake SX1262: it decodes the commands the driver sends, keeps
// registers, the data buffer and the IRQ flags, and drives DIO1 from them.
type chip struct {
	nssLow    bool
	busyHigh  bool // BUSY stuck high
	ignoreCmd bool // the chip stays in RX whatever it is told
	failBus   bool
	mode      byte
	irq       uint16
	dio1Mask  uint16
	regs      map[uint16]byte
	buf       [256]byte
	rxLen     byte
	errors    uint16
	corrupt   int      // the next transfers come back as 0xA5 bytes, untaken
	log       [][]byte // every command, as sent
}

func newChip() *chip {
	return &chip{mode: modeStandbyRC, regs: map[uint16]byte{
		regSyncWord: 0x14, regSyncWord + 1: 0x24, regIQPolarity: 0x0D, regTxClamp: 0xC8, regTxModulation: 0x00,
	}}
}

type busError struct{}

func (busError) Error() string { return "bus" }

func (c *chip) Tx(w, r []byte) error {
	if c.failBus {
		return busError{}
	}
	if !c.nssLow {
		panic("SPI transfer with NSS high")
	}
	out := make([]byte, len(w))
	if c.corrupt > 0 {
		c.corrupt--
		for i := range out {
			out[i] = 0xA5 // seen on the bench
		}
		copy(r, out)
		return nil
	}
	c.log = append(c.log, append([]byte(nil), w...))
	for i := range out {
		out[i] = 0x80 | c.mode<<4 | 0x02 // the status the chip returns while it takes a command
	}
	switch op := w[0]; {
	case c.ignoreCmd:
		c.mode = 5
	case op == opSetStandby:
		c.mode = modeStandbyRC
	case op == opSetTx || op == opSetTxContinuousWave:
		c.mode = 6
	case op == opSetRx:
		c.mode = 5
	case op == opGetStatus:
		out[1] = c.mode<<4 | 0x02
	case op == opGetIrqStatus:
		out[2], out[3] = byte(c.irq>>8), byte(c.irq)
	case op == opClearIrqStatus:
		c.irq &^= uint16(w[1])<<8 | uint16(w[2])
	case op == opSetDioIrqParams:
		c.dio1Mask = uint16(w[3])<<8 | uint16(w[4])
	case op == opGetDeviceErrors:
		out[2], out[3] = byte(c.errors>>8), byte(c.errors)
	case op == opClearDeviceErrors:
		c.errors = 0
	case op == opWriteRegister:
		a := uint16(w[1])<<8 | uint16(w[2])
		for i, b := range w[3:] {
			c.regs[a+uint16(i)] = b
		}
	case op == opReadRegister:
		a := uint16(w[1])<<8 | uint16(w[2])
		for i := 4; i < len(w); i++ {
			out[i] = c.regs[a+uint16(i-4)]
		}
	case op == opWriteBuffer:
		copy(c.buf[w[1]:], w[2:])
	case op == opReadBuffer:
		copy(out[3:], c.buf[w[1]:])
	case op == opGetRxBufferStatus:
		out[2], out[3] = c.rxLen, 0
	case op == opGetPacketStatus:
		out[2], out[3], out[4] = 84, 0xF8, 90 // -42 dBm, -2 dB, -45 dBm
	case op == opGetRssiInst:
		out[2] = 200 // -100 dBm
	}
	if r != nil {
		copy(r, out)
	}
	return nil
}

// finishTx ends the transmission in flight as the chip does: TxDone, standby.
func (c *chip) finishTx() {
	c.irq |= irqTxDone
	c.mode = modeStandbyRC
}

// deliver puts a received packet into the buffer.
func (c *chip) deliver(p []byte, flags uint16) {
	copy(c.buf[:], p)
	c.rxLen = byte(len(p))
	c.irq |= irqRxDone | flags
}

// last returns the last command with opcode op, or nil.
func (c *chip) last(op byte) []byte {
	for i := len(c.log) - 1; i >= 0; i-- {
		if c.log[i][0] == op {
			return c.log[i]
		}
	}
	return nil
}

func (c *chip) count(op byte) int {
	n := 0
	for _, w := range c.log {
		if w[0] == op {
			n++
		}
	}
	return n
}

type pin struct {
	get func() bool
	set func(bool)
}

func (p pin) Get() bool {
	if p.get == nil {
		return false
	}
	return p.get()
}

func (p pin) Set(high bool) {
	if p.set != nil {
		p.set(high)
	}
}

func network() Config {
	return Config{FrequencyHz: 433500000, SpreadingFactor: 7, BandwidthHz: 125000, CodingRate: 5, PowerDBm: -9}
}

func newRadio(c *chip) *Radio {
	return &Radio{
		Bus: c,
		Pins: Pins{
			NSS:   pin{set: func(h bool) { c.nssLow = !h }},
			Reset: pin{},
			Busy:  pin{get: func() bool { return c.busyHigh }},
			DIO1:  pin{get: func() bool { return c.irq&c.dio1Mask != 0 }},
		},
	}
}

func started(t *testing.T) (*chip, *Radio) {
	t.Helper()
	c := newChip()
	d := newRadio(c)
	if !d.Init(network()) {
		t.Fatalf("Init failed: %+v", d.Stats)
	}
	c.log = nil
	return c, d
}

func TestInitSetsTheNetworkUp(t *testing.T) {
	c := newChip()
	d := newRadio(c)
	if !d.Init(network()) || !d.Healthy() {
		t.Fatalf("Init failed: %+v", d.Stats)
	}
	want := map[byte][]byte{
		opSetRfFrequency:      {opSetRfFrequency, 0x1B, 0x18, 0x00, 0x00}, // 433.5 MHz × 2^25 / 32 MHz
		opCalibrateImage:      {opCalibrateImage, 107, 110},
		opSetModulationParams: {opSetModulationParams, 7, 0x04, 1, 0},
		opSetPaConfig:         {opSetPaConfig, 0x04, 0x07, 0x00, 0x01},
		opSetTxParams:         {opSetTxParams, 0xF7, 0x04},
		opSetPacketParams:     {opSetPacketParams, 0xFF, 0xFF, 0, 255, 1, 0}, // receive: the longest preamble
		opSetRx:               {opSetRx, 0xFF, 0xFF, 0xFF},
		opSetDio2AsRfSwitch:   {opSetDio2AsRfSwitch, 1},
		opSetRegulatorMode:    {opSetRegulatorMode, 0},
	}
	for op, w := range want {
		if got := c.last(op); !bytes.Equal(got, w) {
			t.Errorf("command %#x: got % x, want % x", op, got, w)
		}
	}
	if c.count(opSetDio3AsTcxoCtrl) != 0 {
		t.Error("TCXO set up for a crystal")
	}
	if c.regs[regIQPolarity]&0x04 == 0 || c.regs[regTxClamp]&0x1E != 0x1E || c.regs[regTxModulation]&0x04 == 0 {
		t.Errorf("errata registers: %+v", c.regs)
	}
	if c.regs[regSyncWord] != 0x14 || c.regs[regSyncWord+1] != 0x24 {
		t.Error("sync word")
	}
	if c.mode != 5 {
		t.Errorf("chip mode %d, want RX", c.mode)
	}
}

func TestInitTCXOAndBoost(t *testing.T) {
	c := newChip()
	d := newRadio(c)
	cfg := network()
	cfg.TCXOMillivolts, cfg.RxBoost, cfg.DCDC = 1800, true, true
	if !d.Init(cfg) {
		t.Fatal("Init failed")
	}
	if got := c.last(opSetDio3AsTcxoCtrl); !bytes.Equal(got, []byte{opSetDio3AsTcxoCtrl, 2, 0, 1, 0x40}) {
		t.Errorf("TCXO: % x", got)
	}
	if c.count(opCalibrate) != 1 || c.regs[regRxGain] != 0x96 || c.last(opSetRegulatorMode)[1] != 1 {
		t.Error("calibrate, boost or DC-DC missing")
	}
}

func TestInitRefuses(t *testing.T) {
	for name, mod := range map[string]func(*Config, *chip){
		"sf6":        func(c *Config, _ *chip) { c.SpreadingFactor = 6 },
		"bandwidth":  func(c *Config, _ *chip) { c.BandwidthHz = 100000 },
		"cr":         func(c *Config, _ *chip) { c.CodingRate = 4 },
		"tcxo volts": func(c *Config, _ *chip) { c.TCXOMillivolts = 1900 },
		"busy stuck": func(_ *Config, ch *chip) { ch.busyHigh = true },
		"bus":        func(_ *Config, ch *chip) { ch.failBus = true },
		"xosc":       func(_ *Config, ch *chip) { ch.errors = 1 << 5 },
		"no standby": func(_ *Config, ch *chip) { ch.ignoreCmd = true },
	} {
		c := newChip()
		cfg := network()
		mod(&cfg, c)
		d := newRadio(c)
		if d.Init(cfg) || d.Healthy() {
			t.Errorf("%s: Init succeeded", name)
		}
		if d.Send([]byte{1}) {
			t.Errorf("%s: Send on a failed radio", name)
		}
	}
}

func TestSendThenBackToReceive(t *testing.T) {
	c, d := started(t)
	p := []byte("CAIRNMESH-BENCH-0")
	if !d.Send(p) {
		t.Fatal("Send refused")
	}
	if !bytes.Equal(c.buf[:len(p)], p) {
		t.Error("buffer")
	}
	if got := c.last(opSetPacketParams); got[4] != byte(len(p)) {
		t.Errorf("packet length % x", got)
	}
	// 51.456 ms on air: (2 × 51.456 + 100) ms × 64 / ms = 12986 steps.
	if got := c.last(opSetTx); !bytes.Equal(got, []byte{opSetTx, 0x00, 0x32, 0xBA}) {
		t.Errorf("SetTx % x", got)
	}
	if !d.Transmitting() || d.Send(p) {
		t.Fatal("second Send while transmitting")
	}
	c.finishTx()
	if d.Receive() != nil || d.Transmitting() {
		t.Fatal("still transmitting after TxDone")
	}
	if c.mode != 5 || d.Stats.Sent != 1 || d.Stats.TxDone != 1 {
		t.Errorf("mode %d stats %+v", c.mode, d.Stats)
	}
	if !d.Send(p) {
		t.Error("Send after TxDone")
	}
	if d.Send(nil) || d.Send(make([]byte, 256)) {
		t.Error("empty or oversized packet accepted")
	}
}

func TestTxTimeoutReturnsToReceive(t *testing.T) {
	c, d := started(t)
	d.Send([]byte{1, 2, 3})
	c.irq |= irqTimeout
	if d.Transmitting() || d.Stats.TxTimeouts != 1 || c.mode != 5 || !d.TxTimedOut() {
		t.Errorf("stats %+v mode %d", d.Stats, c.mode)
	}
	d.Send([]byte{1})
	c.finishTx()
	if d.Transmitting() || d.TxTimedOut() {
		t.Error("TxDone reported as a timeout")
	}
}

func TestSendPreamble(t *testing.T) {
	c, d := started(t)
	if !d.SendPreamble([]byte{1, 2}, 1000) {
		t.Fatal("refused")
	}
	if got := c.last(opSetPacketParams); got[1] != 0x03 || got[2] != 0xE8 || got[4] != 2 {
		t.Errorf("packet params % x", got)
	}
	c.finishTx()
	d.Transmitting()
	if got := c.last(opSetPacketParams); got[2] != 0xFF || got[4] != 255 {
		t.Errorf("receive not back on its preamble: % x", got)
	}
	d.SendPreamble([]byte{1}, 0)
	if got := c.last(opSetPacketParams); got[2] != 8 || got[4] != 1 {
		t.Errorf("preamble 0: % x", got)
	}
}

func TestReceive(t *testing.T) {
	c, d := started(t)
	if d.Receive() != nil {
		t.Fatal("packet from nowhere")
	}
	p := []byte{0xA5, 1, 2, 3, 4, 5}
	c.deliver(p, 0)
	if got := d.Receive(); !bytes.Equal(got, p) {
		t.Fatalf("got % x", got)
	}
	if info := d.Packet(); info != (PacketInfo{RSSI: -42, SNR: -2, SignalRSSI: -45}) {
		t.Errorf("info %+v", info)
	}
	if d.Receive() != nil {
		t.Error("the packet came twice")
	}
	c.deliver(p, irqCrcErr)
	if d.Receive() != nil || d.Stats.CRCErrors != 1 {
		t.Error("CRC error passed")
	}
	c.irq |= irqHeaderErr
	if d.Receive() != nil || d.Stats.HeaderErrors != 1 {
		t.Error("header error passed")
	}
	if d.Stats.Received != 1 || c.irq != 0 {
		t.Errorf("stats %+v irq %#x", d.Stats, c.irq)
	}
	if d.ChannelRSSI() != -100 {
		t.Error("channel RSSI")
	}
}

func TestReceiving(t *testing.T) {
	c, d := started(t)
	if d.Receiving() {
		t.Fatal("receiving from nowhere")
	}
	c.irq |= irqHeaderValid
	if !d.Receiving() || !d.Receiving() {
		t.Fatal("a valid header is a packet coming in")
	}
	c.deliver([]byte{1, 2}, 0)
	if d.Receiving() || d.Receive() == nil {
		t.Fatal("the packet ended")
	}
	c.irq |= irqHeaderValid
	d.Receiving()
	c.deliver([]byte{1}, irqCrcErr)
	if d.Receiving() {
		t.Error("a CRC error ends the packet")
	}
	c.irq |= irqHeaderValid | irqRxDone // header and end in one poll
	if d.Receiving() {
		t.Error("ended in the same poll")
	}
	d.Receive()
	c.irq |= irqHeaderValid
	d.Receiving()
	if !d.Send([]byte{1}) {
		t.Fatal("send")
	}
	c.finishTx()
	if d.Transmitting() || d.Receiving() {
		t.Error("a transmission cut the packet: back to receiving, nothing coming in")
	}
}

func TestStandby(t *testing.T) {
	c, d := started(t)
	if !d.Standby() || c.last(opSetStandby) == nil {
		t.Fatal("standby")
	}
	if d.Send([]byte{1}) || d.Standby() {
		t.Error("a radio in standby sends or stands by again")
	}
	rx := c.count(opSetRx)
	d.Listen()
	if c.count(opSetRx) != rx+1 || !d.Send([]byte{1}) {
		t.Error("not receiving after Listen")
	}
	d.Listen() // transmitting: no effect
	if !d.Transmitting() {
		t.Error("Listen cut the transmission")
	}
}

func TestCheckAndRecover(t *testing.T) {
	c, d := started(t)
	if !d.Check() {
		t.Fatal("a receiving radio fails its check")
	}
	c.mode = modeStandbyRC // the chip reset itself: standby, setup lost
	if d.Check() {
		t.Fatal("a chip out of receive passes")
	}
	if !d.Recover() || !d.Check() || d.Stats.Resets != 1 {
		t.Fatalf("recover: %+v", d.Stats)
	}
	c.corrupt = 100 // the chip does not answer
	if d.Check() {
		t.Error("a chip that does not answer passes")
	}
	c.corrupt = 0
	if !d.Standby() || !d.Check() {
		t.Error("standby is where the driver left it")
	}
	d.Listen()
	if !d.Send([]byte{1}) || !d.Check() {
		t.Error("transmitting: the check waits for the end")
	}
	var off Radio
	if off.Check() {
		t.Error("a radio never set up passes")
	}
}

func TestRecoverKeepsTheRetune(t *testing.T) {
	c, d := started(t)
	if !d.Retune(444000000, 9) {
		t.Fatal("retune")
	}
	c.mode = modeStandbyRC
	if !d.Recover() {
		t.Fatal("recover")
	}
	if d.Airtime(16, 0) != Airtime(9, 125000, 5, 8, 16) || d.freq != 444000000 {
		t.Errorf("recovered at %d Hz SF%d", d.freq, d.net.sf)
	}
}

func TestPacketReadByAnotherCallIsKept(t *testing.T) {
	c, d := started(t)
	c.deliver([]byte{7}, 0)
	d.Transmitting() // polls: the packet is read from the chip
	c.deliver([]byte{8}, 0)
	d.Transmitting()
	if got := d.Receive(); !bytes.Equal(got, []byte{8}) || d.Stats.Dropped != 1 {
		t.Errorf("got % x, stats %+v", got, d.Stats)
	}
}

func TestCarrier(t *testing.T) {
	c, d := started(t)
	s := radio.Signal{Kind: radio.Carrier, FrequencyHz: 445000000, Duration: 500 * proto.Millisecond}
	if !d.Emit(&s, 1000) {
		t.Fatal("Emit refused")
	}
	if c.count(opSetTxContinuousWave) != 1 || c.count(opCalibrateImage) != 1 {
		t.Error("carrier not on, or the far frequency not calibrated")
	}
	if !d.Busy(1000+499*proto.Millisecond) || d.Send([]byte{1}) || d.Emit(&s, 1000) {
		t.Fatal("radio free during the carrier")
	}
	if d.Busy(1000 + 500*proto.Millisecond) {
		t.Fatal("carrier did not end")
	}
	if got := c.last(opSetRfFrequency); !bytes.Equal(got, []byte{opSetRfFrequency, 0x1B, 0x18, 0, 0}) {
		t.Errorf("frequency not restored: % x", got)
	}
	if got := c.last(opSetTxParams); got[1] != 0xF7 {
		t.Errorf("power not restored: % x", got)
	}
	if c.mode != 5 || !d.Send([]byte{1}) {
		t.Error("not receiving after the carrier")
	}
}

func TestPowerCapLimitsInitAndEmit(t *testing.T) {
	c := newChip()
	d := newRadio(c)
	d.PowerCap = func(dbm int8) int8 { return min(dbm, 10) }
	cfg := network()
	cfg.PowerDBm = 22
	if !d.Init(cfg) {
		t.Fatalf("Init failed: %+v", d.Stats)
	}
	if got := c.last(opSetTxParams); got[1] != 10 {
		t.Errorf("Init sent power %d, want the cap 10", got[1])
	}
	n := c.count(opSetTxParams)
	s := radio.Signal{Kind: radio.Carrier, PowerDBm: radio.MaxPower, Duration: 100 * proto.Millisecond}
	if !d.Emit(&s, 0) {
		t.Fatal("Emit refused")
	}
	if c.count(opSetTxParams) != n {
		t.Error("the carrier at 22 dBm, capped to the power already set, sent power again")
	}
	if got := c.last(opSetTxParams); got[1] != 10 {
		t.Errorf("carrier power %d, want 10", got[1])
	}
	d.Busy(100 * proto.Millisecond)
	// A lower request below the cap passes unchanged.
	s2 := radio.Signal{Kind: radio.Packet, PowerDBm: 3, Payload: []byte("x")}
	if !d.Emit(&s2, 0) {
		t.Fatal("packet Emit refused")
	}
	if got := c.last(opSetTxParams); got[1] != 3 {
		t.Errorf("packet power %d, want 3", got[1])
	}
	// No cap: the chip's own range (the existing tests).
	if _, d2 := started(t); d2.PowerCap != nil {
		t.Error("PowerCap set by default")
	}
}

func TestPacketSignalRestoresTheModem(t *testing.T) {
	c, d := started(t)
	s := radio.Signal{Kind: radio.Packet, PowerDBm: radio.MaxPower, Payload: []byte("beacon"),
		Modem: radio.Modem{SpreadingFactor: 12, PreambleSymbols: 64}}
	if !d.Emit(&s, 0) {
		t.Fatal("Emit refused")
	}
	if got := c.last(opSetModulationParams); !bytes.Equal(got, []byte{opSetModulationParams, 12, 0x04, 1, 1}) {
		t.Errorf("modem % x (LDRO on at SF12)", got)
	}
	if got := c.last(opSetTxParams); got[1] != 22 {
		t.Errorf("power % x", got)
	}
	if got := c.last(opSetPacketParams); got[2] != 64 || got[4] != 6 {
		t.Errorf("packet % x", got)
	}
	if !d.Busy(0) {
		t.Fatal("not busy")
	}
	c.finishTx()
	if d.Busy(0) {
		t.Fatal("busy after TxDone")
	}
	if got := c.last(opSetModulationParams); got[1] != 7 || got[4] != 0 {
		t.Errorf("modem not restored: % x", got)
	}
	if got := c.last(opSetPacketParams); got[2] != 0xFF || got[4] != 255 {
		t.Errorf("rx packet params % x", got)
	}
	bad := radio.Signal{Kind: radio.Packet, Payload: []byte{1}, Modem: radio.Modem{BandwidthHz: 100000}}
	if d.Emit(&bad, 0) || d.Emit(&radio.Signal{Kind: radio.Packet}, 0) {
		t.Error("invalid signal accepted")
	}
}

func TestRetune(t *testing.T) {
	c, d := started(t)
	if !d.Retune(445000000, 9) {
		t.Fatal("Retune refused")
	}
	if got := c.last(opSetModulationParams); got[1] != 9 {
		t.Errorf("% x", got)
	}
	if c.count(opCalibrateImage) != 1 || c.mode != 5 {
		t.Error("not calibrated or not receiving")
	}
	if d.Airtime(16, 0) != Airtime(9, 125000, 5, 8, 16) || d.Airtime(16, 200) != Airtime(9, 125000, 5, 200, 16) {
		t.Error("airtime not at the new SF")
	}
	d.Send([]byte{1})
	if d.Retune(433500000, 7) {
		t.Error("Retune while transmitting")
	}
}

// The table of spec 11.4 (milliseconds, rounded).
func TestAirtimeMatchesSpec(t *testing.T) {
	for _, c := range []struct {
		n, sf int
		ms    int64
	}{
		{16, 7, 51}, {16, 8, 93}, {16, 9, 165}, {16, 10, 330}, {16, 12, 1319},
		{33, 7, 72}, {33, 12, 1810}, {64, 7, 118}, {64, 12, 2793}, {65, 10, 739},
	} {
		got := Airtime(uint8(c.sf), 125000, 5, 8, c.n)
		if ms := (int64(got) + 500) / 1000; ms != c.ms {
			t.Errorf("SF%d %d B: %d µs, want %d ms", c.sf, c.n, got, c.ms)
		}
	}
	if Airtime(6, 125000, 5, 8, 16) != 0 {
		t.Error("SF6 not refused")
	}
}

func TestCheckBus(t *testing.T) {
	c, d := started(t)
	if bad := d.CheckBus(10); bad != 0 {
		t.Errorf("%d bad reads", bad)
	}
	c.regs[regSyncWord+1] = 0x25
	if bad := d.CheckBus(10); bad != 10 {
		t.Errorf("%d bad reads, want 10", bad)
	}
}

func TestUntakenTransferIsSentAgain(t *testing.T) {
	c, d := started(t)
	c.corrupt = 2
	p := []byte{1, 2, 3}
	if !d.Send(p) || !bytes.Equal(c.buf[:3], p) {
		t.Fatal("Send failed")
	}
	if d.Stats.BusRetries != 2 || d.Stats.Faults != 0 {
		t.Errorf("stats %+v", d.Stats)
	}
	c.finishTx()
	d.Transmitting()
	c.corrupt = 3 // three times in a row: the command is lost
	if d.Send(p) {
		t.Error("Send succeeded with its first command lost")
	}
	if d.Stats.Faults != 1 {
		t.Errorf("stats %+v", d.Stats)
	}
	if bad := d.CheckBus(5); bad != 0 {
		t.Errorf("CheckBus %d after the faults", bad)
	}
}

func TestEdges(t *testing.T) {
	_, d := started(t)
	d.OnDIO1()
	d.OnDIO1()
	if d.Edges() != 2 {
		t.Error("edges")
	}
}

// recorder is an RF switch that logs its calls interleaved with the commands.
type recorder struct {
	c   *chip
	log []string
}

func (r *recorder) Tx()  { r.log = append(r.log, "tx") }
func (r *recorder) Rx()  { r.log = append(r.log, "rx") }
func (r *recorder) Off() { r.log = append(r.log, "off") }

func TestSwitchFollowsTheCommands(t *testing.T) {
	c := newChip()
	d := newRadio(c)
	rec := &recorder{c: c}
	d.Switch = rec
	cfg := network()
	cfg.NoDio2RfSwitch = true
	if !d.Init(cfg) {
		t.Fatalf("Init failed: %+v", d.Stats)
	}
	if c.count(opSetDio2AsRfSwitch) != 0 {
		t.Error("SetDio2AsRfSwitchCtrl sent although the radio has no DIO2")
	}
	if got := rec.log[len(rec.log)-1]; got != "rx" {
		t.Fatalf("after Init the switch is %q, want rx: %v", got, rec.log)
	}
	rec.log = nil
	if !d.Send([]byte{1, 2, 3}) {
		t.Fatal("Send refused")
	}
	if len(rec.log) == 0 || rec.log[len(rec.log)-1] != "tx" {
		t.Fatalf("switch while sending: %v", rec.log)
	}
}

func TestAfterTCXORunsBetweenTheVoltageAndTheCalibration(t *testing.T) {
	c := newChip()
	d := newRadio(c)
	var at int
	calls := 0
	d.AfterTCXO = func() bool { calls++; at = len(c.log); return true }
	cfg := network()
	cfg.TCXOMillivolts = 1800
	if !d.Init(cfg) {
		t.Fatalf("Init failed: %+v", d.Stats)
	}
	if calls != 1 {
		t.Fatalf("AfterTCXO called %d times", calls)
	}
	if at == 0 || c.log[at-1][0] != opSetDio3AsTcxoCtrl {
		t.Errorf("AfterTCXO ran after command %#x, want SetDio3AsTcxoCtrl", c.log[at-1][0])
	}
	d2 := newRadio(newChip())
	d2.AfterTCXO = func() bool { t.Error("AfterTCXO ran for a crystal"); return true }
	d2.Init(network())
}

func TestAfterTCXOFailureStopsInit(t *testing.T) {
	c := newChip()
	d := newRadio(c)
	d.AfterTCXO = func() bool { return false }
	cfg := network()
	cfg.TCXOMillivolts = 1800
	if d.Init(cfg) {
		t.Fatal("Init succeeded although the TCXO hook failed")
	}
	if d.FailStep != "tcxo" || d.Healthy() || d.Stats.Faults != 1 {
		t.Errorf("FailStep %q healthy %v faults %d", d.FailStep, d.Healthy(), d.Stats.Faults)
	}
	last := c.log[len(c.log)-1]
	if last[0] != opSetDio3AsTcxoCtrl {
		t.Errorf("Init sent %#x after the failed hook", last[0])
	}
	d.AfterTCXO = func() bool { return true }
	if !d.Init(cfg) || d.FailStep != "" {
		t.Errorf("a good Init after: ok %v FailStep %q", d.Healthy(), d.FailStep)
	}
}
