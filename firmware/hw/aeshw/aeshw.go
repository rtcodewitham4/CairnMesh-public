// Package aeshw drives the AES hardware accelerator of the STM32U083 and the
// STM32WLE5 (the same block; the RCC clock bits differ and are in
// clock_stm32u083.go and clock_cairn_wle5.go).
//
// The block supports AES-128/192/256 in ECB, CBC, CTR, GCM and CCM mode.
// This driver uses ECB with a 128-bit key and polling only.
// The sequence follows ST HAL (stm32u0xx_hal_cryp.c): configure CR with the
// block disabled, load the key, set EN, write DINR four times, wait for CCF,
// clear CCF, then read DOUTR four times.
// See also RM0503 (U083) and RM0461 (WLE5), chapter "AES hardware accelerator".
package aeshw

import (
	"device/stm32"
	"runtime/volatile"
)

// CR fields. See RM0503, AES control register (AES_CR).
const (
	crEN         = 1 << 0  // EN: enable the block
	crModeEnc    = 0 << 3  // MODE = 00: encryption
	crChmodECB   = 0 << 5  // CHMOD = 00: electronic codebook
	crCCFC       = 1 << 7  // CCFC: clear the computation complete flag
	crERRC       = 1 << 8  // ERRC: clear the read and write error flags
	crKeysize128 = 0 << 18 // KEYSIZE = 0: 128-bit key
)

// CR.DATATYPE values. RM0503: 00 = 32-bit, 01 = 16-bit, 10 = 8-bit, 11 = 1-bit.
const (
	DataType32 = 0
	DataType16 = 1
	DataType8  = 2
	DataType1  = 3
)

// SR bits. CCF is bit 0. The SVD of this chip does not name CCF.
const (
	srCCF    = 1 << 0
	srRDERRF = 1 << 1
	srWRERRF = 1 << 2
	srBUSY   = 1 << 3
)

// Device is a driver for the AES block.
//
// Byte mapping, verified on hardware with the FIPS-197 test vector:
//
//	KEYR3 = key bytes [0:4], KEYR2 = [4:8], KEYR1 = [8:12], KEYR0 = [12:16],
//	each as a big-endian word, and DINR/DOUTR words are big-endian too with
//	CR.DATATYPE = 0.
type Device struct {
	Block    *stm32.AES_Type
	DataType uint32 // CR.DATATYPE, see the DataType constants
}

// New returns a driver for the AES block.
func New() Device {
	return Device{Block: stm32.AES, DataType: DataType32}
}

// Configure enables the clock of the AES block and resets the block (the RCC
// bits differ by chip: clock_stm32u083.go, clock_cairn_wle5.go).
func (d Device) Configure() {
	enableClock()
}

// SetKey loads a 128-bit key and enables the block in ECB encryption mode.
func (d Device) SetKey(key *[16]byte) {
	cr := crModeEnc | crChmodECB | crKeysize128 | d.DataType<<1
	d.Block.CR.Set(cr) // EN = 0 during the key load

	d.Block.KEYR3.Set(be32(key[0:4]))
	d.Block.KEYR2.Set(be32(key[4:8]))
	d.Block.KEYR1.Set(be32(key[8:12]))
	d.Block.KEYR0.Set(be32(key[12:16]))

	d.Block.CR.Set(cr | crEN)
}

// EncryptBlock encrypts one 16-byte block in ECB mode.
// It returns false when the hardware does not answer in time.
func (d Device) EncryptBlock(dst, src *[16]byte) bool {
	// Remove a stale flag from an earlier block. The flag is cleared through
	// CR.CCFC, not through a write to SR.
	d.Block.CR.SetBits(crCCFC)
	d.Block.DINR.Set(be32(src[0:4]))
	d.Block.DINR.Set(be32(src[4:8]))
	d.Block.DINR.Set(be32(src[8:12]))
	d.Block.DINR.Set(be32(src[12:16]))
	if !waitSet(&d.Block.SR, srCCF, 100000) {
		return false
	}
	d.Block.CR.SetBits(crCCFC) // clear CCF before reading DOUTR, as ST HAL does
	putBE32(dst[0:4], d.Block.DOUTR.Get())
	putBE32(dst[4:8], d.Block.DOUTR.Get())
	putBE32(dst[8:12], d.Block.DOUTR.Get())
	putBE32(dst[12:16], d.Block.DOUTR.Get())
	return true
}

// ClearErrors clears the read and write error flags of the block.
func (d Device) ClearErrors() {
	d.Block.CR.SetBits(crERRC)
}

// SelfTest encrypts two standard AES-128 ECB vectors and compares the results.
// It reloads the key between the vectors, so it also checks the key load path.
// Use it at start-up to confirm that the block works.
func (d Device) SelfTest() bool {
	// FIPS-197 appendix C.1, AES-128.
	key1 := [16]byte{0x00, 0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08, 0x09, 0x0a, 0x0b, 0x0c, 0x0d, 0x0e, 0x0f}
	pt1 := [16]byte{0x00, 0x11, 0x22, 0x33, 0x44, 0x55, 0x66, 0x77, 0x88, 0x99, 0xaa, 0xbb, 0xcc, 0xdd, 0xee, 0xff}
	want1 := [16]byte{0x69, 0xc4, 0xe0, 0xd8, 0x6a, 0x7b, 0x04, 0x30, 0xd8, 0xcd, 0xb7, 0x80, 0x70, 0xb4, 0xc5, 0x5a}

	// NIST vector, all-zero key and all-zero plaintext.
	var key2 [16]byte
	var pt2 [16]byte
	want2 := [16]byte{0x66, 0xe9, 0x4b, 0xd4, 0xef, 0x8a, 0x2c, 0x3b, 0x88, 0x4c, 0xfa, 0x59, 0xca, 0x34, 0x2b, 0x2e}

	d.SetKey(&key1)
	var got [16]byte
	if !d.EncryptBlock(&got, &pt1) || got != want1 {
		return false
	}
	// Encrypt the second vector, then the first one again. This catches state
	// that leaks from one block to the next.
	d.SetKey(&key2)
	if !d.EncryptBlock(&got, &pt2) || got != want2 {
		return false
	}
	d.SetKey(&key1)
	if !d.EncryptBlock(&got, &pt1) || got != want1 {
		return false
	}
	return true
}

// Status returns the raw SR value. Use it for diagnosis only.
func (d Device) Status() uint32 {
	return d.Block.SR.Get()
}

func waitSet(reg *volatile.Register32, mask uint32, limit int) bool {
	for i := 0; i < limit; i++ {
		if reg.HasBits(mask) {
			return true
		}
	}
	return false
}

// be32 reads four bytes as a big-endian word.
func be32(b []byte) uint32 {
	return uint32(b[0])<<24 | uint32(b[1])<<16 | uint32(b[2])<<8 | uint32(b[3])
}

// putBE32 writes a big-endian word into four bytes.
func putBE32(b []byte, v uint32) {
	b[0] = byte(v >> 24)
	b[1] = byte(v >> 16)
	b[2] = byte(v >> 8)
	b[3] = byte(v)
}
