package checktext

import (
	"os"
	"strings"
	"testing"
)

func TestStatusOK(t *testing.T) {
	for _, c := range []struct {
		b    byte
		want uint8
		ok   bool
	}{
		{0x20, ModeStandbyRC, true},  // mode 2, no command status
		{0x22, ModeStandbyRC, true},  // mode 2, cmd status 1 (reserved, not an error)
		{0xA2, ModeStandbyRC, true},  // what the SX1262 returns in standby (bit 7 reads 1)
		{0xD2, ModeRx, true},         // and in receive
		{0xA5, ModeStandbyRC, false}, // rotated by one bit: bit 0 set (xfer's bench finding)
		{0xFF, ModeStandbyRC, false}, // MISO floating high
		{0x00, ModeStandbyRC, false}, // MISO stuck low: mode 0
		{0x21, ModeStandbyRC, false}, // bit 0 set
		{0x50, ModeRx, true},
		{0x50, ModeStandbyRC, false},
		{0x24, ModeStandbyRC, true},  // cmd status 2 = data available, not an error
		{0x26, ModeStandbyRC, false}, // cmd status 3 = command time out
		{0x28, ModeStandbyRC, false}, // cmd status 4 = processing error
		{0x2A, ModeStandbyRC, false}, // cmd status 5 = execution failure
	} {
		if got := StatusOK(c.b, c.want); got != c.ok {
			t.Errorf("StatusOK(%#02x, %d) = %v, want %v", c.b, c.want, got, c.ok)
		}
	}
}

func TestModeOK(t *testing.T) {
	// 0xaa = standby RC with cmd status 5, what the first status after a
	// power-up read on the bench: mode is fine, StatusOK refuses it.
	if !ModeOK(0xAA, ModeStandbyRC) || StatusOK(0xAA, ModeStandbyRC) {
		t.Error("0xaa must pass ModeOK and fail StatusOK")
	}
	if ModeOK(0xA3, ModeStandbyRC) || ModeOK(0xFF, ModeStandbyRC) || ModeOK(0x00, ModeStandbyRC) || ModeOK(0xD2, ModeStandbyRC) {
		t.Error("ModeOK accepts a wrong mode or bit 0")
	}
}

func TestVerdicts(t *testing.T) {
	if !BusyOK(4000, true, false) || BusyOK(4000, false, false) || BusyOK(4000, true, true) ||
		BusyOK(BusyLowMaxUs+1, true, false) || BusyOK(-1, true, false) {
		t.Error("BusyOK")
	}
	if !SyncOK(0x14, 0x24) || SyncOK(0x34, 0x44) || SyncOK(0x24, 0x14) || SyncOK(0xFF, 0xFF) {
		t.Error("SyncOK")
	}
	if !TCXOOK(true, 0x50, 0) || TCXOOK(false, 0x50, 0) ||
		TCXOOK(true, 0x20, 0) || // standby: the radio did not get to receive
		TCXOOK(true, 0x50, ErrXoscStart) || TCXOOK(true, 0x50, ErrPLLLock) ||
		!TCXOOK(true, 0x50, 1<<8) { // PA ramp is not this check's business
		t.Error("TCXOOK")
	}
	if !CalibOK(0) || !CalibOK(ErrXoscStart) || CalibOK(1) || CalibOK(1<<4) {
		t.Error("CalibOK")
	}
	if !ErrorsOK(0) || ErrorsOK(1<<8) {
		t.Error("ErrorsOK")
	}
	if !RSSIOK(RSSIMinRaw, 0x50) || !RSSIOK(RSSIMaxRaw, 0x50) || RSSIOK(RSSIMinRaw-1, 0x50) ||
		RSSIOK(RSSIMaxRaw+1, 0x50) || RSSIOK(200, 0x20) || RSSIOK(0, 0x50) || RSSIOK(255, 0x50) {
		t.Error("RSSIOK")
	}
}

func TestLines(t *testing.T) {
	var w Writer
	if got := string(w.Start(3, 48_000_000)); got != "wle5check start cycle=3 cpu_hz=48000000\r\n" {
		t.Errorf("Start: %q", got)
	}
	w.Begin("busy", true)
	w.Dec("low_us", 3912)
	w.Bool("timeout", false)
	if got := string(w.End()); got != "wle5check busy PASS low_us=3912 timeout=false\r\n" {
		t.Errorf("busy: %q", got)
	}
	w.Begin("status", false)
	w.Hex("status", 0xA2, 2)
	w.Hex("sync", 0x1424, 4)
	w.Dec("neg", -5)
	w.Str("src", "default")
	if got := string(w.End()); got != "wle5check status FAIL status=0xa2 sync=0x1424 neg=-5 src=default\r\n" {
		t.Errorf("status: %q", got)
	}
	w.Begin("rssi", true)
	w.Half("dbm", 207)
	w.Half("floor", 200)
	w.Half("zero", 0)
	if got := string(w.End()); got != "wle5check rssi PASS dbm=-103.5 floor=-100 zero=-0\r\n" {
		t.Errorf("rssi: %q", got)
	}
	if w.Pass != 2 || w.Fail != 1 {
		t.Errorf("counts %d/%d", w.Pass, w.Fail)
	}
	if got := string(w.Summary()); got != "wle5check FAIL pass=2 fail=1\r\n" {
		t.Errorf("summary: %q", got)
	}
	w.NewPass()
	if got := string(w.Summary()); got != "wle5check FAIL pass=0 fail=0\r\n" {
		t.Errorf("empty summary must not pass: %q", got)
	}
	w.Begin("busy", true)
	w.End()
	if got := string(w.Summary()); got != "wle5check PASS pass=1 fail=0\r\n" {
		t.Errorf("summary: %q", got)
	}
}

func TestErrors(t *testing.T) {
	var w Writer
	for _, c := range []struct {
		e    uint16
		want string
	}{
		{0, "none"},
		{1, "RC64K"},
		{1<<5 | 1<<6, "XOSC|PLLLOCK"},
		{0x1FF &^ (1 << 7), "RC64K|RC13M|PLLCAL|ADCCAL|IMGCAL|XOSC|PLLLOCK|PARAMP"},
		{1 << 7, "?"},
		{1<<15 | 1<<2, "PLLCAL|?"},
	} {
		w.Begin("errors", true)
		w.Errors("e", c.e)
		want := "wle5check errors PASS e=" + c.want + "\r\n"
		if got := string(w.End()); got != want {
			t.Errorf("Errors(%#x) = %q, want %q", c.e, got, want)
		}
	}
}

func TestLongLineIsCutNotGrown(t *testing.T) {
	var w Writer
	w.Begin("x", true)
	for i := 0; i < 50; i++ {
		w.Str("key", "value")
	}
	got := w.End()
	if len(got) > MaxLen || !strings.HasSuffix(string(got), "\r\n") {
		t.Errorf("line of %d bytes, ends %q", len(got), got[len(got)-4:])
	}
}

func TestNoAllocs(t *testing.T) {
	var w Writer
	if n := testing.AllocsPerRun(100, func() {
		w.Begin("tcxo", true)
		w.Dec("mv", 1800)
		w.Hex("status", 0x50, 2)
		w.Half("dbm", 207)
		w.Errors("e", 0x60)
		w.Bool("ok", true)
		w.End()
		w.Summary()
		w.Start(1, 48_000_000)
	}); n != 0 {
		t.Errorf("Writer allocates %v times", n)
	}
}

// The Python assertion carries its own list of checks; they must be the ones
// the program runs.
func TestPythonAssertionKnowsTheNames(t *testing.T) {
	b, err := os.ReadFile("../../../tools/wle5_uart_check.py")
	if err != nil {
		t.Fatal(err)
	}
	for _, name := range Names {
		if !strings.Contains(string(b), `"`+name+`"`) {
			t.Errorf("tools/wle5_uart_check.py does not list the check %q", name)
		}
	}
}

// The longest lines main prints must fit the buffer uncut.
func TestWorstCaseLinesFit(t *testing.T) {
	var w Writer
	w.Begin("tcxo", false)
	w.Dec("mv", 3300)
	w.Str("src", "default")
	w.Bool("init", false)
	w.Hex("status", 0xFF, 2)
	w.Dec("mode", 7)
	w.Dec("faults", 4294967295)
	w.Dec("retries", 4294967295)
	w.Str("hse", "timeout")
	w.Hex("cr", 0xFFFFFFFF, 8)
	got := string(w.End())
	if !strings.HasSuffix(got, "cr=0xffffffff\r\n") || len(got) > MaxLen-20 {
		t.Errorf("tcxo line %d bytes: %q", len(got), got)
	}
	w.Begin("init", false)
	w.Str("step", "calibrate")
	w.Hex("sr2", 0x1F5, 8)
	w.Hex("cr", 0x03200563, 8)
	if got := string(w.End()); got != "wle5check init FAIL step=calibrate sr2=0x000001f5 cr=0x03200563\r\n" {
		t.Errorf("init line: %q", got)
	}
}

func TestOpenLineHasNoVerdict(t *testing.T) {
	var w Writer
	w.Open("wle5probe step")
	w.Dec("v", 3)
	w.Hex("sr2", 0x1F5, 8)
	w.Dec("lo_us", -1)
	if got := string(w.End()); got != "wle5probe step v=3 sr2=0x000001f5 lo_us=-1\r\n" || w.Pass != 0 || w.Fail != 0 {
		t.Errorf("%q %d/%d", got, w.Pass, w.Fail)
	}
}
