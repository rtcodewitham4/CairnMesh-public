package link

import (
	"bytes"
	"testing"
)

func TestCRC16(t *testing.T) {
	if got := CRC16([]byte("123456789")); got != 0x29B1 {
		t.Fatalf("CRC16 = %#04x, want 0x29b1", got)
	}
}

func TestSlipRoundTrip(t *testing.T) {
	frame := []byte{0x81, 0xC0, 0xDB, 0x00, 0xDC, 0xDD, 0xC0}
	var enc [MaxEncoded]byte
	n := SlipEncode(enc[:], frame)
	var d Decoder
	var got []byte
	// Noise before the packet ends in a bad packet, counted, not returned.
	for _, b := range append([]byte{'x', 'y', slipEnd}, enc[:n]...) {
		if f := d.Feed(b); f != nil {
			got = append([]byte(nil), f...)
		}
	}
	if !bytes.Equal(got, frame) || d.Frames != 1 || d.CRCErrs != 1 {
		t.Fatalf("got %x (frames %d, crc errors %d)", got, d.Frames, d.CRCErrs)
	}
}
