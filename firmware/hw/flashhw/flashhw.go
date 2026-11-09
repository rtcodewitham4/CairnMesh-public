// Package flashhw erases and programs pages of the STM32U083 flash memory,
// and keeps a persistent counter in two of them. TinyGo has no machine.Flash
// for the U0 family.
//
// The link protocol needs this: a controller that restarts its sequence or
// frame counter from 0 reuses nonces (AES-CCM) and collides with the
// device's dedup ring. The usual fix is to
// store the counter every N frames and to add N after a reset.
//
// Flash facts (RM0503, "Embedded flash memory"): 2 KB pages, programming in
// 64-bit double words that must be erased (all ones) before, about 10 000
// erase cycles per page. The CPU stalls while it fetches code from flash
// during an operation, so interrupts are delayed for that time.
package flashhw

import (
	"device/stm32"
	"errors"
	"runtime/interrupt"
	"runtime/volatile"
	"unsafe"
)

const (
	Base     = 0x08000000
	PageSize = 2048
	Pages    = 128

	key1 = 0x45670123
	key2 = 0xCDEF89AB

	crPG   = 1 << 0
	crPER  = 1 << 1
	crPNB  = 3
	crSTRT = 1 << 16
	crLOCK = 1 << 31

	srEOP    = 1 << 0
	srErrors = 0x3FA // OPERR, PROGERR, WRPERR, PGAERR, SIZERR, PGSERR, MISSERR, FASTERR
	srBSY1   = 1 << 16
	srCFGBSY = 1 << 18
)

var (
	ErrFlash   = errors.New("flash: operation error")
	ErrAddress = errors.New("flash: address not aligned or out of range")
)

// PageAddr returns the address of a page.
func PageAddr(page int) uintptr { return Base + uintptr(page)*PageSize }

func wait() error {
	f := stm32.FLASH
	for f.SR.HasBits(srBSY1 | srCFGBSY) {
	}
	if f.SR.Get()&srErrors != 0 {
		f.SR.Set(srErrors) // write 1 to clear
		return ErrFlash
	}
	return nil
}

func unlock() {
	f := stm32.FLASH
	if f.CR.HasBits(crLOCK) {
		f.KEYR.Set(key1)
		f.KEYR.Set(key2)
	}
}

func lock() { stm32.FLASH.CR.SetBits(crLOCK) }

// ErasePage erases one page. Never erase a page that holds the program.
func ErasePage(page int) error {
	if page < 0 || page >= Pages {
		return ErrAddress
	}
	f := stm32.FLASH
	unlock()
	defer lock()
	if err := wait(); err != nil {
		return err
	}
	f.SR.Set(srErrors | srEOP)
	f.CR.Set(crPER | uint32(page)<<crPNB)
	f.CR.SetBits(crSTRT)
	err := wait()
	f.CR.ClearBits(crPER)
	return err
}

// Program writes data (a multiple of 8 bytes) at addr, which must be 8-byte
// aligned and erased.
func Program(addr uintptr, data []byte) error {
	if addr%8 != 0 || len(data)%8 != 0 || addr < Base || addr+uintptr(len(data)) > Base+Pages*PageSize {
		return ErrAddress
	}
	f := stm32.FLASH
	unlock()
	defer lock()
	if err := wait(); err != nil {
		return err
	}
	f.SR.Set(srErrors | srEOP)
	f.CR.SetBits(crPG)
	var err error
	for i := 0; i < len(data); i += 8 {
		lo := uint32(data[i]) | uint32(data[i+1])<<8 | uint32(data[i+2])<<16 | uint32(data[i+3])<<24
		hi := uint32(data[i+4]) | uint32(data[i+5])<<8 | uint32(data[i+6])<<16 | uint32(data[i+7])<<24
		// The two words of a double word must be written back to back.
		mask := interrupt.Disable()
		volatile.StoreUint32((*uint32)(unsafe.Pointer(addr+uintptr(i))), lo)
		volatile.StoreUint32((*uint32)(unsafe.Pointer(addr+uintptr(i)+4)), hi)
		interrupt.Restore(mask)
		if err = wait(); err != nil {
			break
		}
	}
	f.CR.ClearBits(crPG)
	return err
}

func load32(addr uintptr) uint32 { return volatile.LoadUint32((*uint32)(unsafe.Pointer(addr))) }

// Counter is a 32-bit value that survives resets. It appends 8-byte records
// to one of two pages. When a page is full it writes the next record to the
// other page first and erases the full page after that, so a power loss at
// any point leaves at least one valid record.
//
// Record: word 0 = 0x5254 << 16 | ^value&0xFFFF (a check), word 1 = value.
type Counter struct {
	PageA, PageB int

	page int     // page with the newest record
	next uintptr // address of the next free record, 0 before the first Store
	val  uint32
}

const recordTag = 0x5254

func (c *Counter) scan(page int) (val uint32, free uintptr, ok bool) {
	base := PageAddr(page)
	for a := base; a < base+PageSize; a += 8 {
		w0, w1 := load32(a), load32(a+4)
		if w0 == 0xFFFFFFFF && w1 == 0xFFFFFFFF {
			return val, a, ok
		}
		if w0>>16 == recordTag && uint16(w0) == ^uint16(w1) {
			val, ok = w1, true
		}
	}
	return val, base + PageSize, ok
}

// Load finds the newest value. It returns 0 when both pages are empty.
func (c *Counter) Load() uint32 {
	va, fa, oka := c.scan(c.PageA)
	vb, fb, okb := c.scan(c.PageB)
	switch {
	case oka && (!okb || va >= vb):
		c.page, c.val, c.next = c.PageA, va, fa
	case okb:
		c.page, c.val, c.next = c.PageB, vb, fb
	default:
		c.page, c.val, c.next = c.PageA, 0, 0
	}
	return c.val
}

// Value returns the last loaded or stored value.
func (c *Counter) Value() uint32 { return c.val }

// Store appends v. Values must not decrease. Call Load once before.
func (c *Counter) Store(v uint32) error {
	if c.next != 0 && c.next < PageAddr(c.page)+PageSize {
		return c.write(v)
	}
	// No record yet, or the page is full: continue on the other page.
	old := c.page
	other := c.PageB
	if old == c.PageB {
		other = c.PageA
	}
	if load32(PageAddr(other)) != 0xFFFFFFFF {
		if err := ErasePage(other); err != nil {
			return err
		}
	}
	first := c.next == 0
	c.page, c.next = other, PageAddr(other)
	if err := c.write(v); err != nil {
		return err
	}
	if !first || load32(PageAddr(old)) != 0xFFFFFFFF {
		return ErasePage(old)
	}
	return nil
}

func (c *Counter) write(v uint32) error {
	w0 := uint32(recordTag)<<16 | uint32(^uint16(v))
	rec := [8]byte{byte(w0), byte(w0 >> 8), byte(w0 >> 16), byte(w0 >> 24), byte(v), byte(v >> 8), byte(v >> 16), byte(v >> 24)}
	if err := Program(c.next, rec[:]); err != nil {
		return err
	}
	c.next += 8
	c.val = v
	return nil
}
