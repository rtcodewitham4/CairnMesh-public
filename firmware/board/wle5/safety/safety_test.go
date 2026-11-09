package safety

import "testing"

func TestClampPower(t *testing.T) {
	for _, c := range []struct {
		in   int8
		high bool
		want int8
	}{
		{22, false, 10}, {11, false, 10}, {10, false, 10}, {0, false, 0}, {-9, false, -9},
		{-10, false, -9}, {-128, false, -9}, {127, false, 10},
		{22, true, 22}, {23, true, 22}, {10, true, 10}, {-20, true, -9},
	} {
		if got := ClampPower(c.in, c.high); got != c.want {
			t.Errorf("ClampPower(%d, %v) = %d, want %d", c.in, c.high, got, c.want)
		}
	}
	if MaxPowerDBm != 10 {
		t.Error("the first-transmission ceiling is 10 dBm")
	}
}

func TestAntennaConfirmed(t *testing.T) {
	if !AntennaConfirmed("1") {
		t.Error("1 must confirm")
	}
	for _, s := range []string{"", "0", "yes", "true", "11", " 1", "1 ", "01"} {
		if AntennaConfirmed(s) {
			t.Errorf("%q confirmed the antenna", s)
		}
	}
}

func TestWatchdogReload(t *testing.T) {
	for _, c := range []struct{ ms, want uint32 }{
		{0, 1}, {8, 1}, {16, 2}, {2000, 250}, {3000, 375}, {32000, 0xFA0}, {1 << 20, WatchdogMaxCount},
	} {
		if got := WatchdogReload(c.ms); got != c.want {
			t.Errorf("WatchdogReload(%d) = %d, want %d", c.ms, got, c.want)
		}
	}
	// 2 s must be well above the main loop's slowest pass (50 ms) and below the
	// carrier hard cap margin, whatever the LSI's error.
	if WatchdogReload(2000)*8 > 2000*2 {
		t.Error("reload too long for 2 s")
	}
}

func TestCarrierHardCap(t *testing.T) {
	if CarrierHardCapSeconds(10) <= 10 {
		t.Error("the cap must leave the driver time to end the carrier itself")
	}
}
