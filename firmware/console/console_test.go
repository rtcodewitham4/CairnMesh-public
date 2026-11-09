package console

import (
	"strings"
	"testing"
)

type fakePort struct {
	in   []byte
	out  strings.Builder
	conn bool
	room int
}

func (f *fakePort) Connected() bool { return f.conn }
func (f *fakePort) Buffered() int   { return len(f.in) }
func (f *fakePort) ReadByte() (byte, error) {
	b := f.in[0]
	f.in = f.in[1:]
	return b, nil
}
func (f *fakePort) Write(p []byte) int       { f.out.Write(p); return len(p) }
func (f *fakePort) WriteString(s string) int { f.out.WriteString(s); return len(s) }
func (f *fakePort) Writable() int            { return f.room }

func newConsole() (*Console, *fakePort) {
	f := &fakePort{conn: true, room: 1024}
	c := &Console{Banner: "hello"}
	c.Init(f)
	c.Poll(0) // the host opened the port
	c.Poll(GreetDelay)
	return c, f
}

// The host echoes what it receives until its program turns the tty's echo
// off: input before the banner is dropped, the banner waits GreetDelay.
func TestGreetingWaitsAndDropsTheHostEcho(t *testing.T) {
	f := &fakePort{room: 1024}
	c := &Console{Banner: "hello"}
	runs := 0
	c.Init(f)
	c.Handle("hello", "", func(*Console, [][]byte) { runs++ })
	c.Poll(0)
	f.conn = true
	c.Poll(10)
	f.in = []byte("hello\r")
	c.Poll(GreetDelay)
	if f.out.Len() != 0 || len(f.in) != 0 {
		t.Fatalf("early: out %q, left %q", f.out.String(), f.in)
	}
	c.Poll(10 + GreetDelay)
	if f.out.String() != "hello\r\n> " || runs != 0 {
		t.Fatalf("banner %q, runs %d", f.out.String(), runs)
	}
}

func TestCommandsAndArguments(t *testing.T) {
	c, f := newConsole()
	var got [][]byte
	c.Handle("set", "set things", func(c *Console, args [][]byte) {
		for _, a := range args {
			got = append(got, append([]byte(nil), a...))
		}
		c.PrintUint(4294967295)
		c.PrintField("x", 7)
		c.Println("")
	})
	if c.Handle("set", "", nil) {
		t.Error("a name registered twice")
	}
	f.in = []byte("  set  a bb \r\nnope\rhelp\r")
	c.Poll(GreetDelay)
	out := f.out.String()
	if !strings.HasPrefix(out, "hello\r\n> ") {
		t.Errorf("banner: %q", out)
	}
	if len(got) != 3 || string(got[1]) != "a" || string(got[2]) != "bb" {
		t.Errorf("args %q", got)
	}
	for _, want := range []string{"4294967295 x 7\r\n", "unknown command", "set       set things"} {
		if !strings.Contains(out, want) {
			t.Errorf("missing %q in %q", want, out)
		}
	}
	if strings.Count(out, "> ") != 4 { // banner + three lines (the LF of CR LF is not a line)
		t.Errorf("prompts: %q", out)
	}
}

func TestBackpressureAndEditing(t *testing.T) {
	c, f := newConsole()
	runs := 0
	c.Handle("go", "", func(*Console, [][]byte) { runs++ })
	f.room = MaxOutput - 1
	f.in = []byte("go\r")
	c.Poll(GreetDelay)
	if runs != 0 || len(f.in) != 3 {
		t.Fatal("read input without room for the answer")
	}
	f.room = 1024
	f.in = []byte("gx\x7fo\r")
	c.Poll(GreetDelay)
	if runs != 1 {
		t.Fatal("backspace editing")
	}
}

func TestParse(t *testing.T) {
	var k [2]byte
	if !ParseHex(k[:], []byte("aB0f")) || k != [2]byte{0xAB, 0x0F} {
		t.Error("hex")
	}
	if ParseHex(k[:], []byte("abc")) || ParseHex(k[:], []byte("zz00")) {
		t.Error("bad hex accepted")
	}
	if v, ok := ParseUint([]byte("4294967295")); !ok || v != 4294967295 {
		t.Error("max uint32")
	}
	if _, ok := ParseUint([]byte("4294967296")); ok {
		t.Error("overflow accepted")
	}
	if _, ok := ParseUint([]byte("12a")); ok {
		t.Error("junk accepted")
	}
}

// A line longer than lineLen runs cut, as ever; LineCut tells the command, and
// only that line.
func TestLineCutIsReportedForTheLongLineOnly(t *testing.T) {
	c, f := newConsole()
	var got []string
	var cut []bool
	c.Handle("echo", "", func(c *Console, args [][]byte) {
		got = append(got, string(args[len(args)-1]))
		cut = append(cut, c.LineCut())
	})
	long := "echo " + strings.Repeat("x", 200)
	f.in = []byte(long + "\recho short\r")
	c.Poll(GreetDelay + 1)
	if len(got) != 2 || len(got[0]) != lineLen-len("echo ") || !cut[0] || got[1] != "short" || cut[1] {
		t.Fatalf("got %q cut %v", got, cut)
	}
}

// The overflow of a cut line does not outlive the line: not a reconnect, not
// backspacing to an empty line.
func TestLineCutDoesNotOutliveTheLine(t *testing.T) {
	var cut []bool
	run := func(c *Console, f *fakePort, in []byte) {
		f.in = in
		c.Poll(GreetDelay + 1)
	}
	mk := func() (*Console, *fakePort) {
		c, f := newConsole()
		c.Handle("t", "", func(c *Console, _ [][]byte) { cut = append(cut, c.LineCut()) })
		return c, f
	}
	long := []byte(strings.Repeat("x", 130))
	// Backspace to an empty line, then a short command.
	c, f := mk()
	run(c, f, long)
	run(c, f, []byte(strings.Repeat("\b", lineLen)+"t\r"))
	// Disconnect and reconnect in the middle of a long line.
	c2, f2 := mk()
	run(c2, f2, long)
	f2.conn = false
	c2.Poll(2 * GreetDelay)
	f2.conn = true
	c2.Poll(3 * GreetDelay)
	c2.Poll(4 * GreetDelay)
	f2.in = []byte("t\r")
	c2.Poll(4*GreetDelay + 1)
	// Backspacing a long line only part way keeps it cut.
	c3, f3 := mk()
	run(c3, f3, append([]byte("t "), long...))
	run(c3, f3, []byte("\b\b\r"))
	if len(cut) != 3 || cut[0] || cut[1] || !cut[2] {
		t.Fatalf("LineCut after backspace-to-empty / reconnect / partial backspace = %v, want [false false true]", cut)
	}
}
