// Command rxsim runs the "rxf" console command of package rxfilter on the host, one console line per
// input line, so tests/unit/test_rxfilter_ctl.py talks to the firmware's own command implementation
// (the output the board prints), not to a copy of it.
//
//	go run ./tools/rxsim [-id N] [-stopped]
//
// stdin: a console line ("rxf topo 1-2") or "!drop SENDER TX KIND FCNT RSSI|?" (a dropped frame, as
// the hook records one). stdout: what the command printed, then a line "--END--". Host only; no
// firmware image links it.
package main

import (
	"bufio"
	"flag"
	"fmt"
	"os"
	"strings"

	"cairnmesh/firmware/console"
	"cairnmesh/firmware/rxfilter"
)

type port struct {
	in  []byte
	out strings.Builder
}

func (p *port) Connected() bool { return true }
func (p *port) Buffered() int   { return len(p.in) }
func (p *port) ReadByte() (byte, error) {
	b := p.in[0]
	p.in = p.in[1:]
	return b, nil
}
func (p *port) Write(b []byte) int       { p.out.Write(b); return len(b) }
func (p *port) WriteString(s string) int { p.out.WriteString(s); return len(s) }
func (p *port) Writable() int            { return 1 << 20 }

func main() {
	id := flag.Int("id", 1, "this node's ID")
	stopped := flag.Bool("stopped", false, "the node is not running (no keys)")
	silent := flag.Bool("old", false, "an old image: no rxf command at all")
	flag.Parse()

	var f rxfilter.Filter
	p := &port{}
	c := &console.Console{}
	c.Init(p)
	if !*silent {
		c.Handle("rxf", "", func(c *console.Console, args [][]byte) {
			rxfilter.Command(c, args, byte(*id), !*stopped, &f)
		})
	}
	c.Poll(0)
	c.Poll(console.GreetDelay)
	in := bufio.NewScanner(os.Stdin)
	for in.Scan() {
		line := in.Text()
		if strings.HasPrefix(line, "!drop ") {
			var sender, tx, kind int
			var fcnt uint32
			var rssi string
			if n, _ := fmt.Sscanf(line, "!drop %d %d %d %d %s", &sender, &tx, &kind, &fcnt, &rssi); n != 5 {
				fmt.Println("bad !drop")
				fmt.Println("--END--")
				continue
			}
			d := rxfilter.Drop{Sender: byte(sender), TX: byte(tx), Kind: byte(kind), Fcnt: fcnt}
			if rssi != "?" {
				var v int
				fmt.Sscan(rssi, &v)
				d.RSSI, d.HasRSSI = int16(v), true
			}
			d.MIC = [4]byte{byte(fcnt), 0xAB, 0xCD, d.TX}
			f.Record(d)
			fmt.Println("--END--")
			continue
		}
		p.out.Reset()
		p.in = []byte(line + "\r")
		c.Poll(console.GreetDelay + 1)
		out := p.out.String()
		if i := strings.Index(out, "\r\n"); i >= 0 {
			out = out[i+2:]
		}
		out = strings.TrimSuffix(out, "> ")
		fmt.Print(strings.ReplaceAll(out, "\r", ""))
		fmt.Println("--END--")
	}
}
