// Package cwpower decides the output power of cmd/wle5cw without a radio: the
// build variable WLE5_CW_DBM (a string linked in with -X), its default and the
// hard ceiling. It is separate from main only so the host can test it (main
// builds for the WLE5 target alone).
package cwpower

import (
	"errors"

	"cairnmesh/firmware/board/wle5/safety"
)

const (
	// Default is used when WLE5_CW_DBM is not set.
	Default = 10
	// Max is the hard ceiling for a first transmission (antenna or dummy load
	// connected, at most 10 dBm).
	// Raising it is a code change on purpose: it cannot be done from the
	// command line.
	Max = safety.MaxPowerDBm
	// Min is the lowest power the high-power PA table sets (RM0461
	// Set_TxParams: 0xF7 = -9 dBm).
	Min = -9
)

// ErrNotNumber is returned for a value that is not a whole number of dBm.
var ErrNotNumber = errors.New("not a whole number of dBm")

// ErrTooHigh is returned for a value above Max.
var ErrTooHigh = errors.New("above the 10 dBm ceiling")

// ErrTooLow is returned for a value below Min.
var ErrTooLow = errors.New("below -9 dBm")

// Parse turns the linked-in string into dBm. "" is Default. A value above
// Max, below Min or not a number is refused with the error; the caller must
// not transmit.
func Parse(s string) (int8, error) {
	if s == "" {
		return Default, nil
	}
	neg := false
	i := 0
	if s[0] == '-' || s[0] == '+' {
		neg = s[0] == '-'
		i = 1
	}
	if i == len(s) {
		return 0, ErrNotNumber
	}
	v := 0
	for ; i < len(s); i++ {
		c := s[i]
		if c < '0' || c > '9' {
			return 0, ErrNotNumber
		}
		v = v*10 + int(c-'0')
		if v > 1000 { // stop before it can overflow; far outside the range either way
			if neg {
				return 0, ErrTooLow
			}
			return 0, ErrTooHigh
		}
	}
	if neg {
		v = -v
	}
	switch {
	case v > Max:
		return 0, ErrTooHigh
	case v < Min:
		return 0, ErrTooLow
	}
	return int8(v), nil
}
