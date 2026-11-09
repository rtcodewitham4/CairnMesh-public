// Package linktext holds what cmd/wle5link can decide and print without a
// radio: the test frame, the console line format, the transmit schedule and the
// outcome of a transmission. It is separate from main only so the host can
// test it (main builds for the WLE5 target alone).
//
// Nothing here knows a pin: the board layer (board/wle5) owns the RF switch,
// the LEDs and the console pins, and the radio parameters come from main.
//
// Line format (CR LF ended; tools and the bench read these exactly):
//
//	wle5link: radio ready ...                    start-up lines, free text
//	wle5link: rx n=<len> rssi=<dBm> snr=<dB> hex=<first 16 bytes, lower case>
//	wle5link: tx ok
//	wle5link: sum rx=<n> crc_err=<n> tx=<n>
package linktext

import "strconv"

// Prefix starts every line.
const Prefix = "wle5link: "

// Frame is the test frame of cmd/radiolink (the U083 bench's first link test):
// the 16 bytes "CAIRNMESH-BENCH-" and one sequence byte, 17 bytes. No new field.
const FrameLen = 17

var tag = [16]byte{'C', 'A', 'I', 'R', 'N', 'M', 'E', 'S', 'H', '-', 'B', 'E', 'N', 'C', 'H', '-'}

// Frame writes the test frame with sequence number seq into dst.
func Frame(dst *[FrameLen]byte, seq byte) {
	copy(dst[:], tag[:])
	dst[16] = seq
}

// IsTestFrame reports whether p is a test frame of cmd/radiolink (any sequence).
func IsTestFrame(p []byte) bool {
	return len(p) == FrameLen && [16]byte(p[:16]) == tag
}

// HexBytes is how many payload bytes the rx line shows.
const HexBytes = 16

const hexDigits = "0123456789abcdef"

// AppendRx appends the rx line (without line end) to b.
func AppendRx(b []byte, n int, rssi int16, snr int8, p []byte) []byte {
	b = append(b, Prefix+"rx n="...)
	b = strconv.AppendInt(b, int64(n), 10)
	b = append(b, " rssi="...)
	b = strconv.AppendInt(b, int64(rssi), 10)
	b = append(b, " snr="...)
	b = strconv.AppendInt(b, int64(snr), 10)
	b = append(b, " hex="...)
	for i := 0; i < len(p) && i < HexBytes; i++ {
		b = append(b, hexDigits[p[i]>>4], hexDigits[p[i]&15])
	}
	return b
}

// AppendTxOK appends the line of a finished transmission.
func AppendTxOK(b []byte) []byte { return append(b, Prefix+"tx ok"...) }

// AppendTxFail appends the line of a transmission that did not finish
// (why: "refused" the radio would not start it, "timeout" the chip's own timeout).
func AppendTxFail(b []byte, why string) []byte {
	b = append(b, Prefix+"tx "...)
	return append(b, why...)
}

// AppendSum appends the summary line.
func AppendSum(b []byte, rx, crcErr, tx uint32) []byte {
	b = append(b, Prefix+"sum rx="...)
	b = strconv.AppendUint(b, uint64(rx), 10)
	b = append(b, " crc_err="...)
	b = strconv.AppendUint(b, uint64(crcErr), 10)
	b = append(b, " tx="...)
	b = strconv.AppendUint(b, uint64(tx), 10)
	return b
}

// Period limits for the transmit interval, in seconds.
const (
	DefaultPeriodSec = 5
	MaxPeriodSec     = 3600
)

// ParsePeriod turns the linked-in string into seconds: "" is the default;
// anything that is not a whole number in 1..MaxPeriodSec is refused (ok false).
func ParsePeriod(s string) (sec int, ok bool) {
	if s == "" {
		return DefaultPeriodSec, true
	}
	v := 0
	for i := 0; i < len(s); i++ {
		if s[i] < '0' || s[i] > '9' {
			return 0, false
		}
		v = v*10 + int(s[i]-'0')
		if v > MaxPeriodSec {
			return 0, false
		}
	}
	if v < 1 {
		return 0, false
	}
	return v, true
}

// Schedule says when the next test frame is due. Times are any monotonic
// microsecond clock.
type Schedule struct {
	PeriodUs int64
	next     int64
}

// Start sets the first frame due at now + delayUs.
func (s *Schedule) Start(now, delayUs int64) { s.next = now + delayUs }

// Due reports whether a frame is due at now.
func (s *Schedule) Due(now int64) bool { return now >= s.next }

// Sent records that a frame was started at now: the next one is one period
// after the previous due time, or after now if the schedule fell behind by a
// whole period or more (no burst to catch up).
func (s *Schedule) Sent(now int64) {
	s.next += s.PeriodUs
	if s.next <= now {
		s.next = now + s.PeriodUs
	}
}

// Outcome is what a transmission came to.
type Outcome uint8

const (
	// Pending: nothing sent, or still on air.
	Pending Outcome = iota
	// Done: the radio reported TxDone.
	Done
	// Timeout: the chip's own transmit timeout ended it.
	Timeout
)

// Tracker follows one transmission through the radio's counters.
type Tracker struct {
	waiting        bool
	done, timeouts uint32
}

// Start notes the counters when a transmission has been started.
func (t *Tracker) Start(txDone, txTimeouts uint32) {
	t.waiting, t.done, t.timeouts = true, txDone, txTimeouts
}

// Waiting reports whether a transmission is on air.
func (t *Tracker) Waiting() bool { return t.waiting }

// Update looks at the counters; it returns Done or Timeout once per
// transmission.
func (t *Tracker) Update(txDone, txTimeouts uint32) Outcome {
	if !t.waiting {
		return Pending
	}
	switch {
	case txDone != t.done:
		t.waiting = false
		return Done
	case txTimeouts != t.timeouts:
		t.waiting = false
		return Timeout
	}
	return Pending
}
