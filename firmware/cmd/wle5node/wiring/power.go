package wiring

import (
	"errors"

	"cairnmesh/firmware/board/wle5/safety"
)

// DefaultPowerDBm is the node's transmit power when the build gives no
// WLE5_TX_DBM: 0 dBm (the E77 link tests run at 0 dBm or
// less). It used to be safety.MaxPowerDBm (10 dBm), written into main.
const DefaultPowerDBm = 0

// ErrPowerNotNumber is returned for a WLE5_TX_DBM that is not a whole number.
var ErrPowerNotNumber = errors.New("not a whole number of dBm")

// ErrPowerRange is returned for a WLE5_TX_DBM outside the chip's range
// (safety.ChipMinDBm .. safety.ChipMaxDBm).
var ErrPowerRange = errors.New("outside the chip's -9..22 dBm")

// ParsePower turns the linked-in WLE5_TX_DBM into dBm; "" is DefaultPowerDBm.
// It only checks the text and the chip's range. The ceiling is not here: it is
// the board layer's power cap (safety.ClampPower, 10 dBm unless the build has
// the tag wle5_allow_high_power), which InitRadio and every emission apply
// whatever main asks for; main shows the capped value (Effective).
func ParsePower(s string) (int8, error) {
	if s == "" {
		return DefaultPowerDBm, nil
	}
	neg, i := false, 0
	if s[0] == '-' || s[0] == '+' {
		neg = s[0] == '-'
		i = 1
	}
	if i == len(s) {
		return 0, ErrPowerNotNumber
	}
	v := 0
	for ; i < len(s); i++ {
		c := s[i]
		if c < '0' || c > '9' {
			return 0, ErrPowerNotNumber
		}
		if v = v*10 + int(c-'0'); v > 1000 {
			return 0, ErrPowerRange // far outside; stop before it can overflow
		}
	}
	if neg {
		v = -v
	}
	if v < safety.ChipMinDBm || v > safety.ChipMaxDBm {
		return 0, ErrPowerRange
	}
	return int8(v), nil
}

// PowerText writes dbm as "-9 dBm".
func PowerText(dbm int8) string {
	v := int(dbm)
	var b [8]byte
	i := len(b)
	neg := v < 0
	if neg {
		v = -v
	}
	for {
		i--
		b[i] = byte('0' + v%10)
		if v /= 10; v == 0 {
			break
		}
	}
	if neg {
		i--
		b[i] = '-'
	}
	return string(b[i:]) + " dBm"
}

// TxText is the transmit state as the console shows it, in the banner and in
// status: "tx ON 0 dBm" or "tx OFF (no WLE5_ANT_OK=1) 0 dBm configured". With
// capped (the board's cap lowered the request) it says what was asked.
func TxText(allowed bool, effective, requested int8) string {
	var s string
	if allowed {
		s = "tx ON " + PowerText(effective)
	} else {
		s = "tx OFF (no WLE5_ANT_OK=1) " + PowerText(effective) + " configured"
	}
	if requested != effective {
		s += " (requested " + PowerText(requested) + ", capped by the board)"
	}
	return s
}
