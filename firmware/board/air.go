//go:build stm32u083

package board

import (
	"machine"

	"cairnmesh/firmware/link"
	"cairnmesh/firmware/proto"
	"cairnmesh/firmware/radio"
)

// SerialAir is the bench's radio.Air: each packet one SLIP + CRC16 frame on
// a UART (package link, spec 6), the PC's controller on the other end.
type SerialAir struct {
	UART *machine.UART
	dec  link.Decoder
	enc  [link.MaxEncoded]byte
}

func (a *SerialAir) Send(packet []byte) bool {
	n := link.SlipEncode(a.enc[:], packet)
	_, err := a.UART.Write(a.enc[:n])
	return err == nil
}

// Quiet is radio.Quiet: no byte waiting and no packet half received.
func (a *SerialAir) Quiet() bool { return a.UART.Buffered() == 0 && a.dec.Idle() }

func (a *SerialAir) Receive() []byte {
	for a.UART.Buffered() > 0 {
		b, _ := a.UART.ReadByte()
		if frame := a.dec.Feed(b); frame != nil {
			return frame
		}
	}
	return nil
}

// BenchEmitter stands in for a radio's signals on the bench, which has
// none: a signal holds the "radio" for its Duration (so the Stack's
// sharing rules run as they will with the SX1262) and lights Indicator, if
// any, meanwhile. Nothing is transmitted.
type BenchEmitter struct {
	Indicator interface{ Set(on bool) }
	// Signals counts the signals started.
	Signals uint32

	until proto.Time
	on    bool
}

func (e *BenchEmitter) Emit(s *radio.Signal, now proto.Time) bool {
	if s.Kind != radio.Carrier && s.Kind != radio.Packet {
		return false
	}
	e.until, e.on = now+s.Duration, true
	e.Signals++
	if e.Indicator != nil {
		e.Indicator.Set(true)
	}
	return true
}

func (e *BenchEmitter) Busy(now proto.Time) bool {
	if now < e.until {
		return true
	}
	if e.on {
		e.on = false
		if e.Indicator != nil {
			e.Indicator.Set(false)
		}
	}
	return false
}
