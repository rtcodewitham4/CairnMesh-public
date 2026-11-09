package subghz

import "testing"

func TestBaudBits(t *testing.T) {
	for _, c := range []struct{ pclk, br uint32 }{
		{48_000_000, 1}, // 12 MHz: table 52
		{32_000_000, 0}, // 16 MHz: table 52
		{4_000_000, 0},  // the reset clock
		{64_000_000, 1}, // 16 MHz
		{96_000_000, 2},
	} {
		if got := BaudBits(c.pclk); got != c.br {
			t.Errorf("BaudBits(%d) = %d, want %d", c.pclk, got, c.br)
		}
		if sck := c.pclk >> (BaudBits(c.pclk) + 1); sck > MaxSCK {
			t.Errorf("pclk %d: SCK %d above the radio's limit", c.pclk, sck)
		}
	}
}

func TestBusyRiseReads(t *testing.T) {
	for _, c := range []struct {
		hz    uint32
		reads int
	}{
		{48_000_000, 10}, // 28.8 -> 29 cycles -> 10 reads of >= 3 cycles
		{32_000_000, 7},
		{4_000_000, 1},
		{64_000_000, 13},
	} {
		got := BusyRiseReads(c.hz)
		if got != c.reads {
			t.Errorf("BusyRiseReads(%d) = %d, want %d", c.hz, got, c.reads)
		}
		// The wait at the fewest cycles per read must reach BusyRiseNs.
		ns := uint64(got) * readCycles * 1_000_000_000 / uint64(c.hz)
		if ns < BusyRiseNs {
			t.Errorf("%d Hz: %d reads wait only %d ns", c.hz, got, ns)
		}
	}
}
