//go:build stm32u083

package board

import (
	"unsafe"

	"cairnmesh/firmware/hw/flashhw"
	"cairnmesh/firmware/keystore"
)

// KeyPage holds the key record (keystore), below the counter pages. The
// firmware image must end below it (the Makefile's size check). An RDP
// regression erases it with the rest of the flash.
const KeyPage = 125

// LoadKeys reads the key record; false when none is stored (or it is damaged).
func LoadKeys(r *keystore.Record) bool {
	src := unsafe.Slice((*byte)(unsafe.Pointer(flashhw.PageAddr(KeyPage))), keystore.RecordSize)
	return r.Decode(src)
}

// StoreKeys writes the key record and reads it back. The page erase stalls
// the CPU about 22 ms.
func StoreKeys(r *keystore.Record) bool {
	var b [keystore.RecordSize]byte
	r.Encode(&b)
	if flashhw.ErasePage(KeyPage) != nil || flashhw.Program(flashhw.PageAddr(KeyPage), b[:]) != nil {
		return false
	}
	var check keystore.Record
	return LoadKeys(&check) && check == *r
}

// EraseKeys removes the key record.
func EraseKeys() bool { return flashhw.ErasePage(KeyPage) == nil }

// Bound is the stored FCNT bound: counters up to it may already have been
// used, the node restarts above it.
func (s *CounterStore) Bound() uint32 { return s.c.Value() }

// Raise stores a higher bound (re-provisioning after the counter pages
// were erased: above the highest FCNT the controller saw, spec 5). Only
// upward: a lower bound would let the node reuse counters. Reboot right
// after: the running protocol keeps its own reservation.
func (s *CounterStore) Raise(bound uint32) bool {
	if bound <= s.c.Value() {
		return false
	}
	return s.ReserveTx(bound)
}
