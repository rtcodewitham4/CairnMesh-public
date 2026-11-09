//go:build stm32u083

// Package usbhw drives the USB full-speed device controller of the
// STM32U083 for package usb (it implements usb.Port). TinyGo has no USB
// driver for any STM32. Polled from the main loop: Poll hands the bus
// events to the usb.Device.
//
// Bring-up (the first attempt missed the first and last):
// HSI48 on and selected by RCC_CCIPR.CLK48SEL = 11, CRS trimming it to the
// host's SOF, PWR_CR2.USV (VDDUSB is isolated after reset). The APB clock
// must be at least 12 MHz (RM0503 37.4): run the core at 48 MHz.
//
// Packet memory (USB SRAM, 1 KB at 0x40009800, 32-bit accesses only): the
// buffer descriptor table at 0, then 64-byte buffers. Errata ES0602:
// 2.12.4, the CTR interrupt comes before the last SRAM write of an OUT:
// wait about 1 µs before reading; 2.12.3, the CRC16 written after the data
// is wrong: never read past the count. See RM0503 chapter 37.
package usbhw

import (
	"device/stm32"
	"runtime/volatile"
	"unsafe"

	"cairnmesh/firmware/usb"
)

const (
	pmaBase = 0x40009800

	// Buffer offsets in the packet memory.
	ep0Tx = 0x040
	ep0Rx = 0x080
	ep1Tx = 0x0C0
	ep1Rx = 0x100
	ep2Tx = 0x140

	// RX buffer size field: BLSIZE = 1, NUM_BLOCK = 1: 64 bytes.
	rx64 = 1<<31 | 1<<26
)

// USB_CHEPnR bits (RM0503 37.6.7).
const (
	chepEA        = 0x000F
	chepStatTX    = 0x0030
	chepDtogTX    = 0x0040
	chepVTTX      = 0x0080
	chepKind      = 0x0100
	chepUType     = 0x0600
	chepSetup     = 0x0800
	chepStatRX    = 0x3000
	chepDtogRX    = 0x4000
	chepVTRX      = 0x8000
	chepRW        = chepEA | chepKind | chepUType // written as they are
	chepInvariant = chepVTRX | chepVTTX           // rc_w0: writing 1 keeps them

	statDisabled = 0
	statStall    = 1
	statNAK      = 2
	statValid    = 3

	typeBulk      = 0 << 9
	typeControl   = 1 << 9
	typeInterrupt = 3 << 9
)

// Controller is the USB controller; it implements usb.Port.
type Controller struct {
	buf [usb.MaxPacket]byte
	// Events seen.
	Resets, Setups, Outs, Ins uint32
}

func chep(n byte) *volatile.Register32 {
	return (*volatile.Register32)(unsafe.Pointer(uintptr(unsafe.Pointer(&stm32.USB.CHEP0R)) + uintptr(n)*4))
}

func pmaWord(off uintptr) *volatile.Register32 {
	return (*volatile.Register32)(unsafe.Pointer(pmaBase + off))
}

func pmaWrite(off uintptr, data []byte) {
	for i := 0; i < len(data); i += 4 {
		var w uint32
		for j := 0; j < 4 && i+j < len(data); j++ {
			w |= uint32(data[i+j]) << (8 * j)
		}
		pmaWord(off + uintptr(i)).Set(w)
	}
}

func pmaRead(off uintptr, dst []byte) {
	for i := 0; i < len(dst); i += 4 {
		w := pmaWord(off + uintptr(i)).Get()
		for j := 0; j < 4 && i+j < len(dst); j++ {
			dst[i+j] = byte(w >> (8 * j))
		}
	}
}

// setStat changes STATTX or STATRX (toggle-on-write-1 bits) to stat,
// keeping everything else.
func setStat(n byte, mask uint32, shift uint, stat uint32) {
	r := chep(n).Get()
	chep(n).Set(r&chepRW | chepInvariant | (r&mask ^ stat<<shift))
}

// initEndpoint sets an endpoint's address and type, its data toggles to
// DATA0 and its states.
func initEndpoint(n byte, utype, tx, rx uint32) {
	r := chep(n).Get()
	w := uint32(n) | utype | chepInvariant
	w |= r & (chepDtogTX | chepDtogRX) // writing the current value toggles them to 0
	w |= r&chepStatTX ^ tx<<4
	w |= r&chepStatRX ^ rx<<12
	chep(n).Set(w)
}

// Init clocks the controller, sets the packet memory up and attaches to
// the bus (D+ pull-up). The core must run at 48 MHz already.
func (c *Controller) Init() bool {
	rcc := stm32.RCC
	rcc.CRRCR.SetBits(stm32.RCC_CRRCR_HSI48ON)
	for i := 0; !rcc.CRRCR.HasBits(stm32.RCC_CRRCR_HSI48RDY); i++ {
		if i > 1000000 {
			return false
		}
	}
	rcc.CCIPR.ReplaceBits(3, stm32.RCC_CCIPR_CLK48SEL_Msk>>stm32.RCC_CCIPR_CLK48SEL_Pos, stm32.RCC_CCIPR_CLK48SEL_Pos)
	rcc.APBENR1.SetBits(stm32.RCC_APBENR1_CRSEN | stm32.RCC_APBENR1_PWREN | stm32.RCC_APBENR1_USBEN)
	_ = rcc.APBENR1.Get()
	// CRS: its reset configuration already syncs on the USB SOF, 48 MHz / 1 kHz.
	stm32.CRS.CR.SetBits(stm32.CRS_CR_AUTOTRIMEN | stm32.CRS_CR_CEN)
	stm32.PWR.CR2.SetBits(stm32.PWR_CR2_USV)

	rcc.APBRSTR1.SetBits(stm32.RCC_APBRSTR1_USBRST)
	rcc.APBRSTR1.ClearBits(stm32.RCC_APBRSTR1_USBRST)
	stm32.USB.CNTR.Set(stm32.USB_CNTR_USBRST) // power up, still in reset
	spin(100)                                 // tSTARTUP: 1 µs
	stm32.USB.CNTR.Set(0)
	stm32.USB.ISTR.Set(0)

	// Buffer descriptors: TX address (count set on send), RX address and size.
	pmaWord(0x00).Set(ep0Tx)
	pmaWord(0x04).Set(ep0Rx | rx64)
	pmaWord(0x08).Set(ep1Tx)
	pmaWord(0x0C).Set(ep1Rx | rx64)
	pmaWord(0x10).Set(ep2Tx)
	pmaWord(0x14).Set(0)

	stm32.USB.SetBCDR_DPPU_DPD(1)
	return true
}

// spin waits at least about n × 10 ns at 48 MHz (an APB read each).
func spin(n int) {
	for i := 0; i < n; i++ {
		_ = stm32.USB.FNR.Get()
	}
}

// Poll handles what happened on the bus since the last call.
func (c *Controller) Poll(d *usb.Device) {
	istr := stm32.USB.ISTR.Get()
	if istr&stm32.USB_ISTR_RST_DCON != 0 {
		stm32.USB.ISTR.Set(^uint32(stm32.USB_ISTR_RST_DCON))
		c.Resets++
		initEndpoint(0, typeControl, statNAK, statValid)
		stm32.USB.DADDR.Set(stm32.USB_DADDR_EF) // address 0
		d.Reset()
		return
	}
	// Bus states the console does not act on.
	if other := istr & (stm32.USB_ISTR_SUSP | stm32.USB_ISTR_WKUP | stm32.USB_ISTR_ERR |
		stm32.USB_ISTR_SOF | stm32.USB_ISTR_ESOF | stm32.USB_ISTR_PMAOVR); other != 0 {
		stm32.USB.ISTR.Set(^other)
	}
	for i := 0; i < 8 && istr&stm32.USB_ISTR_CTR != 0; i++ {
		n := byte(istr & 0xF)
		r := chep(n).Get()
		if r&chepVTRX != 0 {
			chep(n).Set(r&chepRW | chepVTTX) // clear VTRX only
			spin(100)                        // ES0602 2.12.4
			if n == 0 && r&chepSetup != 0 {
				c.Setups++
				pmaRead(ep0Rx, c.buf[:8])
				d.Setup(c.buf[:8])
			} else {
				c.Outs++
				count := int(pmaWord(uintptr(n)*8+4).Get()>>16) & 0x3FF
				count = min(count, len(c.buf))
				pmaRead(rxBuffer(n), c.buf[:count])
				d.Received(n, c.buf[:count])
			}
		}
		if r&chepVTTX != 0 {
			chep(n).Set(r&chepRW | chepVTRX) // clear VTTX only
			c.Ins++
			d.Sent(n)
		}
		istr = stm32.USB.ISTR.Get()
	}
}

func rxBuffer(n byte) uintptr {
	if n == 0 {
		return ep0Rx
	}
	return ep1Rx
}

func txBuffer(n byte) uintptr {
	switch n {
	case 0:
		return ep0Tx
	case 1:
		return ep1Tx
	}
	return ep2Tx
}

// SetAddress applies the device address.
func (c *Controller) SetAddress(addr byte) {
	stm32.USB.DADDR.Set(stm32.USB_DADDR_EF | uint32(addr))
}

// OpenEndpoints enables EP1 (bulk) and EP2 (interrupt IN), NAKing until armed.
func (c *Controller) OpenEndpoints() {
	initEndpoint(1, typeBulk, statNAK, statNAK)
	initEndpoint(2, typeInterrupt, statNAK, statDisabled)
}

// Send arms one IN packet.
func (c *Controller) Send(ep byte, data []byte) {
	pmaWrite(txBuffer(ep), data)
	pmaWord(uintptr(ep) * 8).Set(uint32(txBuffer(ep)) | uint32(len(data))<<16)
	setStat(ep, chepStatTX, 4, statValid)
}

// Receive arms ep for the next OUT packet.
func (c *Controller) Receive(ep byte) {
	setStat(ep, chepStatRX, 12, statValid)
}

// StallControl stalls EP0 until the next SETUP.
func (c *Controller) StallControl() {
	r := chep(0).Get()
	chep(0).Set(r&chepRW | chepInvariant | (r&chepStatTX ^ statStall<<4) | (r&chepStatRX ^ statStall<<12))
}

// Serial is the chip's 96-bit unique ID in hexadecimal (24 characters),
// for the USB serial number, written into dst.
func Serial(dst *[24]byte) string {
	const hex = "0123456789ABCDEF"
	for w := 0; w < 3; w++ {
		v := (*volatile.Register32)(unsafe.Pointer(uintptr(0x1FFF6E50 + 4*w))).Get()
		for i := 0; i < 8; i++ {
			dst[w*8+i] = hex[v>>(28-4*i)&0xF]
		}
	}
	return unsafe.String(&dst[0], len(dst))
}
