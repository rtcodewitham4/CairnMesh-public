package subghz

// MaxSCK is the highest SUBGHZSPI_SCK the radio takes (RM0461 6.2.13: PCLK3 / 2
// at most 16 MHz).
const MaxSCK = 16_000_000

// BaudBits returns SPI_CR1.BR for the fastest SCK = pclk3 / 2^(BR+1) that does
// not exceed MaxSCK: BR = 1 (/4, 12 MHz) at 48 MHz, 0 (/2, 16 MHz) at 32 MHz,
// RM0461 table 52.
func BaudBits(pclk3 uint32) uint32 {
	br := uint32(0)
	for br < 7 && pclk3>>(br+1) > MaxSCK {
		br++
	}
	return br
}

// BusyRiseNs is how long RFBUSY may take to rise after NSS rises (DS_SX1261_2
// 8.3.1: up to 600 ns). Until then Busy can read "not busy" for a command that
// is about to be busy, and Busy's AND rule (RFBUSYS && RFBUSYMS) has no mask
// to cover that gap, so NSS.Set(true) waits at least this long.
const BusyRiseNs = 600

// readCycles is the fewest CPU cycles one volatile PWR_SR2 read takes
// (Cortex-M4: a load from the peripheral bus plus the loop, at least 3).
const readCycles = 3

// BusyRiseReads returns how many PWR_SR2 reads take at least BusyRiseNs at a
// CPU clock of cpuHz, counting every read as the fewest cycles it can take, so
// the real wait is never shorter (a slower read only lengthens it).
func BusyRiseReads(cpuHz uint32) int {
	cycles := (uint64(cpuHz)*BusyRiseNs + 999_999_999) / 1_000_000_000
	return int((cycles + readCycles - 1) / readCycles)
}
