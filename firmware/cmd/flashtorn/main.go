// Command flashtorn tears flash writes on purpose: it appends to a journal
// (the real journal package on the real flash, board.Flash with its NMI
// handler) as fast as it can, so that pulling the power almost always cuts
// a program or an erase. Each boot then shows whether the cut left a torn
// slot (bad CRC) or a double ECC error, and whether the node still boots
// and keeps every entry it had written.
//
// Pages 48-79 only (two banks of 16 pages): above this image, below the
// node's journals (101-124), key page and counter pages. Hammering
// stops after 60 s of uptime to spare them (each page is erased about once
// every 2-3 s while it runs).
//
// Output on the ST-LINK serial port (USART2, 115200), about 3 lines a second:
//
//	H n=<last written> boot: entries=<n> last=<n> torn=<n> ecc=<n> order=<ok|bad> hammer=<on|off>
//	F n=<n>   an append stored nothing
//
// n is the counter of the newest entry that read back as written: after a
// cut the next boot's "last" must not be below the last n printed. The LED
// is lit while it hammers (pull the power then); it waits 4 s after boot
// first so the PC's serial port is back and has read the boot line.
package main

import (
	"machine"
	"time"

	"cairnmesh/firmware/board"
	"cairnmesh/firmware/hw/cyc"
	"cairnmesh/firmware/journal"
	"cairnmesh/firmware/link"
)

const (
	firstPage    = 48
	pagesPerBank = 16
	kind         = 'T'
	settle       = 4 * time.Second
	hammerFor    = 60 * time.Second
)

// counting counts what the boot's reads found: double ECC errors (the NMI
// handler made the read fail) and readable slots whose CRC fails.
type counting struct {
	board.Flash
	ecc, torn uint32
}

func (c *counting) Read(page, offset int, dw *[8]byte) bool {
	if !c.Flash.Read(page, offset, dw) {
		c.ecc++
		return false
	}
	if !erased(dw) && link.CRC16(dw[:6]) != uint16(dw[6])|uint16(dw[7])<<8 {
		c.torn++
	}
	return true
}

func erased(dw *[8]byte) bool {
	for _, b := range dw {
		if b != 0xFF {
			return false
		}
	}
	return true
}

// log replays the counters in order and keeps the newest as the live state.
type log struct {
	entries, last uint32
	bad           bool
}

func (l *log) Replay(e *journal.Entry) {
	v := uint32(e[0]) | uint32(e[1])<<8 | uint32(e[2])<<16 | uint32(e[3])<<24
	if l.entries > 0 && v <= l.last {
		l.bad = true
	}
	l.entries++
	l.last = v
}

// cur is the entry being appended, kept the newest one written: the live
// state a compaction writes before cur.
var cur, kept journal.Entry

func (l *log) Live(j *journal.Journal) bool { return j.Keep(&kept) }

func entry(v uint32) journal.Entry {
	return journal.Entry{byte(v), byte(v >> 8), byte(v >> 16), byte(v >> 24)}
}

func main() {
	led := machine.PA5
	led.Configure(machine.PinConfig{Mode: machine.PinOutput})
	led.Low()
	// 48 MHz: the CPU's share of an append shrinks, so the flash is busy most
	// of the time and a cut lands in a program or an erase.
	if cyc.To48MHz() {
		machine.TIM16.Device.PSC.Set(48/4 - 1) // the runtime set TIM16 and the UART up for 4 MHz
		cyc.SetUART2Baud(115200)
	}

	f := &counting{}
	var j journal.Journal
	var boot log
	j.Open(f, firstPage, pagesPerBank, kind, &boot)
	ecc, torn := f.ecc, f.torn // the boot's reads only

	order := "ok"
	if boot.bad {
		order = "bad"
	}
	n := boot.last
	kept = entry(n)
	report := func(hammer string) {
		println("H n=", n, "boot: entries=", boot.entries, "last=", boot.last, "torn=", torn,
			"ecc=", ecc, "order=", order, "hammer=", hammer)
	}

	start := time.Now()
	for time.Since(start) < settle {
		report("off")
		time.Sleep(300 * time.Millisecond)
	}

	led.High()
	next := time.Now()
	for time.Since(start) < hammerFor {
		v := n + 1
		cur = entry(v)
		if !j.Append(&cur, &boot) {
			println("F n=", v)
			continue
		}
		n, kept = v, cur
		if time.Since(next) >= 0 {
			report("on")
			next = next.Add(300 * time.Millisecond)
		}
	}
	led.Low()
	for {
		report("off")
		time.Sleep(time.Second)
	}
}
