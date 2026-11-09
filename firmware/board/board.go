//go:build stm32u083

// Package board adapts the STM32U083 drivers (hw/...) to the interfaces of
// package proto: the AES accelerator as proto.Cipher, the true RNG as
// proto.Rand, a flash counter as proto.CounterStore, and the clock.
package board

import (
	"time"

	"cairnmesh/firmware/hw/aeshw"
	"cairnmesh/firmware/hw/flashhw"
	"cairnmesh/firmware/hw/rnghw"
	"cairnmesh/firmware/proto"
)

// Cipher is the AES accelerator. It holds one key: Encrypt reloads it when
// a different key (by address) comes, 4 register writes.
type Cipher struct {
	dev    aeshw.Device
	loaded *[16]byte
	// Failures counts blocks the hardware did not finish in time; the frame
	// then fails its MIC and is dropped.
	Failures uint32
}

// NewCipher enables the block and runs its self-test.
func NewCipher() (*Cipher, bool) {
	c := &Cipher{dev: aeshw.New()}
	c.dev.Configure()
	return c, c.dev.SelfTest()
}

func (c *Cipher) Encrypt(key *[16]byte, dst, src *[16]byte) {
	if key != c.loaded {
		c.dev.SetKey(key)
		c.loaded = key
	}
	if !c.dev.EncryptBlock(dst, src) {
		c.Failures++
	}
}

// Forget makes the next Encrypt reload its key (after a key changed in place).
func (c *Cipher) Forget() { c.loaded = nil }

// Rand draws from the true RNG; on an RNG error it falls back to the
// midpoint and counts it.
type Rand struct {
	Errors uint32
}

// NewRand starts the RNG.
func NewRand() (*Rand, error) { return &Rand{}, rnghw.Configure() }

func (r *Rand) Uniform(lo, hi proto.Time) proto.Time {
	if hi <= lo {
		return lo
	}
	a, err1 := rnghw.Uint32()
	b, err2 := rnghw.Uint32()
	if err1 != nil || err2 != nil {
		r.Errors++
		return (lo + hi) / 2
	}
	span := uint64(hi - lo + 1)
	return lo + proto.Time((uint64(a)<<32|uint64(b))%span)
}

// CounterStore keeps the FCNT reservation in the last two flash pages
// (flashhw.Counter: two alternating pages, power-safe). The firmware image
// must end below them (see the Makefile's size check).
type CounterStore struct {
	c flashhw.Counter
	// Errors counts failed or unverified writes.
	Errors uint32
}

// Pages of the counter: the last two of 128.
const (
	CounterPageA = 126
	CounterPageB = 127
)

// NewCounterStore loads the stored bound: the counter restarts there
// (spec section 5: a counter never repeats under one key).
func NewCounterStore() (*CounterStore, uint32) {
	s := &CounterStore{c: flashhw.Counter{PageA: CounterPageA, PageB: CounterPageB}}
	return s, s.c.Load()
}

// ReserveTx stores bound and reads it back; a counter is used only after
// this returned true. A page erase stalls the CPU about 22 ms: it happens
// once per 256 reservations (256 frames each).
func (s *CounterStore) ReserveTx(bound uint32) bool {
	if err := s.c.Store(bound); err != nil {
		s.Errors++
		return false
	}
	check := flashhw.Counter{PageA: CounterPageA, PageB: CounterPageB}
	if check.Load() != bound {
		s.Errors++
		return false
	}
	return true
}

var boot = time.Now()

// Now is the time since boot.
func Now() proto.Time { return proto.Time(time.Since(boot) / time.Microsecond) }
