// Package sx126x drives one SX1261/2 LoRa radio over SPI as a radio.Air and
// a radio.Emitter. Pure Go: the bus and the pins are interfaces, so the host
// tests run it against a fake chip and the board passes spihw and
// machine.Pin. Several radios may share one bus (each has its own NSS).
//
// The radio sits in continuous receive whenever it does not transmit. Send
// starts a packet and returns at once; Receive, Busy and Transmitting run
// the radio's state machine, so the main loop must call one of them often
// (the chip holds one received packet: a second one arriving before the
// first was read replaces it). DIO1 carries every interrupt the driver uses
// and is read as a level; the board may also call OnDIO1 from the pin's
// EXTI handler to count edges.
//
// Commands and errata: DS_SX1261/2 V2.2, chapters 13 and 15. Send, Receive,
// Busy and Transmitting do not allocate.
package sx126x

import (
	"sync/atomic"

	"cairnmesh/firmware/proto"
	"cairnmesh/firmware/radio"
)

// Bus is an SPI master: Tx sends w and receives into r at the same time
// (r may be nil; when set it is as long as w).
type Bus interface {
	Tx(w, r []byte) error
}

// Pin is a GPIO: NSS and Reset are outputs, Busy and DIO1 inputs.
type Pin interface {
	Get() bool
	Set(high bool)
}

// Switch is an RF switch the MCU drives (the on-chip radio of the STM32WL has
// no DIO2 pin: the E77 module's switch is on PA6/PA7). The driver calls it in
// front of every command that changes what the radio does with the antenna.
type Switch interface {
	Tx()  // before a transmission (SetTx, a carrier)
	Rx()  // before receiving
	Off() // in standby: no path
}

// Commands (DS_SX1261/2, table 13-1).
const (
	opSetStandby          = 0x80
	opSetTx               = 0x83
	opSetRx               = 0x82
	opSetRegulatorMode    = 0x96
	opCalibrate           = 0x89
	opCalibrateImage      = 0x98
	opSetPaConfig         = 0x95
	opWriteRegister       = 0x0D
	opReadRegister        = 0x1D
	opWriteBuffer         = 0x0E
	opReadBuffer          = 0x1E
	opSetDioIrqParams     = 0x08
	opGetIrqStatus        = 0x12
	opClearIrqStatus      = 0x02
	opSetDio2AsRfSwitch   = 0x9D
	opSetDio3AsTcxoCtrl   = 0x97
	opSetRfFrequency      = 0x86
	opSetPacketType       = 0x8A
	opSetTxParams         = 0x8E
	opSetModulationParams = 0x8B
	opSetPacketParams     = 0x8C
	opSetBufferBase       = 0x8F
	opGetStatus           = 0xC0
	opGetRssiInst         = 0x15
	opGetRxBufferStatus   = 0x13
	opGetPacketStatus     = 0x14
	opGetDeviceErrors     = 0x17
	opClearDeviceErrors   = 0x07
	opSetTxContinuousWave = 0xD1
)

// Registers.
const (
	regSyncWord     = 0x0740
	regIQPolarity   = 0x0736 // errata 15.4: bit 2 set for standard IQ
	regTxModulation = 0x0889 // errata 15.1: bit 2 clear only at 500 kHz
	regRxGain       = 0x08AC
	regTxClamp      = 0x08D8 // errata 15.2: bits 4:1 set
	regOCP          = 0x08E7
)

// IRQ bits (table 13-29).
const (
	irqTxDone      = 1 << 0
	irqRxDone      = 1 << 1
	irqHeaderValid = 1 << 4
	irqHeaderErr   = 1 << 5
	irqCrcErr      = 1 << 6
	irqTimeout     = 1 << 9
	irqMask        = irqTxDone | irqRxDone | irqHeaderValid | irqHeaderErr | irqCrcErr | irqTimeout
)

// Device errors that make the radio unusable: calibrations, the crystal or
// TCXO not starting, the PLL not locking (table 13-85).
const errFatal = 0x7F

const (
	modeStandbyRC = 2 // GetStatus chip mode (bits 6:4)
	modeRx        = 5
	// busySpins bounds the wait for BUSY low: calibrations take up to a few
	// milliseconds, a reset about 4 ms (bench, 2026-10-26).
	busySpins = 200000
)

// DefaultSyncWord is the LoRa sync word of private networks (the chip's
// reset value).
const DefaultSyncWord = 0x1424

// rxPreamble is the preamble length set while receiving: the longest. A
// receiver set to the sender's 8 symbols lost frames with longer preambles
// (wake frames, spec 14) without any error, depending on the length: on the
// bench 12 and 200 symbols 0/5, 16–64 2–4/5; set to 65535, every length
// from 8 to 1000 symbols 5/5 (2026-10-26). Semtech: when the preamble
// length is not known, set the receiver to the longest.
const rxPreamble = 0xFFFF

// MaxPacket is the longest LoRa payload.
const MaxPacket = 255

// Config is the network's radio settings.
type Config struct {
	FrequencyHz     uint32
	SpreadingFactor uint8  // 7..12
	BandwidthHz     uint32 // 7810, 10420, 15630, 20830, 31250, 41670, 62500, 125000, 250000 or 500000
	CodingRate      uint8  // 5..8: 4/5..4/8
	PreambleSymbols uint16 // 0: 8
	PowerDBm        int8   // clamped to -9..22
	SyncWord        uint16 // 0: DefaultSyncWord
	// TCXOMillivolts: 0 for a crystal; else DIO3 powers a TCXO at 1600,
	// 1700, 1800, 2200, 2400, 2700, 3000 or 3300 mV.
	TCXOMillivolts uint16
	// DCDC: the module has the DC-DC inductor; false uses the LDO.
	DCDC bool
	// RxBoost: receive with the boosted gain (about +2 dB, +0.6 mA).
	RxBoost bool
	// NoDio2RfSwitch: do not send SetDio2AsRfSwitchCtrl. The STM32WL's radio
	// has no DIO2 pin and no such command (RM0461 table 33); set Radio.Switch.
	NoDio2RfSwitch bool
}

var bandwidths = [...]struct {
	hz   uint32
	code byte
}{
	{7810, 0x00}, {10420, 0x08}, {15630, 0x01}, {20830, 0x09}, {31250, 0x02},
	{41670, 0x0A}, {62500, 0x03}, {125000, 0x04}, {250000, 0x05}, {500000, 0x06},
}

func bandwidthCode(hz uint32) (byte, bool) {
	for _, b := range bandwidths {
		if b.hz == hz {
			return b.code, true
		}
	}
	return 0, false
}

func tcxoCode(mv uint16) (byte, bool) {
	for i, v := range [...]uint16{1600, 1700, 1800, 2200, 2400, 2700, 3000, 3300} {
		if v == mv {
			return byte(i), true
		}
	}
	return 0, false
}

// modem is one set of modulation parameters.
type modem struct {
	sf, cr byte
	bwHz   uint32
}

func (m modem) valid() bool {
	_, ok := bandwidthCode(m.bwHz)
	return ok && m.sf >= 7 && m.sf <= 12 && m.cr >= 5 && m.cr <= 8
}

// lowDataRate is the rule of the Python model (airtime.py): on when a
// symbol lasts 16 ms or more, which the chip needs at SF11 and SF12 / 125 kHz.
func (m modem) lowDataRate() bool {
	return (uint64(1)<<m.sf)*1000 >= 16*uint64(m.bwHz)
}

// Airtime is the time on air of an n-byte packet: explicit header, CRC on
// (Semtech formula, the same as cairnmesh/airtime.py, spec 11.4).
// cr is 5..8 for 4/5..4/8.
func Airtime(sf uint8, bwHz uint32, cr uint8, preamble uint16, n int) proto.Time {
	m := modem{sf: sf, cr: cr, bwHz: bwHz}
	if !m.valid() {
		return 0
	}
	de := 0
	if m.lowDataRate() {
		de = 1
	}
	bits := 8*n - 4*int(sf) + 28 + 16
	per := 4 * (int(sf) - 2*de)
	blocks := 0
	if bits > 0 {
		blocks = (bits + per - 1) / per
	}
	// Quarter symbols: preamble + 4.25 + 8 + blocks × cr.
	quarters := int64(4*int(preamble) + 17 + 32 + 4*blocks*int(cr))
	symbolNs := (int64(1) << sf) * 1000000000 / int64(bwHz)
	return proto.Time(quarters * symbolNs / 4 / 1000)
}

type state uint8

const (
	stateOff state = iota // not set up, or Init failed
	stateRx
	stateTx           // a protocol packet (Send)
	stateCarrier      // a radio.Carrier signal
	statePacketSignal // a radio.Packet signal
	stateStandby      // Standby: receiver off
)

// Stats counts what the radio did.
type Stats struct {
	Sent, TxDone, TxTimeouts          uint32
	Received, CRCErrors, HeaderErrors uint32
	// Dropped: a packet read from the chip before the previous one was
	// taken by Receive.
	Dropped uint32
	// BusRetries: commands sent again because the chip's status byte came
	// back wrong (a transfer the chip did not take; see xfer).
	BusRetries uint32
	// Faults: BUSY stayed high, the bus failed, or a command was not taken
	// after retries.
	Faults uint32
	// Resets: Recover calls.
	Resets uint32
}

// PacketInfo is the signal of the last packet Receive returned.
type PacketInfo struct {
	RSSI       int16 // dBm, averaged over the packet
	SNR        int8  // dB
	SignalRSSI int16 // dBm, the LoRa signal after despreading
}

// Pins are a radio's control lines.
type Pins struct {
	NSS, Reset, Busy, DIO1 Pin
}

// Radio is one SX1262. Set Bus and Pins, then Init. It holds packet
// buffers: keep it in a global.
type Radio struct {
	Bus   Bus
	Pins  Pins
	Stats Stats
	// Switch, when set, is driven in front of Tx, Rx and Standby commands.
	Switch Switch
	// AfterTCXO, when set, runs right after Init has told the radio the TCXO
	// voltage (SetDio3AsTcxoCtrl) and before the calibration that needs the
	// clock. The STM32WL switches PB0 to its VDDTCXO supply there and turns
	// HSE32 on (RM0461 6.2.1: the voltage is set in the radio first, then
	// HSEBYPPWR and HSEON). It returns false when the clock did not come up;
	// Init then stops at once (FailStep "tcxo") instead of sending commands
	// to a radio that holds BUSY high.
	AfterTCXO func() bool
	// PowerCap, when set, limits every power the driver sends to the chip
	// (Config.PowerDBm, Signal.PowerDBm): the board's safety ceiling, which
	// the caller of Init or Emit cannot raise. nil: only the chip's -9..22.
	PowerCap func(dbm int8) int8
	// FailStep names where the last Init failed ("" after a good one):
	// "config", "reset", "standby", "tcxo", "calibrate", "frequency",
	// "power", "modem", "registers", "irq", "errors", "rx".
	FailStep string

	cfg   Config
	net   modem // the network's modulation
	state state

	// What is set in the chip now.
	freq         uint32
	power        int8
	cur          modem
	calLo, calHi byte

	until    proto.Time // end of a carrier
	timedOut bool       // the last transmission hit the chip's timeout
	edges    atomic.Uint32

	w, r  [3 + MaxPacket]byte
	rx    [MaxPacket]byte
	rxLen int
	got   bool
	info  PacketInfo
	// hearing: a valid header came in, the packet's end (RxDone, a CRC
	// error) not yet.
	hearing bool
}

// Init resets the chip, sets the network up and starts receiving. It
// returns false when the settings are invalid or the chip does not answer
// as it should; the radio then stays off (Healthy is false).
func (d *Radio) Init(c Config) bool {
	d.state, d.got = stateOff, false
	d.FailStep = ""
	if c.PreambleSymbols == 0 {
		c.PreambleSymbols = 8
	}
	if c.SyncWord == 0 {
		c.SyncWord = DefaultSyncWord
	}
	net := modem{sf: c.SpreadingFactor, cr: c.CodingRate, bwHz: c.BandwidthHz}
	tcxo, tcxoOK := tcxoCode(c.TCXOMillivolts)
	if !net.valid() || (c.TCXOMillivolts != 0 && !tcxoOK) {
		d.FailStep = "config"
		return false
	}
	d.cfg, d.net = c, net
	faults := d.Stats.Faults

	d.Pins.NSS.Set(true)
	d.Pins.Reset.Set(false)
	d.spin(2000) // NRESET low for at least 100 µs
	d.Pins.Reset.Set(true)
	if !d.waitBusy() {
		d.FailStep = "reset"
		return false
	}
	d.cmd(opSetStandby, 0x00)
	if (d.status()>>4)&7 != modeStandbyRC {
		d.Stats.Faults++
		d.FailStep = "standby"
		return false
	}
	if c.TCXOMillivolts != 0 {
		// 5 ms to start: 320 steps of 15.625 µs. The reset calibration ran
		// without the TCXO, so its errors are cleared and it runs again.
		d.cmd(opSetDio3AsTcxoCtrl, tcxo, 0x00, 0x01, 0x40)
		if d.AfterTCXO != nil && !d.AfterTCXO() {
			d.Stats.Faults++
			d.FailStep = "tcxo"
			return false
		}
		d.cmd(opClearDeviceErrors, 0, 0)
		d.cmd(opCalibrate, 0x7F)
		if !d.waitBusy() {
			d.FailStep = "calibrate"
			return false
		}
	}
	reg := byte(0)
	if c.DCDC {
		reg = 1
	}
	d.cmd(opSetRegulatorMode, reg)
	d.cmd(opSetPacketType, 0x01) // LoRa
	if !c.NoDio2RfSwitch {
		d.cmd(opSetDio2AsRfSwitch, 0x01)
	}
	d.cmd(opSetBufferBase, 0, 0)
	d.calLo, d.calHi = 0, 0
	// A fault (BUSY stuck, the bus) ends Init at the group where it happened:
	// every later command would wait out its own BUSY timeout.
	step := func(name string) bool {
		if d.Stats.Faults == faults {
			return true
		}
		d.FailStep = name
		return false
	}
	d.setFrequency(c.FrequencyHz)
	if !step("frequency") {
		return false
	}
	d.setPower(c.PowerDBm)
	if !step("power") {
		return false
	}
	d.setModem(net)
	if !step("modem") {
		return false
	}
	d.writeReg(regSyncWord, byte(c.SyncWord>>8), byte(c.SyncWord))
	d.writeReg(regIQPolarity, d.readReg(regIQPolarity)|0x04)
	d.writeReg(regTxClamp, d.readReg(regTxClamp)|0x1E)
	if c.RxBoost {
		d.writeReg(regRxGain, 0x96)
	}
	if !step("registers") {
		return false
	}
	d.cmd(opSetDioIrqParams, irqMask>>8, irqMask&0xFF, irqMask>>8, irqMask&0xFF, 0, 0, 0, 0)
	if !step("irq") {
		return false
	}
	errs := d.deviceErrors()
	d.cmd(opClearDeviceErrors, 0, 0)
	if errs&errFatal != 0 || d.Stats.Faults != faults {
		d.Stats.Faults++
		d.FailStep = "errors"
		return false
	}
	d.startRx()
	if !step("rx") {
		return false
	}
	return true
}

// Counts returns Stats (for interfaces: Stats is a field).
func (d *Radio) Counts() Stats { return d.Stats }

// Healthy reports whether Init succeeded.
func (d *Radio) Healthy() bool { return d.state != stateOff }

// Send starts transmitting packet and returns at once; false when the
// radio is transmitting (a packet or a signal), off, or the packet is empty
// or too long.
func (d *Radio) Send(packet []byte) bool {
	return d.SendPreamble(packet, d.cfg.PreambleSymbols)
}

// SendPreamble is Send with a preamble of the given length in symbols (a
// long one reaches sleeping radios, spec 14); 0 means the network's.
func (d *Radio) SendPreamble(packet []byte, preamble uint16) bool {
	d.poll()
	if d.state != stateRx || len(packet) == 0 || len(packet) > MaxPacket {
		return false
	}
	if preamble == 0 {
		preamble = d.cfg.PreambleSymbols
	}
	faults := d.Stats.Faults
	d.cmd(opSetStandby, 0x00)
	return d.transmit(packet, preamble, stateTx, faults)
}

// TxTimedOut reports whether the last transmission ended by the chip's own
// timeout instead of TxDone.
func (d *Radio) TxTimedOut() bool { return d.timedOut }

// Receive returns the next packet received, or nil. The slice is valid
// until the next call; Packet tells its signal.
func (d *Radio) Receive() []byte {
	d.poll()
	if !d.got {
		return nil
	}
	d.got = false
	return d.rx[:d.rxLen]
}

// Packet is the signal of the last packet Receive returned.
func (d *Radio) Packet() PacketInfo { return d.info }

// PacketRSSI is radio.Levels: the last packet's RSSI.
func (d *Radio) PacketRSSI() (int16, bool) { return d.info.RSSI, true }

// Check asks the chip whether it is where the driver left it, receiving or
// in standby. False when it is elsewhere (it reset itself, after a brownout
// say, and lost its setup), does not answer, or the radio is off: Recover
// then. While a signal is on it is true: its end, or the chip's own
// timeout, tells.
func (d *Radio) Check() bool {
	d.poll()
	want := byte(modeRx)
	switch d.state {
	case stateOff:
		return false
	case stateStandby:
		want = modeStandbyRC
	case stateRx:
	default:
		return true
	}
	faults := d.Stats.Faults
	mode := (d.status() >> 4) & 7
	return d.Stats.Faults == faults && mode == want
}

// Recover resets the chip and sets it up again as it was: the network's
// settings with the last Retune, receiving. It counts in Stats.Resets.
func (d *Radio) Recover() bool {
	d.Stats.Resets++
	return d.Init(d.cfg)
}

// Standby turns the receiver off until Listen; false when the radio is not
// receiving (it transmits, or is off).
func (d *Radio) Standby() bool {
	d.poll()
	if d.state != stateRx {
		return false
	}
	d.cmd(opSetStandby, 0x00)
	d.state = stateStandby
	return true
}

// Listen turns the receiver back on after Standby.
func (d *Radio) Listen() {
	if d.state == stateStandby {
		d.startRx()
	}
}

// Receiving reports whether a packet is coming in: its header was valid,
// its end has not come yet. A transmission or a retune now would cut it.
func (d *Radio) Receiving() bool {
	d.poll()
	return d.state == stateRx && d.hearing
}

// Transmitting reports whether a packet or a signal is going out.
func (d *Radio) Transmitting() bool {
	d.poll()
	return d.state == stateTx || d.state == stateCarrier || d.state == statePacketSignal
}

// Airtime is the time on air of an n-byte packet with the network's settings.
func (d *Radio) Airtime(n int, preamble uint16) proto.Time {
	if preamble == 0 {
		preamble = d.cfg.PreambleSymbols
	}
	return Airtime(d.net.sf, d.net.bwHz, d.net.cr, preamble, n)
}

// Retune moves the network's settings to another frequency and spreading
// factor (a session channel, spec 15.2 and 15.3) and keeps receiving there;
// false while transmitting or with invalid settings.
func (d *Radio) Retune(freqHz uint32, sf uint8) bool {
	d.poll()
	net := modem{sf: sf, cr: d.net.cr, bwHz: d.net.bwHz}
	if d.state != stateRx || !net.valid() {
		return false
	}
	d.cfg.FrequencyHz, d.cfg.SpreadingFactor, d.net = freqHz, sf, net
	faults := d.Stats.Faults
	d.cmd(opSetStandby, 0x00)
	d.restore()
	d.startRx()
	return d.Stats.Faults == faults
}

// ChannelRSSI is the instantaneous RSSI in dBm while receiving.
func (d *Radio) ChannelRSSI() int16 {
	return -int16(d.query(opGetRssiInst, 1)[0]) / 2
}

// CheckBus reads the sync word register n times and returns how many reads
// differ from the value Init wrote: bit errors on the bus (long wires, too
// fast a clock). Any transmission in flight is unaffected.
func (d *Radio) CheckBus(n int) (bad int) {
	for i := 0; i < n; i++ {
		d.w[0], d.w[1], d.w[2] = opReadRegister, regSyncWord>>8, regSyncWord&0xFF
		d.w[3], d.w[4], d.w[5] = 0, 0, 0
		if !d.xfer(6) || uint16(d.r[4])<<8|uint16(d.r[5]) != d.cfg.SyncWord {
			bad++
		}
	}
	return bad
}

// OnDIO1 counts a DIO1 edge; call it from the pin's interrupt handler.
func (d *Radio) OnDIO1() { d.edges.Add(1) }

// Edges is the number of OnDIO1 calls.
func (d *Radio) Edges() uint32 { return d.edges.Load() }

// Emit starts a signal outside the protocol (radio.Emitter). The radio
// returns to the network's settings and to receiving when it ends.
func (d *Radio) Emit(s *radio.Signal, now proto.Time) bool {
	d.poll()
	if d.state != stateRx {
		return false
	}
	freq := s.FrequencyHz
	if freq == 0 {
		freq = d.cfg.FrequencyHz
	}
	switch s.Kind {
	case radio.Carrier:
		faults := d.Stats.Faults
		d.cmd(opSetStandby, 0x00)
		d.tune(freq, s.PowerDBm)
		d.cmd(opSetTxContinuousWave)
		if d.Stats.Faults != faults {
			d.cmd(opSetStandby, 0x00)
			d.restore()
			d.startRx()
			return false
		}
		d.state, d.until = stateCarrier, now+s.Duration
		return true
	case radio.Packet:
		m, pre := d.net, d.cfg.PreambleSymbols
		if s.Modem.SpreadingFactor != 0 {
			m.sf = s.Modem.SpreadingFactor
		}
		if s.Modem.BandwidthHz != 0 {
			m.bwHz = s.Modem.BandwidthHz
		}
		if s.Modem.CodingRate != 0 {
			m.cr = s.Modem.CodingRate
		}
		if s.Modem.PreambleSymbols != 0 {
			pre = s.Modem.PreambleSymbols
		}
		if !m.valid() || len(s.Payload) == 0 || len(s.Payload) > MaxPacket {
			return false
		}
		faults := d.Stats.Faults
		d.cmd(opSetStandby, 0x00)
		d.tune(freq, s.PowerDBm)
		d.setModem(m)
		return d.transmit(s.Payload, pre, statePacketSignal, faults)
	}
	return false
}

// Busy reports whether a signal still holds the radio at now (radio.Emitter).
func (d *Radio) Busy(now proto.Time) bool {
	d.poll()
	switch d.state {
	case stateCarrier:
		if now < d.until {
			return true
		}
		d.cmd(opSetStandby, 0x00)
		d.restore()
		d.startRx()
	case statePacketSignal:
		return true
	}
	return false
}

// poll handles what DIO1 announces: the end of a transmission, a packet.
func (d *Radio) poll() {
	if d.state == stateOff || !d.Pins.DIO1.Get() {
		return
	}
	irq := d.irqStatus()
	d.cmd(opClearIrqStatus, 0xFF, 0xFF)
	switch d.state {
	case stateTx, statePacketSignal:
		switch {
		case irq&irqTxDone != 0:
			d.Stats.TxDone++
			d.timedOut = false
		case irq&irqTimeout != 0:
			d.Stats.TxTimeouts++
			d.timedOut = true
		default:
			return
		}
		if d.state == statePacketSignal {
			d.restore()
		}
		d.startRx()
	case stateRx:
		d.hearing = irq&(irqRxDone|irqCrcErr|irqHeaderErr) == 0 && (d.hearing || irq&irqHeaderValid != 0)
		switch {
		case irq&irqCrcErr != 0:
			d.Stats.CRCErrors++
		case irq&irqHeaderErr != 0:
			d.Stats.HeaderErrors++
		case irq&irqRxDone != 0:
			d.readPacket()
		}
	}
}

func (d *Radio) readPacket() {
	st := d.query(opGetRxBufferStatus, 2)
	n, start := int(st[0]), st[1]
	if n == 0 {
		return
	}
	d.w[0], d.w[1], d.w[2] = opReadBuffer, start, 0
	clear(d.w[3 : 3+n])
	if !d.xfer(3 + n) {
		return
	}
	if d.got {
		d.Stats.Dropped++
	}
	d.rxLen = copy(d.rx[:], d.r[3:3+n])
	ps := d.query(opGetPacketStatus, 3)
	d.info = PacketInfo{RSSI: -int16(ps[0]) / 2, SNR: int8(ps[1]) / 4, SignalRSSI: -int16(ps[2]) / 2}
	d.got = true
	d.Stats.Received++
}

// transmit writes p, sets the packet up and starts the transmission; the
// chip is in standby with the frequency, power and modulation set. faults
// is Stats.Faults before the caller's first command: any fault since then
// abandons the transmission.
func (d *Radio) transmit(p []byte, preamble uint16, next state, faults uint32) bool {
	d.w[0], d.w[1] = opWriteBuffer, 0
	n := copy(d.w[2:], p)
	d.xfer(2 + n)
	d.packetParams(preamble, byte(n))
	d.cmd(opClearIrqStatus, 0xFF, 0xFF)
	air := Airtime(d.cur.sf, d.cur.bwHz, d.cur.cr, preamble, n)
	// The chip's own timeout, in steps of 15.625 µs: twice the airtime and
	// 100 ms, so only a stuck transmission hits it.
	steps := min((2*int64(air)+100000)*64/1000, 0xFFFFFE)
	d.cmd(opSetTx, byte(steps>>16), byte(steps>>8), byte(steps))
	if d.Stats.Faults != faults {
		d.restore()
		d.startRx()
		return false
	}
	d.state = next
	d.Stats.Sent++
	return true
}

func (d *Radio) startRx() {
	d.packetParams(rxPreamble, MaxPacket)
	d.cmd(opClearIrqStatus, 0xFF, 0xFF)
	d.cmd(opSetRx, 0xFF, 0xFF, 0xFF) // continuous
	d.state, d.hearing = stateRx, false
}

// restore puts the network's frequency, power and modulation back.
func (d *Radio) restore() {
	d.tune(d.cfg.FrequencyHz, d.cfg.PowerDBm)
	if d.cur != d.net {
		d.setModem(d.net)
	}
}

func (d *Radio) tune(freq uint32, dbm int8) {
	if freq != d.freq {
		d.setFrequency(freq)
	}
	dbm = d.capPower(dbm)
	if dbm != d.power {
		d.setPower(dbm)
	}
}

// setFrequency sets the frequency, calibrating the image rejection first
// when it lies outside the band calibrated last (4 MHz steps, ±4 MHz).
func (d *Radio) setFrequency(hz uint32) {
	mhz := hz / 1000000
	lo, hi := byte((mhz-4)/4), byte((mhz+4+3)/4)
	if d.calHi == 0 || lo < d.calLo || hi > d.calHi {
		d.cmd(opCalibrateImage, lo, hi)
		d.waitBusy()
		d.calLo, d.calHi = lo, hi
	}
	f := uint32(uint64(hz) << 25 / 32000000)
	d.cmd(opSetRfFrequency, byte(f>>24), byte(f>>16), byte(f>>8), byte(f))
	d.freq = hz
}

// setPower uses the SX1262's +22 dBm PA table for every power, which the
// datasheet allows from -9 to +22 dBm (table 13-21), and a 140 mA current
// limit.
func (d *Radio) setPower(dbm int8) {
	dbm = d.capPower(dbm)
	p := min(max(dbm, -9), 22)
	d.cmd(opSetPaConfig, 0x04, 0x07, 0x00, 0x01)
	d.writeReg(regOCP, 0x38)
	d.cmd(opSetTxParams, byte(p), 0x04) // 200 µs ramp
	d.power = dbm
}

// capPower applies PowerCap.
func (d *Radio) capPower(dbm int8) int8 {
	if d.PowerCap != nil {
		return d.PowerCap(dbm)
	}
	return dbm
}

func (d *Radio) setModem(m modem) {
	bw, _ := bandwidthCode(m.bwHz)
	ldro := byte(0)
	if m.lowDataRate() {
		ldro = 1
	}
	d.cmd(opSetModulationParams, m.sf, bw, m.cr-4, ldro)
	mod := d.readReg(regTxModulation)
	if m.bwHz == 500000 {
		mod &^= 0x04
	} else {
		mod |= 0x04
	}
	d.writeReg(regTxModulation, mod)
	d.cur = m
}

func (d *Radio) packetParams(preamble uint16, n byte) {
	// Explicit header, CRC on, standard IQ.
	d.cmd(opSetPacketParams, byte(preamble>>8), byte(preamble), 0x00, n, 0x01, 0x00)
}

func (d *Radio) status() byte {
	d.query(opGetStatus, 0)
	return d.r[1]
}

func (d *Radio) irqStatus() uint16 {
	b := d.query(opGetIrqStatus, 2)
	return uint16(b[0])<<8 | uint16(b[1])
}

func (d *Radio) deviceErrors() uint16 {
	b := d.query(opGetDeviceErrors, 2)
	return uint16(b[0])<<8 | uint16(b[1])
}

func (d *Radio) readReg(addr uint16) byte {
	d.w[0], d.w[1], d.w[2], d.w[3], d.w[4] = opReadRegister, byte(addr>>8), byte(addr), 0, 0
	d.xfer(5)
	return d.r[4]
}

func (d *Radio) writeReg(addr uint16, b ...byte) {
	d.w[0], d.w[1], d.w[2] = opWriteRegister, byte(addr>>8), byte(addr)
	n := copy(d.w[3:], b)
	d.xfer(3 + n)
}

// query sends op and n NOPs after the status byte and returns the n bytes
// read (valid until the next command).
func (d *Radio) query(op byte, n int) []byte {
	d.w[0] = op
	clear(d.w[1 : 2+n])
	d.xfer(2 + n)
	return d.r[2 : 2+n]
}

func (d *Radio) cmd(b ...byte) {
	if d.Switch != nil {
		switch b[0] {
		case opSetTx, opSetTxContinuousWave:
			d.Switch.Tx()
		case opSetRx:
			d.Switch.Rx()
		case opSetStandby:
			d.Switch.Off()
		}
	}
	n := copy(d.w[:], b)
	d.xfer(n)
}

// xfer runs one command of n bytes from w; what the chip returns lands in
// r. The chip returns its status byte while it takes a command (0xD2 in
// receive, 0xA2 in standby: bit 0 clear, chip mode 2..6). On the NUCLEO
// bench such transfers came back as the status byte rotated one bit (0xA5
// = 0xD2, 0x45 = 0xA2, repeated): the chip counted one SCK edge too many
// and did not take the command. The cause was SCK, MOSI and MISO twisted
// together: the data lines' edges, at SCK's falling edge, put a glitch on
// it; from 1 in 10^4..10^5 at rest to most transfers while the other radio
// sent +10..+22 dBm, whatever the SPI clock. Untwisted, 0 in 400000 and 0
// under a +22 dBm carrier (2026-10-26). Every command the driver sends can
// be repeated, so such a transfer is still sent again, as insurance.
func (d *Radio) xfer(n int) bool {
	for try := 0; try < 3; try++ {
		if !d.waitBusy() {
			return false
		}
		d.Pins.NSS.Set(false)
		err := d.Bus.Tx(d.w[:n], d.r[:n])
		d.Pins.NSS.Set(true)
		// BUSY rises up to 600 ns after NSS (8.3.1): do not look at it
		// before. One pin read through the interface takes about 1.2 µs at
		// 48 MHz and 14 µs at 4 MHz (bench, 2026-10-26).
		d.spin(2)
		if err != nil {
			d.Stats.Faults++
			return false
		}
		if statusOK(d.r[0]) && (n < 2 || statusOK(d.r[1])) {
			return true
		}
		d.Stats.BusRetries++
	}
	d.Stats.Faults++
	return false
}

// statusOK tells whether b can be the chip's status byte.
func statusOK(b byte) bool {
	mode := (b >> 4) & 7
	return b&1 == 0 && mode >= 2 && mode <= 6
}

func (d *Radio) waitBusy() bool {
	for i := 0; i < busySpins; i++ {
		if !d.Pins.Busy.Get() {
			return true
		}
	}
	d.Stats.Faults++
	return false
}

// spin reads BUSY n times: a delay that needs no clock.
func (d *Radio) spin(n int) {
	for i := 0; i < n; i++ {
		d.Pins.Busy.Get()
	}
}
