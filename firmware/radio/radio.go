// Package radio is what the firmware asks of a radio, without naming one:
// the protocol's packets (Air) and transmissions outside the protocol
// (Emitter). The bench implements Air over the serial port and Emitter as a
// stub that only holds the radio (board); the SX1262 driver will implement
// both over SPI. Pure Go: the host tests use
// fakes.
package radio

import "cairnmesh/firmware/proto"

// LongPreamble is implemented by an Air that can send a packet with a
// preamble of that many symbols (a wake-up, spec 14.3); without it the
// relay's long-preamble frames go with the standard one.
type LongPreamble interface {
	SendPreamble(packet []byte, symbols uint16) bool
}

// Levels is implemented by an Air that measures what it receives: the RSSI
// (dBm) of the packet Receive returned last. The relay backs off and picks
// paths by it (spec 7, 11.3).
type Levels interface {
	PacketRSSI() (dbm int16, ok bool)
}

// Quiet is implemented by an Air that can tell whether a packet is coming
// in or going out right now. The node erases flash only while it is quiet:
// an erase stalls the CPU for about 22 ms (README rule 4). An Air without
// it is never taken for quiet.
type Quiet interface {
	Quiet() bool
}

// Air carries the protocol's packets: whole sealed frames on the network's
// channel and modem settings.
type Air interface {
	// Send transmits one packet; false when it could not go out (the
	// protocol's retries cover it).
	Send(packet []byte) bool
	// Receive returns the next packet received, or nil. The slice is valid
	// until the next call.
	Receive() []byte
}

// SignalKind is what an out-of-protocol transmission sends.
type SignalKind uint8

const (
	// Carrier is an unmodulated carrier for Signal.Duration.
	Carrier SignalKind = iota + 1
	// Packet is Signal.Payload sent as one packet with Signal.Modem.
	Packet
)

// Modem selects the modulation of a Packet signal. The zero value means the
// network's own settings.
type Modem struct {
	SpreadingFactor uint8  // LoRa SF5..SF12
	BandwidthHz     uint32 // LoRa 7.8 kHz..500 kHz
	CodingRate      uint8  // 5..8: 4/5..4/8
	PreambleSymbols uint16
}

// Signal is one transmission outside the protocol: a beacon, an alarm
// burst, a carrier for direction finding. It shares the radio with the
// protocol (see node.Host.Emit), so it never starts while a frame goes out,
// and the radio returns to the network's settings afterwards.
type Signal struct {
	Kind SignalKind
	// FrequencyHz: 0 means the network's channel.
	FrequencyHz uint32
	// PowerDBm: the radio clamps it to what it can do; MaxPower asks for its most.
	PowerDBm int8
	// Duration of a Carrier; a Packet lasts its airtime.
	Duration proto.Time
	Payload  []byte
	Modem    Modem
}

// MaxPower asks for the radio's highest output power.
const MaxPower int8 = 127

// Emitter sends signals outside the protocol. A radio without the
// capability simply does not implement it.
type Emitter interface {
	// Emit starts a signal at now; false when the radio cannot send it
	// (unsupported kind, settings out of range). The signal holds the radio
	// until Busy turns false.
	Emit(s *Signal, now proto.Time) bool
	// Busy reports whether a signal still holds the radio at now; the
	// emitter returns to the network's settings when it ends.
	Busy(now proto.Time) bool
}
