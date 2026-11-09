// Package adapt is the part of the WLE5 node's board support that needs no
// chip: the adapters between the protocol stack's interfaces (proto.Cipher,
// proto.Rand, proto.CounterStore, journal.Flash, radio.Air, console.Port) and
// the peripherals behind them (the AES block, the RNG, the flash, the radio,
// the console UART). The peripherals are small interfaces here; package wle5
// plugs the registers (hw/aeshw, hw/rnghw, hw/flashhw, hw/subghz) into them,
// and the host tests plug in fakes (a flash that loses power at any operation,
// an AES block that counts key loads, a radio that records what was sent).
//
// Pure Go, no allocation after construction. Nothing here names a pin or a
// register: the board layer (board/wle5) owns those.
package adapt

import "cairnmesh/firmware/proto"

// Block is the AES block as the Cipher uses it (hw/aeshw.Device has these).
type Block interface {
	// SetKey loads a 128-bit key.
	SetKey(key *[16]byte)
	// EncryptBlock encrypts one block; false when the block did not answer.
	EncryptBlock(dst, src *[16]byte) bool
}

// Cipher is proto.Cipher on a Block. It holds one key: Encrypt reloads it when
// a different key (by address) comes.
type Cipher struct {
	HW     Block
	loaded *[16]byte
	// Failures counts blocks the hardware did not finish; the frame then
	// fails its MIC and is dropped.
	Failures uint32
}

func (c *Cipher) Encrypt(key *[16]byte, dst, src *[16]byte) {
	if key != c.loaded {
		c.HW.SetKey(key)
		c.loaded = key
	}
	if !c.HW.EncryptBlock(dst, src) {
		c.Failures++
	}
}

// Forget makes the next Encrypt reload its key (after a key changed in place,
// or a temporary key whose address may be reused).
func (c *Cipher) Forget() { c.loaded = nil }

// Source is the true random number generator (hw/rnghw.Uint32 is one).
type Source interface {
	Uint32() (uint32, error)
}

// Rand is proto.Rand on a Source; on an error it falls back to the midpoint
// and counts it.
type Rand struct {
	Src    Source
	Errors uint32
}

func (r *Rand) Uniform(lo, hi proto.Time) proto.Time {
	if hi <= lo {
		return lo
	}
	a, err1 := r.Src.Uint32()
	b, err2 := r.Src.Uint32()
	if err1 != nil || err2 != nil {
		r.Errors++
		return (lo + hi) / 2
	}
	span := uint64(hi - lo + 1)
	return lo + proto.Time((uint64(a)<<32|uint64(b))%span)
}
