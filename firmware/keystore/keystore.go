// Package keystore is the layout of the node's key record: the node's
// address and keys, provisioned over the debug console into a flash page of
// their own and never part of the firmware image. Pure Go: the
// board writes and reads the bytes (board.LoadKeys, board.StoreKeys).
//
// Record (RecordSize bytes, little endian):
//
//	0   magic "LFK1"
//	4   format version (1), node ID, application key version, 0
//	8   network key (16 B)
//	24  application key (16 B)
//	40  zero (20 B)
//	60  CRC-32 (IEEE) of bytes 0..59
package keystore

import "cairnmesh/firmware/proto"

// RecordSize is the record's length, a multiple of the flash's 8-byte
// programming unit.
const RecordSize = 64

const version = 1

var magic = [4]byte{'L', 'F', 'K', '1'}

// Record is a node's identity and keys.
type Record struct {
	Node          byte
	AppKeyVersion byte
	Network       [16]byte
	App           [16]byte
}

// ValidNode reports whether the node ID can be a device's: not 0, not the
// default controller, not broadcast.
func ValidNode(id byte) bool {
	return id != 0 && id != proto.DefaultControllerID && id != proto.BroadcastID
}

// Encode writes the record.
func (r *Record) Encode(dst *[RecordSize]byte) {
	*dst = [RecordSize]byte{}
	copy(dst[0:4], magic[:])
	dst[4], dst[5], dst[6] = version, r.Node, r.AppKeyVersion
	copy(dst[8:24], r.Network[:])
	copy(dst[24:40], r.App[:])
	c := crc32(dst[:60])
	dst[60], dst[61], dst[62], dst[63] = byte(c), byte(c>>8), byte(c>>16), byte(c>>24)
}

// Decode reads a record; false when the bytes are not one (erased flash,
// another format, a damaged copy, an invalid node ID).
func (r *Record) Decode(src []byte) bool {
	if len(src) < RecordSize || [4]byte(src[0:4]) != magic || src[4] != version {
		return false
	}
	c := uint32(src[60]) | uint32(src[61])<<8 | uint32(src[62])<<16 | uint32(src[63])<<24
	if crc32(src[:60]) != c || !ValidNode(src[5]) {
		return false
	}
	r.Node, r.AppKeyVersion = src[5], src[6]
	copy(r.Network[:], src[8:24])
	copy(r.App[:], src[24:40])
	return true
}

// KCV is a key's check value: the first 3 bytes of AES(key, zero block).
// It identifies a key without revealing it, to compare with the operator's
// key table after provisioning.
func KCV(c proto.Cipher, key *[16]byte) [3]byte {
	var zero, out [16]byte
	c.Encrypt(key, &out, &zero)
	return [3]byte{out[0], out[1], out[2]}
}

// crc32 is CRC-32/IEEE, bit by bit (the record is small and written rarely).
func crc32(b []byte) uint32 {
	c := ^uint32(0)
	for _, x := range b {
		c ^= uint32(x)
		for i := 0; i < 8; i++ {
			c = c>>1 ^ 0xEDB88320&-(c&1)
		}
	}
	return ^c
}
