// Package link is the serial hop between the PC and a board (spec section
// 6; cairnmesh/serial_link.py):
//
//	END | SLIP-escaped(frame || CRC16) | END
//
// SLIP is RFC 1055 (END 0xC0, ESC 0xDB, ESC_END 0xDC, ESC_ESC 0xDD); the CRC
// is CRC16-CCITT-FALSE (poly 0x1021, init 0xFFFF), big-endian. Until a board
// carries a radio, the bench firmware (cmd/node) treats this link as its
// air: the PC plays the controller over the ST-LINK serial port.
package link

const (
	slipEnd    = 0xC0
	slipEsc    = 0xDB
	slipEscEnd = 0xDC
	slipEscEsc = 0xDD

	// MaxHeader is the most a gateway bridge message puts before a LoRa
	// packet (spec 6, package gateway).
	MaxHeader = 8
	// MaxPayload is the largest unframed payload: a LoRa packet, a bridge
	// header and the CRC.
	MaxPayload = 255 + MaxHeader + 2
	// MaxEncoded is the room SlipEncode needs at most.
	MaxEncoded = 2*MaxPayload + 2
)

// crcTable is never written, so it stays in flash.
var crcTable = func() (t [256]uint16) {
	for i := range t {
		crc := uint16(i) << 8
		for j := 0; j < 8; j++ {
			if crc&0x8000 != 0 {
				crc = crc<<1 ^ 0x1021
			} else {
				crc <<= 1
			}
		}
		t[i] = crc
	}
	return
}()

// CRC16 is CRC16-CCITT-FALSE: CRC16([]byte("123456789")) == 0x29B1.
func CRC16(data []byte) uint16 {
	crc := uint16(0xFFFF)
	for _, b := range data {
		crc = crc<<8 ^ crcTable[byte(crc>>8)^b]
	}
	return crc
}

// SlipEncode appends the CRC to frame, SLIP-encodes the result into dst
// (MaxEncoded bytes are always enough) and returns the encoded length.
func SlipEncode(dst, frame []byte) int {
	crc := CRC16(frame)
	dst[0] = slipEnd
	n := 1
	for _, b := range frame {
		n = slipPut(dst, n, b)
	}
	n = slipPut(dst, n, byte(crc>>8))
	n = slipPut(dst, n, byte(crc))
	dst[n] = slipEnd
	return n + 1
}

func slipPut(dst []byte, n int, b byte) int {
	switch b {
	case slipEnd:
		dst[n], dst[n+1] = slipEsc, slipEscEnd
		return n + 2
	case slipEsc:
		dst[n], dst[n+1] = slipEsc, slipEscEsc
		return n + 2
	}
	dst[n] = b
	return n + 1
}

// Decoder collects bytes from a serial stream into frames; bad packets are
// counted, not returned.
type Decoder struct {
	buf     [MaxPayload]byte
	n       int
	esc     bool
	drop    bool // the current packet overflowed buf
	Frames  uint32
	CRCErrs uint32
	Overrun uint32
}

// Idle reports whether no packet is half received.
func (s *Decoder) Idle() bool { return s.n == 0 && !s.esc && !s.drop }

// Feed adds one byte. When the byte ends a valid packet, Feed returns the
// frame without its CRC; the slice stays valid until the next call.
func (s *Decoder) Feed(b byte) []byte {
	if b == slipEnd {
		n, drop := s.n, s.drop
		s.n, s.esc, s.drop = 0, false, false
		if drop {
			s.Overrun++
			return nil
		}
		if n == 0 {
			return nil // empty packet between two END bytes
		}
		if n < 3 || CRC16(s.buf[:n-2]) != uint16(s.buf[n-2])<<8|uint16(s.buf[n-1]) {
			s.CRCErrs++
			return nil
		}
		s.Frames++
		return s.buf[:n-2]
	}
	if s.esc {
		s.esc = false
		switch b {
		case slipEscEnd:
			b = slipEnd
		case slipEscEsc:
			b = slipEsc
		}
	} else if b == slipEsc {
		s.esc = true
		return nil
	}
	if s.n == len(s.buf) {
		s.drop = true
		return nil
	}
	s.buf[s.n] = b
	s.n++
	return nil
}
