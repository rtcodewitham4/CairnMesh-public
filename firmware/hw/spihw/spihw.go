// Package spihw drives the SPI controllers of the STM32U083 as a polled
// master. TinyGo has no SPI driver for the U0 family (machine_stm32_spi.go
// excludes stm32u0), so the SX126x radio cannot be used without this package.
//
// Bus implements the tinygo.org/x/drivers.SPI interface (Tx and Transfer),
// which is all the official sx126x driver needs.
//
// The U0 SPI has a 32-bit FIFO and a data size field (CR2.DS), like the G0
// and L4. Two details matter for 8-bit frames (RM0503, "Data packing"):
// CR2.FRXTH must be 1, and DR must be accessed with 8-bit loads and stores.
// A 16-bit store to DR queues two frames.
package spihw

import (
	"device/stm32"
	"errors"
	"machine"
	"runtime/volatile"
	"unsafe"
)

// CR1, CR2 and SR bits. See RM0503, SPI registers.
const (
	cr1CPHA     = 1 << 0
	cr1CPOL     = 1 << 1
	cr1MSTR     = 1 << 2
	cr1BRPos    = 3
	cr1SPE      = 1 << 6
	cr1LSBFIRST = 1 << 7
	cr1SSI      = 1 << 8
	cr1SSM      = 1 << 9

	cr2DS8bit = 7 << 8
	cr2FRXTH  = 1 << 12

	srRXNE = 1 << 0
	srTXE  = 1 << 1
	srBSY  = 1 << 7
)

var (
	// ErrTimeout is returned when the controller does not finish a frame.
	ErrTimeout = errors.New("spi: timeout")
	errLength  = errors.New("spi: Tx buffers differ in length")
)

// Bus is one SPI controller in master mode.
type Bus struct {
	Regs    *stm32.SPI_Type
	AltFunc uint8 // GPIO alternate function of SCK, SDO and SDI
	enable  func()
}

// SPI1 is on APB2. On the NUCLEO-U083RC its default pins are PA5 (SCK,
// also the green LED), PA6 (MISO) and PA7 (MOSI): Arduino D13, D12, D11.
var SPI1 = &Bus{
	Regs:    stm32.SPI1,
	AltFunc: 5,
	enable:  func() { stm32.RCC.APBENR2.SetBits(stm32.RCC_APBENR2_SPI1EN) },
}

// SPI2 is on APB1. Check the alternate function of the chosen pins in the
// datasheet before use; AltFunc may need to change.
var SPI2 = &Bus{
	Regs:    stm32.SPI2,
	AltFunc: 1,
	enable:  func() { stm32.RCC.APBENR1.SetBits(stm32.RCC_APBENR1_SPI2EN) },
}

// Config is the bus setup. Frequency is the highest SCK frequency wanted;
// the controller divides PCLK by 2 to 256, so the result can be lower.
type Config struct {
	SCK, SDO, SDI machine.Pin
	Frequency     uint32
	Mode          uint8 // 0..3, CPOL<<1 | CPHA
	LSBFirst      bool
}

// Configure sets the pins and the controller up. NSS is not used: drive the
// chip select pin of each device as a GPIO.
func (b *Bus) Configure(c Config) {
	b.enable()
	c.SCK.ConfigureAltFunc(machine.PinConfig{Mode: machine.PinModeSPICLK}, b.AltFunc)
	c.SDO.ConfigureAltFunc(machine.PinConfig{Mode: machine.PinModeSPISDO}, b.AltFunc)
	c.SDI.ConfigureAltFunc(machine.PinConfig{Mode: machine.PinModeSPISDI}, b.AltFunc)

	r := b.Regs
	r.CR1.ClearBits(cr1SPE)
	cr1 := uint16(cr1MSTR|cr1SSM|cr1SSI) | uint16(BaudDivisorBits(machine.CPUFrequency(), c.Frequency))<<cr1BRPos
	if c.Mode&1 != 0 {
		cr1 |= cr1CPHA
	}
	if c.Mode&2 != 0 {
		cr1 |= cr1CPOL
	}
	if c.LSBFirst {
		cr1 |= cr1LSBFIRST
	}
	r.CR1.Set(cr1)
	r.CR2.Set(cr2DS8bit | cr2FRXTH)
	r.CR1.SetBits(cr1SPE)
}

// BaudDivisorBits returns CR1.BR for the fastest SCK that does not exceed
// want: SCK = pclk / 2^(BR+1).
func BaudDivisorBits(pclk, want uint32) uint32 {
	br := uint32(0)
	for br < 7 && pclk>>(br+1) > want {
		br++
	}
	return br
}

// SetBaudBits changes CR1.BR, for example after the system clock changed.
func (b *Bus) SetBaudBits(br uint32) {
	r := b.Regs
	b.wait(srBSY, 0)
	r.CR1.ClearBits(cr1SPE)
	r.CR1.Set(r.CR1.Get()&^(7<<cr1BRPos) | uint16(br)<<cr1BRPos)
	r.CR1.SetBits(cr1SPE)
}

// dr8 is DR as an 8-bit register, see the package comment.
func (b *Bus) dr8() *volatile.Register8 {
	return (*volatile.Register8)(unsafe.Pointer(&b.Regs.DR))
}

func (b *Bus) wait(mask, want uint16) bool {
	for i := 0; i < 100000; i++ {
		if b.Regs.SR.Get()&mask == want {
			return true
		}
	}
	return false
}

// Transfer sends one byte and returns the byte received at the same time.
func (b *Bus) Transfer(w byte) (byte, error) {
	if !b.wait(srTXE, srTXE) {
		return 0, ErrTimeout
	}
	dr := b.dr8()
	dr.Set(w)
	if !b.wait(srRXNE, srRXNE) {
		return 0, ErrTimeout
	}
	return dr.Get(), nil
}

// Tx sends w and receives into r at the same time. Either may be nil; when
// both are set they must have the same length. It keeps the transmit FIFO
// one frame ahead of the receive side, so SCK runs with few gaps.
func (b *Bus) Tx(w, r []byte) error {
	n := len(w)
	if n == 0 {
		n = len(r)
	} else if r != nil && len(r) != n {
		return errLength
	}
	dr := b.dr8()
	sr := &b.Regs.SR
	sent, got := 0, 0
	for got < n {
		// At most two frames in flight, so the RX FIFO cannot overflow.
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
	if !b.wait(srBSY, 0) {
		return ErrTimeout
	}
	return nil
}
