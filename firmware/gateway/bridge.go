// Package gateway is the dual-radio gateway as a thin bridge (spec 6.1,
// user 2026-10-26): the PC's Python gateway (cairnmesh/gateway.py)
// keeps sessions, probes and floods; the board only puts frames on its
// radios, hands up what they receive, and arbitrates the two radios
// (spec 15.3):
//
//  1. never both transmit: requests queue and go out first come, first
//     served;
//  2. while one radio waits for an answer (the window the PC gave with the
//     frame, from its end), the other holds back frames not marked urgent;
//  3. every transmission is reported with its airtime (TX_DONE): the PC
//     counts the other radio as blinded meanwhile;
//  4. while a radio receives a packet (valid header, no end yet), its TX and
//     TUNE wait for the end: they would cut it (user 2026-10-26). BUSY
//     tells the PC, whose listen-before-talk (spec 11.2) needs it.
//
// Health: every CheckEvery a radio not sending nor receiving is asked
// whether it is where it was left (Check); a transmission that neither ends
// nor hits the chip's own timeout by txMargin past it ends as TX_DONE
// timeout. Either way the radio is reset and set up again as it was
// (Recover); while that fails it is tried again, RetryFirst after the first
// failure, doubling to RetryMax.
//
// Messages, one per serial-hop frame (spec 6, SLIP + CRC16), numbers big-endian:
//
//	PC → board   TX       0x01 radio flags preamble:2 answer_ms:2 frame…   flags bit 0: urgent; preamble 0: the network's
//	             TUNE     0x02 radio channel sf
//	             PING     0x03
//	board → PC   RX       0x81 radio rssi:2 (signed dBm) snr (signed dB) frame…
//	             TX_DONE  0x82 radio result queued_ms:2 air_ms:2           one per TX, in the order they end
//	             STATUS   0x83 version radios radio_bytes {healthy channel sf queued crc:2 header:2 retries:2 faults:2 resets:2}×radios counters counters:4×counters
//	             BUSY     0x84 radio receiving                            on each change, 1: a packet is coming in
//
// To grow (user 2026-10-26: keep it open to new features): fields are only
// appended, readers ignore bytes past the ones they know and message types
// they do not know; STATUS gives its record and counter counts; TX flags
// bits 1..7 are 0 until given a meaning. A message whose last field is a
// frame (TX, RX) grows by a new type.
//
// Pure Go: the radios are an interface (*sx126x.Radio fits); hot paths do
// not allocate.
package gateway

import (
	"cairnmesh/firmware/link"
	"cairnmesh/firmware/proto"
	"cairnmesh/firmware/radio/sx126x"
)

// Message types.
const (
	MsgTx     = 0x01
	MsgTune   = 0x02
	MsgPing   = 0x03
	MsgRx     = 0x81
	MsgTxDone = 0x82
	MsgStatus = 0x83
	MsgBusy   = 0x84
)

// Version is the bridge format's version, sent in STATUS.
const Version = 1

// FlagUrgent marks a TX that rule 2 must not hold back (session commands,
// retransmissions).
const FlagUrgent = 0x01

// TX_DONE results.
const (
	TxOK        = 0 // sent
	TxTimeout   = 1 // the chip's own timeout ended it
	TxRefused   = 2 // bad radio number or frame, or the radio refused it
	TxQueueFull = 3
)

const (
	MaxRadios = 2
	// QueueLen is how many TX and TUNE requests wait at most.
	QueueLen = 8
	txHeader = 7
	rxHeader = 5
	// radioStatus is the bytes per radio in STATUS.
	radioStatus = 14
)

// Health timing.
const (
	CheckEvery = proto.Second
	RetryFirst = proto.Second
	RetryMax   = 32 * proto.Second
	// txMargin: past the chip's own timeout (twice the airtime and 100 ms)
	// a transmission is stuck.
	txMargin = proto.Second
)

// Radio is what the bridge needs of a radio; *sx126x.Radio has it.
type Radio interface {
	SendPreamble(packet []byte, preamble uint16) bool
	Receive() []byte
	Packet() sx126x.PacketInfo
	Transmitting() bool
	TxTimedOut() bool
	Retune(freqHz uint32, sf uint8) bool
	Receiving() bool
	Airtime(n int, preamble uint16) proto.Time
	Check() bool
	Recover() bool
	Healthy() bool
	Counts() sx126x.Stats
}

// Stats counts what the bridge did.
type Stats struct {
	Received   uint32 // RX sent up
	Sent       uint32 // TX that went out
	Deferred   uint32 // TX held back by rule 2 (counted once each)
	Refused    uint32 // TX_DONE TxRefused
	QueueFull  uint32 // TX_DONE TxQueueFull, TUNE dropped
	Malformed  uint32 // PC messages that did not parse
	TuneFailed uint32 // TUNE with a bad channel or SF, or refused by the radio
	Held       uint32 // TX and TUNE held back by rule 4 (counted once each)
	TxStuck    uint32 // transmissions ended by txMargin, not by the radio
	// WatchdogBoot: this boot followed a watchdog reset (0 or 1; set by
	// the program).
	WatchdogBoot uint32
}

type entry struct {
	tune     bool
	radio    byte
	flags    byte
	channel  byte // TUNE
	sf       byte // TUNE
	preamble uint16
	answer   proto.Time
	at       proto.Time // when it was queued
	deferred bool
	held     bool
	n        int
	data     [sx126x.MaxPacket]byte
}

type arm struct {
	channel, sf byte
	busy        bool
	start       proto.Time // the transmission's start
	queued      proto.Time // how long it waited
	answer      proto.Time // its answer window
	answerUntil proto.Time
	rxing       bool       // the radio says a packet is coming in
	rxStart     proto.Time // since when
	hearing     bool       // what BUSY last told the PC
	deadline    proto.Time // a transmission not ended by then is stuck
	nextCheck   proto.Time
	retry       proto.Time // wait after a failed Recover, 0 after a good one
}

// Bridge connects the PC to the radios. Set the fields, then Init, then
// call Handle for each message from the PC and Poll often. It is large
// (the queue): keep it in a global.
type Bridge struct {
	Radios []Radio
	// Up sends one message to the PC; the slice is valid during the call.
	Up func(msg []byte)
	// Frequency maps a channel (spec 15.3) to Hz; false: no such channel.
	Frequency func(channel byte) (uint32, bool)
	// SF is the spreading factor the radios start at (channel 0).
	SF    byte
	Stats Stats

	arms  [MaxRadios]arm
	queue [QueueLen]entry
	n     int
	out   [link.MaxHeader + sx126x.MaxPacket]byte
}

// Init sets the radios' bookkeeping up: channel 0 at SF, nothing queued.
func (b *Bridge) Init() {
	for i := range b.arms {
		b.arms[i] = arm{sf: b.SF}
	}
	b.n = 0
}

// Handle takes one message from the PC.
func (b *Bridge) Handle(msg []byte, now proto.Time) {
	if len(msg) == 0 {
		b.Stats.Malformed++
		return
	}
	switch msg[0] {
	case MsgTx:
		if len(msg) < txHeader {
			b.Stats.Malformed++
			return
		}
		r, frame := msg[1], msg[txHeader:]
		if int(r) >= len(b.Radios) || len(frame) == 0 || len(frame) > sx126x.MaxPacket {
			b.txDone(r, TxRefused, 0, 0)
			return
		}
		e := b.push(now)
		if e == nil {
			b.txDone(r, TxQueueFull, 0, 0)
			return
		}
		e.radio, e.flags = r, msg[2]
		e.preamble = uint16(msg[3])<<8 | uint16(msg[4])
		e.answer = proto.Time(uint16(msg[5])<<8|uint16(msg[6])) * proto.Millisecond
		e.n = copy(e.data[:], frame)
	case MsgTune:
		if len(msg) < 4 || int(msg[1]) >= len(b.Radios) {
			b.Stats.Malformed++
			return
		}
		e := b.push(now)
		if e == nil {
			b.Stats.QueueFull++
			return
		}
		e.tune, e.radio, e.channel, e.sf = true, msg[1], msg[2], msg[3]
	case MsgPing:
		b.status()
	default:
		b.Stats.Malformed++
	}
}

// push takes a free queue entry, or nil when the queue is full.
func (b *Bridge) push(now proto.Time) *entry {
	if b.n == QueueLen {
		return nil
	}
	e := &b.queue[b.n]
	b.n++
	e.tune, e.flags, e.channel, e.sf, e.preamble = false, 0, 0, 0, 0
	e.answer, e.at, e.deferred, e.held, e.n = 0, now, false, false, 0
	return e
}

// remove drops queue entry i, keeping the order of the others.
func (b *Bridge) remove(i int) {
	copy(b.queue[i:b.n], b.queue[i+1:b.n])
	b.n--
}

// Poll ends transmissions, hands up received packets and starts what the
// arbitration allows.
func (b *Bridge) Poll(now proto.Time) {
	for i, r := range b.Radios {
		a := &b.arms[i]
		if a.busy && r.Transmitting() && now >= a.deadline {
			a.busy = false
			b.Stats.TxStuck++
			b.txDone(byte(i), TxTimeout, a.queued, now-a.start)
			b.recover(i, now)
		}
		if a.busy && !r.Transmitting() {
			a.busy = false
			result := byte(TxOK)
			if r.TxTimedOut() {
				result = TxTimeout
			}
			b.txDone(byte(i), result, a.queued, now-a.start)
			if a.answer > 0 {
				a.answerUntil = now + a.answer
			}
		}
		if p := r.Receive(); p != nil {
			b.rx(byte(i), r.Packet(), p)
		}
		rxing := r.Receiving()
		if rxing && !a.rxing {
			a.rxStart = now
		}
		a.rxing = rxing
		// No packet lasts longer than the longest one: past that its end
		// was missed, the radio is free.
		hearing := rxing && now-a.rxStart <= r.Airtime(sx126x.MaxPacket, 0)
		if hearing != a.hearing {
			a.hearing = hearing
			b.busy(byte(i), hearing)
		}
		if !a.busy && !a.hearing && now >= a.nextCheck {
			a.nextCheck = now + CheckEvery
			if !r.Check() {
				b.recover(i, now)
			}
		}
	}
	b.dispatch(now)
}

// dispatch runs the queue in order. A radio's requests keep their order and
// wait while it receives a packet (rule 4); a TX waits while any radio
// transmits (rule 1); a TX not urgent waits while another radio's answer
// window is open (rule 2).
func (b *Bridge) dispatch(now proto.Time) {
	busy := false
	for i := range b.Radios {
		busy = busy || b.arms[i].busy
	}
	var blocked [MaxRadios]bool
	for i := 0; i < b.n; {
		e := &b.queue[i]
		a := &b.arms[e.radio]
		switch {
		case blocked[e.radio]:
		case a.hearing:
			if !e.held {
				e.held = true
				b.Stats.Held++
			}
		case e.tune && !a.busy:
			b.tune(e)
			b.remove(i)
			continue
		case e.tune || busy:
		case e.flags&FlagUrgent == 0 && b.answering(int(e.radio), now):
			if !e.deferred {
				e.deferred = true
				b.Stats.Deferred++
			}
		default:
			if b.Radios[e.radio].SendPreamble(e.data[:e.n], e.preamble) {
				a.busy, a.start, a.queued, a.answer = true, now, now-e.at, e.answer
				a.deadline = now + 2*b.Radios[e.radio].Airtime(e.n, e.preamble) + 100*proto.Millisecond + txMargin
				busy = true
				b.Stats.Sent++
			} else {
				b.txDone(e.radio, TxRefused, now-e.at, 0)
			}
			b.remove(i)
			continue
		}
		blocked[e.radio] = true
		i++
	}
}

// answering reports whether a radio other than r waits for an answer.
func (b *Bridge) answering(r int, now proto.Time) bool {
	for i := range b.Radios {
		if i != r && now < b.arms[i].answerUntil {
			return true
		}
	}
	return false
}

// recover resets radio i; when that fails it is tried again later, the
// wait doubling.
func (b *Bridge) recover(i int, now proto.Time) {
	a := &b.arms[i]
	ok := b.Radios[i].Recover()
	a.rxing = false
	if a.hearing {
		a.hearing = false
		b.busy(byte(i), false)
	}
	switch {
	case ok:
		a.retry = 0
	case a.retry == 0:
		a.retry = RetryFirst
	default:
		a.retry = min(2*a.retry, RetryMax)
	}
	a.nextCheck = now + max(a.retry, CheckEvery)
}

func (b *Bridge) tune(e *entry) {
	a := &b.arms[e.radio]
	freq, ok := b.Frequency(e.channel)
	if !ok || e.sf < 7 || e.sf > 12 || !b.Radios[e.radio].Retune(freq, e.sf) {
		b.Stats.TuneFailed++
		return
	}
	a.channel, a.sf = e.channel, e.sf
}

func (b *Bridge) rx(r byte, info sx126x.PacketInfo, p []byte) {
	o := &b.out
	o[0], o[1] = MsgRx, r
	o[2], o[3], o[4] = byte(uint16(info.RSSI)>>8), byte(info.RSSI), byte(info.SNR)
	n := copy(o[rxHeader:], p)
	b.Stats.Received++
	b.Up(o[:rxHeader+n])
}

func (b *Bridge) busy(r byte, receiving bool) {
	o := &b.out
	o[0], o[1], o[2] = MsgBusy, r, 0
	if receiving {
		o[2] = 1
	}
	b.Up(o[:3])
}

func (b *Bridge) txDone(r, result byte, queued, air proto.Time) {
	switch result {
	case TxRefused:
		b.Stats.Refused++
	case TxQueueFull:
		b.Stats.QueueFull++
	}
	q, t := millis(queued), millis(air)
	o := &b.out
	o[0], o[1], o[2] = MsgTxDone, r, result
	o[3], o[4], o[5], o[6] = byte(q>>8), byte(q), byte(t>>8), byte(t)
	b.Up(o[:7])
}

// millis is d in whole milliseconds, rounded, at most 65535.
func millis(d proto.Time) uint16 {
	return uint16(min(max((d+proto.Millisecond/2)/proto.Millisecond, 0), 0xFFFF))
}

func (b *Bridge) status() {
	o := &b.out
	o[0], o[1], o[2], o[3] = MsgStatus, Version, byte(len(b.Radios)), radioStatus
	n := 4
	for i, r := range b.Radios {
		queued := 0
		for j := 0; j < b.n; j++ {
			if int(b.queue[j].radio) == i {
				queued++
			}
		}
		healthy := byte(0)
		if r.Healthy() {
			healthy = 1
		}
		o[n], o[n+1], o[n+2], o[n+3] = healthy, b.arms[i].channel, b.arms[i].sf, byte(queued)
		n += 4
		c := r.Counts()
		for _, v := range [...]uint32{c.CRCErrors, c.HeaderErrors, c.BusRetries, c.Faults, c.Resets} {
			v = min(v, 0xFFFF)
			o[n], o[n+1] = byte(v>>8), byte(v)
			n += 2
		}
	}
	s := &b.Stats
	counters := [...]uint32{s.Received, s.Sent, s.Deferred, s.Refused, s.QueueFull, s.Malformed, s.TuneFailed, s.Held,
		s.TxStuck, s.WatchdogBoot}
	o[n] = byte(len(counters))
	n++
	for _, v := range counters {
		o[n], o[n+1], o[n+2], o[n+3] = byte(v>>24), byte(v>>16), byte(v>>8), byte(v)
		n += 4
	}
	b.Up(o[:n])
}
