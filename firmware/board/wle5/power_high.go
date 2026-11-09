//go:build cairn_wle5 && wle5_allow_high_power

package wle5

// allowHighPower: this build was made with the tag wle5_allow_high_power, so
// the board layer lets the chip's full range (to +22 dBm) through. Nothing in
// the Makefile sets the tag: it takes a hand-written tinygo command line, after
// an explicit decision to allow more than 10 dBm.
const allowHighPower = true
