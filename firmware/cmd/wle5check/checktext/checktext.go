// Package checktext holds what cmd/wle5check can decide and print without a
// radio: the console line format and the pass/fail rules for the values read
// from the chip. It is separate from main only so the host can test it (main
// builds for the WLE5 target alone).
//
// Line format (CR LF ended; one line per check; the last line is the summary):
//
//	wle5check start cycle=<n> cpu_hz=<hz>
//	wle5check <item> PASS|FAIL <key>=<value> ...
//	wle5check PASS|FAIL pass=<n> fail=<n>
//
// tools/wle5_uart_check.py reads exactly this; keep Names in step with it.
package checktext

// Prefix starts every line.
const Prefix = "wle5check "

// Names are the checks one pass runs, in order. The Python assertion expects
// each one to be PASS.
var Names = [...]string{"busy", "status", "tcxo", "sync", "errors", "calib", "rssi"}

// Chip facts (RM0461 Rev 11: 4.8.5 Get_Status, Get_Error, Get_RssiInst; the
// LSYNCR register table).
const (
	ModeStandbyRC = 2
	ModeRx        = 5

	// SUBGHZ_LSYNCRH/L reset value 0x1424, the private-network sync word.
	SyncHi = 0x14
	SyncLo = 0x24

	ErrCalibMask = 0x001F // RC64K, RC13M, PLL, ADC, image calibration
	ErrXoscStart = 1 << 5
	ErrPLLLock   = 1 << 6

	// BUSY must fall within 100 ms of the reset (a reset takes about 4 ms).
	BusyLowMaxUs = 100_000

	// Get_RssiInst: power = -raw/2 dBm. Louder than -20 dBm is no quiet
	// channel; 255 (-127.5 dBm) is the register's floor, nothing measured.
	RSSIMinRaw = 40
	RSSIMaxRaw = 250

	cmdStatusTimeout = 3
	cmdStatusInvalid = 4
	cmdStatusFailed  = 5
)

// StatusMode is Get_Status bits 6:4, the operating mode.
func StatusMode(b byte) uint8 { return (b >> 4) & 7 }

// StatusCmd is Get_Status bits 3:1, the status of the last command.
func StatusCmd(b byte) uint8 { return (b >> 1) & 7 }

// StatusOK: bit 0 is clear (0xFF, a floating MISO, fails here), the mode is
// want, and the last command did not time out, get rejected or fail. Bit 7 is
// not looked at: the SX1262 returns it set (0xA2 in standby, 0xD2 in receive,
// see radio/sx126x xfer), and the manual calls it reserved.
func StatusOK(b byte, want uint8) bool {
	if b&0x01 != 0 || StatusMode(b) != want {
		return false
	}
	c := StatusCmd(b)
	return c != cmdStatusTimeout && c != cmdStatusInvalid && c != cmdStatusFailed
}

// ModeOK: bit 0 is clear and the mode is want; the command status is not
// looked at. For Get_Status right after a reset: its command status keeps the
// radio's own boot-time calibration failure (DS_SX1261_2 9.2.1: "when the
// 32 MHz clock is coming from a TCXO, the calibration fails", 13.3.6:
// XOSC_START_ERR is expected at POR with a TCXO) and Get_Status does not clear
// it (bench 2026-11-06: 0xaa twice, cleared by Init's commands).
func ModeOK(b byte, want uint8) bool { return b&0x01 == 0 && StatusMode(b) == want }

// BusyOK: BUSY fell after the reset, in time, and the radio reset flag cleared.
func BusyOK(lowUs int64, fell, resetTimedOut bool) bool {
	return fell && !resetTimedOut && lowUs >= 0 && lowUs <= BusyLowMaxUs
}

// SyncOK: the LoRa sync word register reads 0x1424.
func SyncOK(hi, lo byte) bool { return hi == SyncHi && lo == SyncLo }

// TCXOOK: Init (reset, Set_TcxoMode, the AfterTCXO hook, calibration, receive)
// went through, the radio is now in receive (which needs HSE32, so the TCXO
// runs) and reports neither a clock start error nor a PLL lock error.
func TCXOOK(initOK bool, status byte, errs uint16) bool {
	return initOK && StatusOK(status, ModeRx) && errs&(ErrXoscStart|ErrPLLLock) == 0
}

// CalibOK: none of the five calibrations reports an error.
func CalibOK(errs uint16) bool { return errs&ErrCalibMask == 0 }

// ErrorsOK: Get_Error is empty.
func ErrorsOK(errs uint16) bool { return errs == 0 }

// RSSIOK: Get_RssiInst reads a plausible noise floor in receive mode, not
// saturated at either end.
func RSSIOK(raw byte, status byte) bool {
	return StatusMode(status) == ModeRx && raw >= RSSIMinRaw && raw <= RSSIMaxRaw
}

// MaxLen is the size of Writer's buffer, well above the longest line.
const MaxLen = 256

// Writer builds the lines in one fixed buffer and counts the verdicts of a
// pass. It does not allocate; a line that would not fit is cut.
type Writer struct {
	buf        [MaxLen]byte
	n          int
	Pass, Fail int
}

// NewPass clears the counts for the next pass.
func (w *Writer) NewPass() { w.Pass, w.Fail = 0, 0 }

func (w *Writer) str(s string) {
	if w.n+len(s) > len(w.buf) {
		return
	}
	w.n += copy(w.buf[w.n:], s)
}

func (w *Writer) put(b []byte) {
	if w.n+len(b) > len(w.buf) {
		return
	}
	w.n += copy(w.buf[w.n:], b)
}

func (w *Writer) key(k string) {
	w.str(" ")
	w.str(k)
	w.str("=")
}

func (w *Writer) end() []byte {
	// CR LF always fits: reserve it by cutting the line if needed.
	if w.n > len(w.buf)-2 {
		w.n = len(w.buf) - 2
	}
	w.buf[w.n], w.buf[w.n+1] = '\r', '\n'
	w.n += 2
	return w.buf[:w.n]
}

// Start is the first line of a pass.
func (w *Writer) Start(cycle uint32, cpuHz uint32) []byte {
	w.n = 0
	w.str(Prefix)
	w.str("start")
	w.Dec("cycle", int64(cycle))
	w.Dec("cpu_hz", int64(cpuHz))
	return w.end()
}

// Begin starts the line of one check and counts its verdict. Add the values
// with Dec, Hex, Half, Str, Bool, Errors, then End.
func (w *Writer) Begin(name string, ok bool) {
	w.n = 0
	w.str(Prefix)
	w.str(name)
	if ok {
		w.Pass++
		w.str(" PASS")
	} else {
		w.Fail++
		w.str(" FAIL")
	}
}

// End finishes the line and returns it (valid until the next Begin).
func (w *Writer) End() []byte { return w.end() }

// Summary is the last line of a pass: PASS only when nothing failed and
// something ran.
func (w *Writer) Summary() []byte {
	w.n = 0
	w.str(Prefix)
	if w.Fail == 0 && w.Pass > 0 {
		w.str("PASS")
	} else {
		w.str("FAIL")
	}
	w.Dec("pass", int64(w.Pass))
	w.Dec("fail", int64(w.Fail))
	return w.end()
}

// decimal writes u in decimal at the end of d and returns the start.
func decimal(d *[20]byte, u uint64) int {
	i := len(d)
	for {
		i--
		d[i] = byte('0' + u%10)
		u /= 10
		if u == 0 {
			return i
		}
	}
}

// Dec adds " k=<decimal>".
func (w *Writer) Dec(k string, v int64) {
	w.key(k)
	var d [20]byte
	u := uint64(v)
	if v < 0 {
		w.str("-")
		u = -u
	}
	w.put(d[decimal(&d, u):])
}

const hexDigits = "0123456789abcdef"

// Hex adds " k=0x<digits hex digits>".
func (w *Writer) Hex(k string, v uint32, digits int) {
	w.key(k)
	w.str("0x")
	for i := digits - 1; i >= 0; i-- {
		d := (v >> (4 * uint(i))) & 0xF
		w.str(hexDigits[d : d+1])
	}
}

// Str adds " k=v"; v must not contain spaces.
func (w *Writer) Str(k, v string) {
	w.key(k)
	w.str(v)
}

// Bool adds " k=true|false".
func (w *Writer) Bool(k string, v bool) {
	if v {
		w.Str(k, "true")
	} else {
		w.Str(k, "false")
	}
}

// Half adds " k=<dBm>" for a Get_RssiInst byte: the power is -raw/2 dBm.
func (w *Writer) Half(k string, raw byte) {
	w.key(k)
	w.str("-")
	var d [20]byte
	w.put(d[decimal(&d, uint64(raw/2)):])
	if raw&1 != 0 {
		w.str(".5")
	}
}

var errNames = [...]string{"RC64K", "RC13M", "PLLCAL", "ADCCAL", "IMGCAL", "XOSC", "PLLLOCK", "", "PARAMP"}

// Errors adds " k=<names>" for a Get_Error word: the set bits joined with
// '|', "none" when empty, "?" for bits the manual reserves.
func (w *Writer) Errors(k string, errs uint16) {
	w.key(k)
	if errs == 0 {
		w.str("none")
		return
	}
	first := true
	for bit := uint(0); bit < 16; bit++ {
		if errs&(1<<bit) == 0 {
			continue
		}
		if !first {
			w.str("|")
		}
		first = false
		if bit < uint(len(errNames)) && errNames[bit] != "" {
			w.str(errNames[bit])
		} else {
			w.str("?")
		}
	}
}

// Open starts a line that is not a check: prefix (without the trailing space)
// and nothing else. Add values, then End. It is for cmd/wle5probe, which
// prints raw readings and no verdicts; the counts do not move.
func (w *Writer) Open(prefix string) {
	w.n = 0
	w.str(prefix)
}
