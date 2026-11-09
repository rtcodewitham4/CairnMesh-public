// Package console is the node's debug console: a line editor and a table of
// commands over a byte stream (the USB serial port, usb.Device). The
// firmware registers its commands at boot (stats, keys, DFU, ...); the
// console runs whether or not the node joined the network, so an
// unprovisioned node can still be provisioned. Pure Go, no allocation after
// the commands are registered.
package console

import "cairnmesh/firmware/proto"

// GreetDelay: the banner waits this long after the host opened the port,
// and input meanwhile is dropped. A program on the host sets DTR when it
// opens the port, before it turns the tty's echo off: a banner sent at
// once comes straight back as input (seen on the bench, 2026-10-22).
const GreetDelay = 200 * proto.Millisecond

// Port is the stream the console talks over (usb.Device has these methods).
type Port interface {
	Connected() bool
	Buffered() int
	ReadByte() (byte, error)
	Write(p []byte) int
	WriteString(s string) int
	Writable() int
}

// Handler runs a command; args[0] is the command's name.
type Handler func(c *Console, args [][]byte)

const (
	MaxCommands = 24
	MaxArgs     = 8
	lineLen     = 120
	// MaxOutput bounds what one command prints: the console reads input
	// only while this much room is left for output, so nothing is dropped
	// (the input waits and the host is held back).
	MaxOutput = 600
)

type command struct {
	name, help string
	run        Handler
}

// Console is one console.
type Console struct {
	port  Port
	cmds  [MaxCommands]command
	nCmds int
	line  [lineLen]byte
	nLine int
	// overflow: characters of the line being typed were dropped (it is longer
	// than lineLen); cut: the same for the line now running.
	overflow, cut bool
	args          [MaxArgs][]byte
	num           [20]byte
	connected     bool
	greeted       bool
	greetAt       proto.Time
	// Banner is printed when a program on the host opens the port.
	Banner string
}

// Init binds the console to its port and registers "help".
func (c *Console) Init(p Port) {
	c.port = p
	c.Handle("help", "list the commands", help)
}

// Handle registers a command; false when the table is full or the name is taken.
func (c *Console) Handle(name, help string, run Handler) bool {
	if c.nCmds >= MaxCommands || c.find(name) != nil {
		return false
	}
	c.cmds[c.nCmds] = command{name: name, help: help, run: run}
	c.nCmds++
	return true
}

func (c *Console) find(name string) *command {
	for i := 0; i < c.nCmds; i++ {
		if c.cmds[i].name == name {
			return &c.cmds[i]
		}
	}
	return nil
}

func help(c *Console, _ [][]byte) {
	for i := 0; i < c.nCmds; i++ {
		c.Print(c.cmds[i].name)
		for j := len(c.cmds[i].name); j < 10; j++ {
			c.Print(" ")
		}
		c.Println(c.cmds[i].help)
	}
}

// Poll reads what the host typed and runs complete lines.
func (c *Console) Poll(now proto.Time) {
	if conn := c.port.Connected(); conn != c.connected {
		c.connected, c.greeted, c.nLine, c.overflow = conn, false, 0, false
		c.greetAt = now + GreetDelay
	}
	if !c.connected {
		return
	}
	if !c.greeted {
		for c.port.Buffered() > 0 {
			c.port.ReadByte() // the host's own echo of nothing yet: drop
		}
		if now < c.greetAt {
			return
		}
		c.greeted = true
		c.Println(c.Banner)
		c.Print("> ")
	}
	for c.port.Buffered() > 0 && c.port.Writable() >= MaxOutput {
		b, _ := c.port.ReadByte()
		switch {
		case b == '\r' || b == '\n':
			if b == '\n' && c.nLine == 0 {
				continue // the LF of a CR LF
			}
			c.Print("\r\n")
			c.cut = c.overflow
			c.run(c.line[:c.nLine])
			c.cut, c.overflow = false, false
			c.nLine = 0
			c.Print("> ")
		case b == 0x7F || b == 0x08:
			if c.nLine > 0 {
				c.nLine--
				c.Print("\b \b")
				if c.nLine == 0 {
					c.overflow = false // an empty line has lost nothing
				}
			}
		case b >= ' ' && b < 0x7F && c.nLine < len(c.line):
			c.line[c.nLine] = b
			c.nLine++
			c.port.Write(c.line[c.nLine-1 : c.nLine]) // echo
		case b >= ' ' && b < 0x7F:
			c.overflow = true // the line is full: this character is dropped (LineCut tells the command)
		}
	}
}

// LineCut reports, inside a command, that the line it was given is the first
// lineLen characters of a longer one (the rest was dropped). A command whose
// meaning a cut line could change (a list that stops short is still a valid
// list) refuses it; the others behave as ever.
func (c *Console) LineCut() bool { return c.cut }

// run splits a line into words and runs its command.
func (c *Console) run(line []byte) {
	n := 0
	for i := 0; i < len(line); {
		for i < len(line) && line[i] == ' ' {
			i++
		}
		start := i
		for i < len(line) && line[i] != ' ' {
			i++
		}
		if i > start {
			if n == MaxArgs {
				c.Println("too many words")
				return
			}
			c.args[n] = line[start:i]
			n++
		}
	}
	if n == 0 {
		return
	}
	for i := 0; i < c.nCmds; i++ {
		if c.cmds[i].name == string(c.args[0]) {
			c.cmds[i].run(c, c.args[:n])
			return
		}
	}
	c.Println("unknown command; type help")
}

// Print writes s.
func (c *Console) Print(s string) { c.port.WriteString(s) }

// Println writes s and a line end.
func (c *Console) Println(s string) {
	c.port.WriteString(s)
	c.port.WriteString("\r\n")
}

// PrintBytes writes b as text.
func (c *Console) PrintBytes(b []byte) { c.port.Write(b) }

// PrintUint writes v in decimal.
func (c *Console) PrintUint(v uint64) {
	i := len(c.num)
	for {
		i--
		c.num[i] = byte('0' + v%10)
		v /= 10
		if v == 0 {
			break
		}
	}
	c.port.Write(c.num[i:])
}

// PrintField writes " name value": one counter of a stats line.
func (c *Console) PrintField(name string, v uint32) {
	c.Print(" ")
	c.Print(name)
	c.Print(" ")
	c.PrintUint(uint64(v))
}

// PrintHex writes b in hexadecimal.
func (c *Console) PrintHex(b []byte) {
	const digits = "0123456789abcdef"
	var two [2]byte
	for _, x := range b {
		two[0], two[1] = digits[x>>4], digits[x&15]
		c.port.Write(two[:])
	}
}

// ParseUint reads a decimal number.
func ParseUint(s []byte) (uint32, bool) {
	if len(s) == 0 || len(s) > 10 {
		return 0, false
	}
	var v uint64
	for _, ch := range s {
		if ch < '0' || ch > '9' {
			return 0, false
		}
		v = v*10 + uint64(ch-'0')
	}
	return uint32(v), v <= 0xFFFFFFFF
}

// ParseHex reads exactly len(dst) bytes of hexadecimal.
func ParseHex(dst, s []byte) bool {
	if len(s) != 2*len(dst) {
		return false
	}
	for i := range dst {
		hi, ok1 := nibble(s[2*i])
		lo, ok2 := nibble(s[2*i+1])
		if !ok1 || !ok2 {
			return false
		}
		dst[i] = hi<<4 | lo
	}
	return true
}

func nibble(ch byte) (byte, bool) {
	switch {
	case ch >= '0' && ch <= '9':
		return ch - '0', true
	case ch >= 'a' && ch <= 'f':
		return ch - 'a' + 10, true
	case ch >= 'A' && ch <= 'F':
		return ch - 'A' + 10, true
	}
	return 0, false
}
