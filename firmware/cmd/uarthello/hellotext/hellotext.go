// Package hellotext formats the line cmd/uarthello prints. It is separate from
// main only so the host can test it (main builds for the WLE5 target alone).
package hellotext

// Prefix starts every line.
const Prefix = "cairnmesh uarthello "

// MaxLen is the longest line: Prefix, a uint32 in decimal, CR LF.
const MaxLen = len(Prefix) + 10 + 2

// Line appends "cairnmesh uarthello <n>\r\n" to dst. It does not allocate when
// dst has MaxLen bytes of capacity.
func Line(dst []byte, n uint32) []byte {
	dst = append(dst, Prefix...)
	var digits [10]byte
	i := len(digits)
	for {
		i--
		digits[i] = byte('0' + n%10)
		n /= 10
		if n == 0 {
			break
		}
	}
	dst = append(dst, digits[i:]...)
	return append(dst, '\r', '\n')
}
