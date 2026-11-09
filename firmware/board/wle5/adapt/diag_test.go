package adapt

import (
	"strings"
	"testing"

	"cairnmesh/firmware/console"
)

func runDiag(t *testing.T, d Diag) string {
	t.Helper()
	u := &fakeUART{}
	var c console.Console
	c.Init(Serial{S: u})
	c.Handle("diag", "boot self-tests", DiagCommand(func(out *Diag) { *out = d }))
	c.Poll(0)
	c.Poll(console.GreetDelay + 1)
	u.out = u.out[:0]
	u.in = []byte("diag\r")
	c.Poll(console.GreetDelay + 2)
	return string(u.out)
}

func TestDiagGoodBoot(t *testing.T) {
	out := runDiag(t, Diag{AESRan: true, AESOK: true, RNGRan: true, PLLRDY: true, PLLQEN: true, RNGSR: 1})
	for _, want := range []string{
		"aes: selftest ok failures 0",
		"rng: start ok draw_errors 0 sr 1",
		"clock: rngsel pllq pll ok pllq ok",
		"flash: pages 128 x 2048 B, image below page 101; records 101-116 marks 117-124 keys 125 fcnt 126,127 ecc_faults 0",
	} {
		if !strings.Contains(out, want) {
			t.Errorf("missing %q in:\n%s", want, out)
		}
	}
}

func TestDiagShowsFailures(t *testing.T) {
	out := runDiag(t, Diag{AESRan: true, AESOK: false, AESFailures: 2, RNGRan: true, RNGErr: "rng: clock error", RNGErrors: 3, RNGSEL: 3, ECCFaults: 1})
	for _, want := range []string{
		"aes: selftest FAIL failures 2",
		"rng: start FAIL (rng: clock error) draw_errors 3",
		"rngsel msi pll FAIL pllq FAIL",
		"ecc_faults 1",
	} {
		if !strings.Contains(out, want) {
			t.Errorf("missing %q in:\n%s", want, out)
		}
	}
}

func TestDiagBeforeStartUp(t *testing.T) {
	out := runDiag(t, Diag{})
	if !strings.Contains(out, "aes: selftest not run") || !strings.Contains(out, "rng: start not run") {
		t.Errorf("a boot that never reached the peripherals is not marked:\n%s", out)
	}
}

// The report carries no key: the Diag type has no key field, and the output is
// only the words above and decimal numbers.
func TestDiagOutputHasNoHexDump(t *testing.T) {
	out := runDiag(t, Diag{AESRan: true, AESOK: true, RNGRan: true})
	if strings.Contains(out, "kcv") || strings.Contains(out, "0x") {
		t.Errorf("unexpected key-like output:\n%s", out)
	}
}

func TestPrintHWSurvivesNilPeripherals(t *testing.T) {
	u := &fakeUART{}
	var c console.Console
	c.Init(Serial{S: u})
	PrintHW(&c, nil, nil, 2)
	if got := string(u.out); got != "hw: aes none counter none ecc_faults 2\r\n" {
		t.Errorf("nil peripherals: %q", got)
	}
	u.out = u.out[:0]
	store, _ := newCounter(newFlash())
	PrintHW(&c, &Cipher{HW: &fakeBlock{}, Failures: 4}, store, 0)
	if got := string(u.out); got != "hw: aes_failures 4 counter_errors 0 ecc_faults 0\r\n" {
		t.Errorf("live peripherals: %q", got)
	}
}
