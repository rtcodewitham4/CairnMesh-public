//go:build cairn_wle5

package stackwater

import (
	"device/arm"
	"unsafe"
)

const (
	wle5RAMStart = 0x20000000
	wle5RAMEnd   = 0x20010000
)

//go:extern _stack_start
var stackStartSymbol [0]byte

//go:extern _stack_top
var stackTopSymbol [0]byte

// SampleSystem scans the linker's .stack region, the MSP used by the TinyGo
// scheduler and all interrupts.
func SampleSystem() Usage {
	base := uintptr(unsafe.Pointer(&stackStartSymbol))
	top := uintptr(unsafe.Pointer(&stackTopSymbol))
	if top < base || top-base != SystemStackSize {
		return Usage{}
	}
	words := unsafe.Slice((*uint32)(unsafe.Pointer(base)), int((top-base)/4))
	return ScanSystem(words, base)
}

// SampleTask scans the active goroutine stack. In thread mode the active SP is
// PSP; interrupts continue to use MSP. The current PSP is within the task's
// stack at or below its 4 KiB top.
func SampleTask() Usage {
	psp := uintptr(arm.AsmFull("mov {}, sp", nil)) &^ 3
	if psp < wle5RAMStart+4 || psp > wle5RAMEnd {
		return Usage{}
	}
	windowBase := (psp - TaskStackSize) &^ 3
	if windowBase < wle5RAMStart {
		return Usage{}
	}
	window := unsafe.Slice((*uint32)(unsafe.Pointer(windowBase)), int((psp-windowBase)/4))
	canary := FindTaskCanary(window)
	if canary < 0 {
		return Usage{}
	}
	base := windowBase + uintptr(canary)*4
	if base+TaskStackSize > wle5RAMEnd {
		return Usage{}
	}
	words := unsafe.Slice((*uint32)(unsafe.Pointer(base)), TaskStackWords)
	return ScanTask(words, base)
}
