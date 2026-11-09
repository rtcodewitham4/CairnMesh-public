package adapt

import "cairnmesh/firmware/console"

// Stream is a UART as the console uses it (machine.UART has these methods).
type Stream interface {
	Buffered() int
	ReadByte() (byte, error)
	WriteByte(c byte) error
}

// Serial is console.Port on a UART: the node's console on USART2 (the WLE5
// has no USB). A UART has no "connected" line, so the console is always
// connected and greets once, GreetDelay after boot (a program that opens the
// port later sees the prompt after its first Enter). Writes block until the
// bytes are in the UART, so there is always room: Writable reports more than
// console.MaxOutput. A 115200 bit/s UART takes ~87 us per byte, so a long
// answer (stats, ~900 bytes) holds the main loop ~80 ms; the radio's packet
// buffer keeps one received packet until the loop gets back.
type Serial struct {
	S Stream
}

func (p Serial) Connected() bool { return true }
func (p Serial) Buffered() int   { return p.S.Buffered() }

func (p Serial) ReadByte() (byte, error) { return p.S.ReadByte() }

func (p Serial) Write(b []byte) int {
	for i, c := range b {
		if p.S.WriteByte(c) != nil {
			return i
		}
	}
	return len(b)
}

func (p Serial) WriteString(s string) int {
	for i := 0; i < len(s); i++ {
		if p.S.WriteByte(s[i]) != nil {
			return i
		}
	}
	return len(s)
}

func (p Serial) Writable() int { return 2 * console.MaxOutput }
