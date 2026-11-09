package adapt

import "cairnmesh/firmware/console"

// Diag is what the console's "diag" command reports: the results of the boot's
// peripheral start-up and the clock state behind the RNG, plus the flash layout.
// It holds no key material and no key-derived value. The board layer fills it
// (board/wle5/node.go: Diagnose); the printing is here so the host tests cover
// the format.
type Diag struct {
	AESRan, AESOK bool   // the FIPS-197 self-test ran at boot / passed
	AESFailures   uint32 // blocks the AES hardware did not finish since boot
	RNGRan        bool   // the RNG start-up ran
	RNGErr        string // its error, "" when it started
	RNGErrors     uint32 // draws that failed since boot (Rand fell back to the midpoint)
	RNGSEL        uint8  // RCC_CCIPR.RNGSEL: 0 = PLLQ, 1 = LSI, 2 = LSE, 3 = MSI
	PLLRDY        bool   // RCC_CR.PLLRDY
	PLLQEN        bool   // RCC_PLLCFGR.PLLQEN
	RNGSR         uint32 // RNG_SR (DRDY bit 0, CECS 1, SECS 2, CEIS 5, SEIS 6)
	ECCFaults     uint32 // double ECC errors read from the flash since boot
}

func onOff(b bool) string {
	if b {
		return "ok"
	}
	return "FAIL"
}

// Print writes the report: one line each for the AES, the RNG, the clock behind
// the RNG and the flash layout.
func (d *Diag) Print(c *console.Console) {
	c.Print("aes:")
	if !d.AESRan {
		c.Print(" selftest not run")
	} else {
		c.Print(" selftest ")
		c.Print(onOff(d.AESOK))
	}
	c.PrintField("failures", d.AESFailures)
	c.Println("")

	c.Print("rng:")
	switch {
	case !d.RNGRan:
		c.Print(" start not run")
	case d.RNGErr != "":
		c.Print(" start FAIL (")
		c.Print(d.RNGErr)
		c.Print(")")
	default:
		c.Print(" start ok")
	}
	c.PrintField("draw_errors", d.RNGErrors)
	c.PrintField("sr", d.RNGSR)
	c.Println("")

	c.Print("clock: rngsel ")
	c.Print(rngselName(d.RNGSEL))
	c.Print(" pll ")
	c.Print(onOff(d.PLLRDY))
	c.Print(" pllq ")
	c.Print(onOff(d.PLLQEN))
	c.Println("")

	c.Print("flash: pages ")
	c.PrintUint(Pages)
	c.Print(" x ")
	c.PrintUint(PageSize)
	c.Print(" B, image below page ")
	c.PrintUint(RecordsPage)
	c.Print("; records ")
	printRange(c, RecordsPage, RecordsPage+2*RecordsBank-1)
	c.Print(" marks ")
	printRange(c, MarksPage, MarksPage+2*MarksBank-1)
	c.Print(" keys ")
	c.PrintUint(KeyPage)
	c.Print(" fcnt ")
	c.PrintUint(CounterPageA)
	c.Print(",")
	c.PrintUint(CounterPageB)
	c.PrintField("ecc_faults", d.ECCFaults)
	c.Println("")
}

func printRange(c *console.Console, first, last int) {
	c.PrintUint(uint64(first))
	c.Print("-")
	c.PrintUint(uint64(last))
}

func rngselName(v uint8) string {
	switch v {
	case 0:
		return "pllq"
	case 1:
		return "lsi"
	case 2:
		return "lse"
	}
	return "msi"
}

// DiagCommand is the console command "diag": fill gathers the live state.
func DiagCommand(fill func(*Diag)) console.Handler {
	return func(c *console.Console, _ [][]byte) {
		var d Diag
		fill(&d)
		d.Print(c)
	}
}

// PrintHW writes the "hw:" line of the stats: the AES failures and the counter
// errors. Either may be nil (the boot stopped before it created them, as an AES
// self-test failure does): the line then says so instead of dereferencing it.
func PrintHW(c *console.Console, cipher *Cipher, counter *CounterStore, eccFaults uint32) {
	c.Print("hw:")
	if cipher != nil {
		c.PrintField("aes_failures", cipher.Failures)
	} else {
		c.Print(" aes none")
	}
	if counter != nil {
		c.PrintField("counter_errors", counter.Errors)
	} else {
		c.Print(" counter none")
	}
	c.PrintField("ecc_faults", eccFaults)
	c.Println("")
}
