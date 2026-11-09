package board

import (
	"device/stm32"
	"runtime/volatile"
	"unsafe"

	"cairnmesh/firmware/hw/flashhw"
	"cairnmesh/firmware/journal"
)

// Journal pages, below the key page: the device's duplicate records (spec
// 4.1, two banks of 8 pages) and the controller's replay mark (spec 5, two
// banks of 4 pages). A page takes about 10 000 erases and a journal lives
// as long as its pages are many (README). The image must end below
// RecordsPage (Makefile FLASH_LIMIT).
const (
	RecordsPage = 101 // 101-116
	RecordsBank = 8
	MarksPage   = 117 // 117-124
	MarksBank   = 4
)

// Journals places the two journals in their pages (Load reads them).
func Journals() (*journal.Records, *journal.Marks) {
	return journal.NewRecords(journal.Where{Flash: Flash{}, First: RecordsPage, Pages: RecordsBank}),
		journal.NewMarks(journal.Where{Flash: Flash{}, First: MarksPage, Pages: MarksBank})
}

// Flash is the chip's flash under a journal (journal.Flash).
type Flash struct{}

func (Flash) Erase(page int) bool { return flashhw.ErasePage(page) == nil }

func (f Flash) Program(page, offset int, dw *[8]byte) bool {
	if flashhw.Program(flashhw.PageAddr(page)+uintptr(offset), dw[:]) != nil {
		return false
	}
	var back [8]byte
	return f.Read(page, offset, &back) && back == *dw
}

// Read reports false when the double word failed its ECC (a write cut by
// a reset or power loss, RM0503 3.3.5): the NMI handler below caught it.
func (Flash) Read(page, offset int, dw *[8]byte) bool {
	eccFault.Set(0)
	addr := flashhw.PageAddr(page) + uintptr(offset)
	lo := volatile.LoadUint32((*uint32)(unsafe.Pointer(addr)))
	hi := volatile.LoadUint32((*uint32)(unsafe.Pointer(addr + 4)))
	*dw = [8]byte{byte(lo), byte(lo >> 8), byte(lo >> 16), byte(lo >> 24), byte(hi), byte(hi >> 8), byte(hi >> 16), byte(hi >> 24)}
	return eccFault.Get() == 0
}

// ECCFaults counts double ECC errors read from the flash since boot.
var ECCFaults uint32

var eccFault volatile.Register8

// A double ECC error raises an NMI (RM0503 3.3.2). Without this handler a
// double word torn by a power cut would stop the node at every boot; with
// it the read returns and the reader sees the fault. Any other NMI stops
// as before (TinyGo's default handler: an endless loop).
//
//export NMI_Handler
func nmiHandler() {
	if stm32.FLASH.ECCR.Get()&stm32.FLASH_ECCR_ECCD_Msk != 0 {
		stm32.FLASH.ECCR.Set(stm32.FLASH_ECCR_ECCD_Msk) // write 1 to clear
		eccFault.Set(1)
		ECCFaults++
		return
	}
	for {
	}
}
