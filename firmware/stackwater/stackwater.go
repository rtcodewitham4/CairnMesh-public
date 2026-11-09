// Package stackwater measures the two stacks used by a TinyGo Cortex-M
// program. It is pure Go so the scanning rules can be tested on the host; the
// cairn_wle5-only file binds it to the linker symbols and active PSP.
package stackwater

const (
	// SystemStackSize is the MSP stack region reserved by cairn-wle5.ld.
	SystemStackSize = 4 * 1024
	// TaskStackSize is the goroutine stack size passed by the firmware build.
	TaskStackSize = 4 * 1024

	SystemFill = 0xA5A5A5A5
	TaskCanary = 0xB83BF575

	SystemStackWords = SystemStackSize / 4
	TaskStackWords   = TaskStackSize / 4
)

// Usage describes one downward-growing stack. Low is the lowest address known
// to have been changed (MSP) or the lowest non-zero word above the canary
// (goroutine task).
type Usage struct {
	Found    bool
	CanaryOK bool
	Base     uintptr
	Top      uintptr
	Low      uintptr
	Used     uint32
	Free     uint32
}

// ParseArgs accepts the read-only "stack" command without arguments.
func ParseArgs(args [][]byte) bool {
	return len(args) == 1
}

// ScanSystem scans an MSP image from low to high. Everything from the lowest
// word no longer equal to SystemFill through Top is counted as used.
func ScanSystem(words []uint32, base uintptr) Usage {
	top := base + uintptr(len(words))*4
	u := Usage{Found: len(words) != 0, CanaryOK: true, Base: base, Top: top, Low: top}
	if len(words) == 0 {
		return u
	}
	for i, word := range words {
		if word != SystemFill {
			u.Low = base + uintptr(i)*4
			u.Used = uint32((len(words) - i) * 4)
			u.Free = uint32(i * 4)
			return u
		}
	}
	u.Free = uint32(len(words) * 4)
	return u
}

// FindTaskCanary returns the highest canary address in a window that contains
// the active goroutine stack. Looking downward from the current PSP avoids an
// older task, whose canary must lie below a different 4 KiB task stack.
func FindTaskCanary(window []uint32) int {
	for i := len(window) - 1; i >= 0; i-- {
		if window[i] == TaskCanary {
			return i
		}
	}
	return -1
}

// ScanTask scans one complete goroutine stack. The first word is TinyGo's
// canary. Allocation zeroes the rest, so the lowest non-zero word above it is
// the stack watermark. The canary word is reserved and is not reported free.
func ScanTask(words []uint32, base uintptr) Usage {
	top := base + uintptr(len(words))*4
	u := Usage{Base: base, Top: top, Low: top}
	if len(words) == 0 || words[0] != TaskCanary {
		return u
	}
	u.Found, u.CanaryOK = true, true
	for i := 1; i < len(words); i++ {
		if words[i] != 0 {
			u.Low = base + uintptr(i)*4
			u.Used = uint32((len(words) - i) * 4)
			u.Free = uint32((i - 1) * 4)
			return u
		}
	}
	u.Free = uint32((len(words) - 1) * 4)
	return u
}
