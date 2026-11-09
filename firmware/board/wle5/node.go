//go:build cairn_wle5

package wle5

import (
	"device/stm32"
	"runtime/volatile"
	"time"
	"unsafe"

	"cairnmesh/firmware/board/wle5/adapt"
	"cairnmesh/firmware/hw/aeshw"
	"cairnmesh/firmware/hw/flashhw"
	"cairnmesh/firmware/hw/rnghw"
	"cairnmesh/firmware/journal"
	"cairnmesh/firmware/keystore"
	"cairnmesh/firmware/proto"
)

// The node's peripherals on the STM32WLE5: the chip-free adapters are in
// package adapt (host-tested), this file plugs the registers into them. The
// flash pages are adapt's (the same as the U083's). NOT VERIFIED ON A BOARD.

// NewCipher enables the AES block, runs its FIPS-197 self-test and returns the
// proto.Cipher; false when the self-test failed (the node must send nothing).
func NewCipher() (*adapt.Cipher, bool) {
	dev := aeshw.New()
	dev.Configure()
	return &adapt.Cipher{HW: dev}, dev.SelfTest()
}

type rngSource struct{}

func (rngSource) Uint32() (uint32, error) { return rnghw.Uint32() }

// NewRand starts the RNG (PLLQ, hw/rnghw) and returns the proto.Rand.
func NewRand() (*adapt.Rand, error) {
	return &adapt.Rand{Src: rngSource{}}, rnghw.Configure()
}

// rawFlash is the chip as journal.Flash, unverified (adapt.Verified adds the
// read-back).
type rawFlash struct{}

func (rawFlash) Erase(page int) bool { return flashhw.ErasePage(page) == nil }

func (rawFlash) Program(page, offset int, dw *[8]byte) bool {
	return flashhw.Program(flashhw.PageAddr(page)+uintptr(offset), dw[:]) == nil
}

// Read reports false when the double word failed its ECC (a write cut by a
// reset or power loss, RM0461 3.3.3): the NMI handler below caught it.
//
// NOT VERIFIED ON A BOARD, and the order is an assumption: the flag is cleared
// before the two loads and read after them, which is right only if the NMI of a
// double ECC error is taken before the second load completes (the load stalls
// until the flash answers and the NMI preempts at once, but no barrier enforces
// it here, and a late NMI would let a bad read pass as good once). What limits
// the damage: a counter record carries a check, a key record a CRC-32, a
// journal entry its own check, so a bad double word that passes is refused by
// them, not used. Bench: the torn-write test (power cut /
// SWD reset during the journal and counter writes, then read back: ecc_faults
// in "stats" and "diag" counts the NMIs, and no record may be taken as good
// that was not written).
func (rawFlash) Read(page, offset int, dw *[8]byte) bool {
	eccFault.Set(0)
	addr := flashhw.PageAddr(page) + uintptr(offset)
	lo := volatile.LoadUint32((*uint32)(unsafe.Pointer(addr)))
	hi := volatile.LoadUint32((*uint32)(unsafe.Pointer(addr + 4)))
	*dw = [8]byte{byte(lo), byte(lo >> 8), byte(lo >> 16), byte(lo >> 24), byte(hi), byte(hi >> 8), byte(hi >> 16), byte(hi >> 24)}
	return eccFault.Get() == 0
}

// Flash is the chip's flash with every write read back.
var Flash = adapt.Verified{Raw: rawFlash{}}

// ECCFaults counts double ECC errors read from the flash since boot.
var ECCFaults uint32

var eccFault volatile.Register8

// A double ECC error raises an NMI (RM0461 3.3.3, FLASH_ECCR.ECCD; NOT VERIFIED
// ON A BOARD: that this handler is entered, clears ECCD and returns to the
// load that caused it is what the bench must show). Without
// this handler a double word torn by a power cut would stop the node at every
// boot; with it the read returns and the reader sees the fault. Any other NMI
// stops as before.
//
//export NMI_Handler
func nmiHandler() {
	if stm32.FLASH.ECCR.Get()&stm32.Flash_ECCR_ECCD_Msk != 0 {
		stm32.FLASH.ECCR.Set(stm32.Flash_ECCR_ECCD_Msk) // write 1 to clear
		eccFault.Set(1)
		ECCFaults++
		return
	}
	for {
	}
}

// Journals places the two journals in their pages (Load reads them).
func Journals() (*journal.Records, *journal.Marks) {
	return journal.NewRecords(journal.Where{Flash: Flash, First: adapt.RecordsPage, Pages: adapt.RecordsBank}),
		journal.NewMarks(journal.Where{Flash: Flash, First: adapt.MarksPage, Pages: adapt.MarksBank})
}

// NewCounterStore loads the stored FCNT bound.
func NewCounterStore() (*adapt.CounterStore, uint32) {
	return adapt.NewCounterStore(Flash, adapt.CounterPageA, adapt.CounterPageB)
}

// Keys is the key page.
var Keys = adapt.KeyStore{F: Flash, Page: adapt.KeyPage}

// LoadKeys, StoreKeys and EraseKeys: the key record.
func LoadKeys(r *keystore.Record) bool  { return Keys.Load(r) }
func StoreKeys(r *keystore.Record) bool { return Keys.Store(r) }
func EraseKeys() bool                   { return Keys.Erase() }

// ReadProtection is the flash read protection level (RM0461 3.4): 0 (RDP byte
// 0xAA, none), 1 (any other value) or 2 (0xCC). Deployed chips must be at level
// 1 at least. The WLE5 has no OEM1 key: a regression from 1 to 0
// erases the flash and SRAM (RM0461 Table 12).
func ReadProtection() int {
	switch stm32.FLASH.OPTR.Get() & stm32.Flash_OPTR_RDP_Msk {
	case 0xAA:
		return 0
	case 0xCC:
		return 2
	}
	return 1
}

var boot = time.Now()

// Now is the time since boot.
func Now() proto.Time { return proto.Time(time.Since(boot) / time.Microsecond) }

// BootResults are what the node's start-up learned about its peripherals, for
// the console's "diag". Set by the program at boot (cmd/wle5node).
var BootResults struct {
	AESRan, AESOK bool
	RNGRan        bool
	RNGErr        error
}

// Diagnose fills d with the boot results and the live clock and RNG state
// (registers only; no key material is read).
func Diagnose(d *adapt.Diag, cipher *adapt.Cipher, random *adapt.Rand) {
	*d = adapt.Diag{
		AESRan: BootResults.AESRan, AESOK: BootResults.AESOK,
		RNGRan:    BootResults.RNGRan,
		RNGSEL:    uint8(stm32.RCC.CCIPR.Get() >> stm32.RCC_CCIPR_RNGSEL_Pos),
		PLLRDY:    stm32.RCC.CR.HasBits(stm32.RCC_CR_PLLRDY),
		PLLQEN:    stm32.RCC.PLLCFGR.HasBits(stm32.RCC_PLLCFGR_PLLQEN),
		RNGSR:     stm32.RNG.SR.Get(),
		ECCFaults: ECCFaults,
	}
	if BootResults.RNGErr != nil {
		d.RNGErr = BootResults.RNGErr.Error()
	}
	if cipher != nil {
		d.AESFailures = cipher.Failures
	}
	if random != nil {
		d.RNGErrors = random.Errors
	}
}
