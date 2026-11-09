// Package safety holds the numbers and rules that keep a WLE5 bench program
// from transmitting what it should not: the power ceiling, the antenna
// confirmation and the watchdog timing. It is separate from board/wle5 only so
// the host can test it (that package builds for the WLE5 target alone).
package safety

const (
	// MaxPowerDBm is the board layer's ceiling while the first transmissions
	// are made with an antenna or a 50 ohm load by hand (2026-11-06).
	// board/wle5 applies it to every power the driver
	// sends; building with the tag wle5_allow_high_power lifts it to ChipMaxDBm.
	MaxPowerDBm = 10
	// ChipMaxDBm is the most the high-power PA table sets (RM0461 Set_TxParams).
	ChipMaxDBm = 22
	// ChipMinDBm is the least it sets (0xF7).
	ChipMinDBm = -9
)

// ClampPower limits dbm to the chip's range and, unless allowHigh, to
// MaxPowerDBm.
func ClampPower(dbm int8, allowHigh bool) int8 {
	top := int8(MaxPowerDBm)
	if allowHigh {
		top = ChipMaxDBm
	}
	return min(max(dbm, ChipMinDBm), top)
}

// AntennaConfirmed tells whether the build variable WLE5_ANT_OK (linked in as
// a string) says that an antenna or a 50 ohm load is on the antenna port.
// Only the exact text "1" counts; empty, "0", "yes" and anything else do not.
func AntennaConfirmed(s string) bool { return s == "1" }

// IWDG timing (RM0461, independent watchdog): the counter runs from the LSI,
// nominally 32 kHz (its error is several percent, DS13105), divided by 256.
const (
	// WatchdogPrescaler is IWDG_PR for /256.
	WatchdogPrescaler = 6
	lsiHz             = 32000
	prescale          = 256
	// WatchdogMaxCount is the reload register's range (12 bits).
	WatchdogMaxCount = 0xFFF
)

// WatchdogReload returns IWDG_RLR for a timeout of about ms milliseconds: at
// least 1 count, at most WatchdogMaxCount (about 32 s). An 8 ms step.
func WatchdogReload(ms uint32) uint32 {
	n := ms * (lsiHz / 1000) / prescale
	return min(max(n, 1), WatchdogMaxCount)
}

// CarrierHardCapSeconds is the longest a carrier of the given length may be
// on the air before the program cuts the radio's power by resetting it,
// whatever the driver says: the length plus a margin for the driver's own
// end-of-carrier handling.
func CarrierHardCapSeconds(seconds int) int { return seconds + 2 }
