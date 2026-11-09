package usb

// ringSize is each direction's buffer (a power of two).
const ringSize = 1024

// ring is a byte FIFO.
type ring struct {
	buf        [ringSize]byte
	head, tail uint32 // free-running; len = head - tail
}

func (r *ring) clear()    { r.head, r.tail = 0, 0 }
func (r *ring) len() int  { return int(r.head - r.tail) }
func (r *ring) free() int { return ringSize - r.len() }

func (r *ring) write(p []byte) int {
	n := min(len(p), r.free())
	for i := 0; i < n; i++ {
		r.buf[(r.head+uint32(i))%ringSize] = p[i]
	}
	r.head += uint32(n)
	return n
}

func (r *ring) writeString(s string) int {
	n := min(len(s), r.free())
	for i := 0; i < n; i++ {
		r.buf[(r.head+uint32(i))%ringSize] = s[i]
	}
	r.head += uint32(n)
	return n
}

func (r *ring) read(p []byte) int {
	n := min(len(p), r.len())
	for i := 0; i < n; i++ {
		p[i] = r.buf[(r.tail+uint32(i))%ringSize]
	}
	r.tail += uint32(n)
	return n
}
