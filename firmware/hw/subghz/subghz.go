//go:build cairn_wle5

// Package subghz connects the driver in radio/sx126x to the radio inside the
// STM32WLE5: the SUBGHZSPI master (SPI3 at 0x58010000, RM0461 35), and the
// four signals the driver wants as pins, which the chip implements in its
// power and clock controllers instead of on package pins:
//
//	NSS    PWR_SUBGHZSPICR.NSS (bit 15; 0 = low). Reset value 1.
//	Reset  RCC_CSR.RFRST (bit 15; 1 = radio in reset), RFRSTF (bit 14) = in reset.
//	Busy   PWR_SR2.RFBUSYS (bit 1, the radio's busy signal) AND RFBUSYMS (bit 2,
//	       the masked busy, RM0461 5.3: high from NSS falling to at least the
//	       minimum busy delay after NSS rising). Both must be high. Evidence:
//	       on the E77 bench RFBUSYMS never fell after any command while RFBUSYS
//	       did, and with the AND rule the probe matrix passed (12 of 14, 2026-11-06,
//	       cmd/wle5probe) and the link test ran. That RFBUSYMS stays high is NOT
//	       explained. That ST's HAL ANDs the two bits is recalled, not checked
//	       against its source (not on this machine). The AND rule has no mask for
//	       the gap between NSS rising and RFBUSY rising (up to 600 ns), so NSS.Set(true)
//	       waits that long itself (BusyRiseReads).
//	DIO1   the radio's IRQ line has no pin; it is NVIC 50 (EXTI line 44) and
//	       the level cannot be read, so Get asks the radio (GetIrqStatus).
//
// NOT VERIFIED ON A BOARD (2026-11-05, no hardware yet): every register and bit
// is from RM0461 Rev 11 and the SVD in TinyGo 0.42.0. The first bring-up
// has to confirm all of them.
package subghz

import (
	"device/stm32"
	"errors"
	"runtime/volatile"
	"unsafe"

	"cairnmesh/firmware/radio/sx126x"
)

// SPI_CR1, SPI_CR2 and SPI_SR bits (the same SPI block as the U0: RM0461 35.9).
const (
	cr1MSTR = 1 << 2
	cr1BRPo = 3
	cr1SPE  = 1 << 6
	cr1SSI  = 1 << 8
	cr1SSM  = 1 << 9

	cr2DS8bit = 7 << 8
	cr2FRXTH  = 1 << 12

	srRXNE = 1 << 0
	srTXE  = 1 << 1
	srBSY  = 1 << 7

	nssBit = 1 << 15 // PWR_SUBGHZSPICR.NSS
)

// riseReads is the NSS-rising wait in PWR_SR2 reads for the clock Init was
// given; 10 (48 MHz) until then.
var riseReads = 10

// ErrTimeout is returned when the SPI controller does not finish a frame.
var ErrTimeout = errors.New("subghz: spi timeout")
var errLength = errors.New("subghz: Tx buffers differ in length")

const resetSpins = 100000

// ResetTimedOut is true when the last Reset.Set(true) gave up waiting for RFRSTF
// to clear; the driver's Init then fails on BUSY, and this says why.
var ResetTimedOut bool

// Init turns the SUBGHZSPI clock on and sets the master up for 8-bit frames
// at the highest SCK the radio takes. Call it after the system clock is set,
// and again with the new PCLK3 if it changes. The radio itself starts when
// the driver's Init releases its reset.
func Init(pclk3 uint32) {
	riseReads = BusyRiseReads(pclk3)
	stm32.RCC.APB3ENR.SetBits(stm32.RCC_APB3ENR_SUBGHZSPIEN)
	// NSS from software (PWR_SUBGHZSPICR), which keeps RFBUSYMS valid.
	stm32.PWR.CR1.ClearBits(stm32.PWR_CR1_SUBGHZSPINSSSEL)
	stm32.PWR.SUBGHZSPICR.SetBits(nssBit) // idle high
	r := stm32.SPI3
	r.CR1.ClearBits(cr1SPE)
	r.CR1.Set(uint32(cr1MSTR|cr1SSM|cr1SSI) | BaudBits(pclk3)<<cr1BRPo)
	r.CR2.Set(cr2DS8bit | cr2FRXTH)
	r.CR1.SetBits(cr1SPE)
}

// Bus is SUBGHZSPI. Tx is the transfer of sx126x.Bus; NSS is the Pin below.
type Bus struct{}

func (Bus) dr8() *volatile.Register8 {
	return (*volatile.Register8)(unsafe.Pointer(&stm32.SPI3.DR))
}

func wait(mask, want uint32) bool {
	for i := 0; i < 100000; i++ {
		if stm32.SPI3.SR.Get()&mask == want {
			return true
		}
	}
	return false
}

// Tx sends w and receives into r at the same time; either may be nil, and
// when both are set they have the same length. DR is accessed as 8 bits (a
// 16-bit store would queue two frames, RM0461 35.5.9).
func (b Bus) Tx(w, r []byte) error {
	n := len(w)
	if n == 0 {
		n = len(r)
	} else if r != nil && len(r) != n {
		return errLength
	}
	dr := b.dr8()
	sr := &stm32.SPI3.SR
	sent, got := 0, 0
	for spins := 0; got < n; spins++ {
		if spins > 1000000 {
			return ErrTimeout // no clock on the SPI, or no radio behind it
		}
		// At most two frames in flight, so the 32-bit RX FIFO cannot overflow.
		if sent < n && sent-got < 2 && sr.Get()&srTXE != 0 {
			var c byte
			if w != nil {
				c = w[sent]
			}
			dr.Set(c)
			sent++
		}
		if sr.Get()&srRXNE != 0 {
			c := dr.Get()
			if r != nil {
				r[got] = c
			}
			got++
		}
	}
	if !wait(srBSY, 0) {
		return ErrTimeout
	}
	return nil
}

// NSS is the chip-select: Set(false) selects the radio.
type NSS struct{}

func (NSS) Set(high bool) {
	if high {
		stm32.PWR.SUBGHZSPICR.SetBits(nssBit)
		// Let RFBUSY rise before anyone looks at Busy (see the package comment):
		// each read is a volatile load of at least 3 cycles, so this is at least
		// BusyRiseNs whatever the clock (the driver's own spin after NSS was
		// timed on the U083's GPIO reads, 1.2 us each, and a PWR_SR2 read is
		// much faster).
		for i := 0; i < riseReads; i++ {
			stm32.PWR.SR2.Get()
		}
	} else {
		stm32.PWR.SUBGHZSPICR.ClearBits(nssBit)
	}
}
func (NSS) Get() bool { return stm32.PWR.SUBGHZSPICR.HasBits(nssBit) }

// Reset is the radio's reset. Set(false) holds it in reset. Set(true) lets it
// go, waits for RFRSTF to clear (the radio must not be accessed before) and
// wakes it with a falling edge on NSS: after a reset the radio is busy until
// that edge (RM0461 5.3), and the driver waits for BUSY next.
type Reset struct{}

func (Reset) Set(high bool) {
	if !high {
		stm32.RCC.CSR.SetBits(stm32.RCC_CSR_RFRST)
		return
	}
	stm32.RCC.CSR.ClearBits(stm32.RCC_CSR_RFRST)
	ResetTimedOut = true
	for i := 0; i < resetSpins; i++ {
		if !stm32.RCC.CSR.HasBits(stm32.RCC_CSR_RFRSTF) {
			ResetTimedOut = false
			break
		}
	}
	NSS{}.Set(false)
	// RM0461 5.x: waking from Sleep needs NSS low >= 20 us; after a reset only
	// the falling edge is specified, so holding it low as long is harmless.
	// Each iteration is a volatile register read (>= 3 cycles), so the
	// compiler cannot drop the loop; 1000 reads is >= 60 us at 48 MHz.
	for i := 0; i < 1000; i++ {
		NSS{}.Get()
	}
	NSS{}.Set(true)
}
func (Reset) Get() bool { return !stm32.RCC.CSR.HasBits(stm32.RCC_CSR_RFRST) }

// BusyMode picks which PWR_SR2 bits Busy.Get reads (RM0461 5.3, 5.5.6).
type BusyMode uint8

const (
	// BusyMS: RFBUSYMS alone, the masked status. What every program used up to
	// 2026-11-06; on the E77 bench it never falls (calms = -1 in the probe).
	BusyMS BusyMode = iota
	// BusyAnd: RFBUSYS and RFBUSYMS both high, the rule of ST's HAL
	// (SUBGHZ_WaitOnBusy, recalled, not checked); the default. Bench 2026-11-06:
	// the radio calibrated in 6-9 ms (RFBUSYS high that long) and every later
	// command worked.
	BusyAnd
	// BusyS: the unmasked RFBUSYS alone.
	BusyS
)

// Mode is the busy definition in use; only the bench probe changes it.
var Mode = BusyAnd

// RFBusyS reports RFBUSYS, the radio's busy signal.
func RFBusyS() bool { return stm32.PWR.SR2.HasBits(stm32.PWR_SR2_RFBUSYS) }

// RFBusyMS reports RFBUSYMS, the masked busy status.
func RFBusyMS() bool { return stm32.PWR.SR2.HasBits(stm32.PWR_SR2_RFBUSYMS) }

// Busy is the radio's busy line: RFBUSYS && RFBUSYMS by default, see Mode.
// NSS.Set(true) has already waited the 600 ns RFBUSY needs to rise.
type Busy struct{}

func (Busy) Set(bool) {}
func (Busy) Get() bool {
	switch Mode {
	case BusyAnd:
		sr2 := stm32.PWR.SR2.Get()
		return sr2&stm32.PWR_SR2_RFBUSYS != 0 && sr2&stm32.PWR_SR2_RFBUSYMS != 0
	case BusyS:
		return RFBusyS()
	}
	return RFBusyMS()
}

// IRQ stands for DIO1: Get is true while the radio has an enabled interrupt
// pending, found with GetIrqStatus (0x12) because the line cannot be read.
// It costs one 4-byte transfer per call (about 3 µs at 12 MHz), and the NSS
// edge wakes a radio that sleeps, so it is for a radio that is running (the
// driver polls only then). After EnableIRQ it reads what the radio's interrupt
// handler left instead (irq.go, latch.go): no SPI traffic while nothing happens.
type IRQ struct{}

func (IRQ) Set(bool) {}
func (IRQ) Get() bool {
	if irqMode.Load() {
		return takeIRQ()
	}
	w := [4]byte{0x12, 0, 0, 0}
	var r [4]byte
	// The command is polled between the driver's own transfers, never inside one.
	for i := 0; i < 100000 && (Busy{}).Get(); i++ {
	}
	NSS{}.Set(false)
	err := Bus{}.Tx(w[:], r[:])
	NSS{}.Set(true)
	return err == nil && (r[2]|r[3]) != 0
}

// Pins are the four signals in the form radio/sx126x takes.
func Pins() sx126x.Pins {
	return sx126x.Pins{NSS: NSS{}, Reset: Reset{}, Busy: Busy{}, DIO1: IRQ{}}
}
