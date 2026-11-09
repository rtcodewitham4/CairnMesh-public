package hellotext

import (
	"strconv"
	"testing"
)

func TestLine(t *testing.T) {
	for _, n := range []uint32{0, 1, 9, 10, 99, 100, 123456789, 1 << 31, 4294967295} {
		want := "cairnmesh uarthello " + strconv.FormatUint(uint64(n), 10) + "\r\n"
		if got := string(Line(nil, n)); got != want {
			t.Errorf("Line(%d) = %q, want %q", n, got, want)
		}
	}
}

func TestLineFitsMaxLen(t *testing.T) {
	var buf [MaxLen]byte
	if got := Line(buf[:0], 4294967295); len(got) != MaxLen {
		t.Errorf("longest line is %d bytes, MaxLen says %d", len(got), MaxLen)
	}
	if n := testing.AllocsPerRun(100, func() { Line(buf[:0], 4294967295) }); n != 0 {
		t.Errorf("Line allocates %v times", n)
	}
}
