package adapt

import (
	"crypto/aes"
	"errors"
	"testing"

	"cairnmesh/firmware/console"
	"cairnmesh/firmware/keystore"
	"cairnmesh/firmware/proto"
	"cairnmesh/firmware/radio"
)

// ---- Cipher ----

// fakeBlock is an AES block made of crypto/aes that counts key loads and can
// be told to fail.
type fakeBlock struct {
	key   [16]byte
	loads int
	fail  bool
	calls int
}

func (b *fakeBlock) SetKey(key *[16]byte) { b.key = *key; b.loads++ }

func (b *fakeBlock) EncryptBlock(dst, src *[16]byte) bool {
	b.calls++
	if b.fail {
		return false
	}
	c, _ := aes.NewCipher(b.key[:])
	c.Encrypt(dst[:], src[:])
	return true
}

func TestCipherMatchesAESAndLoadsAKeyOnlyWhenItChanges(t *testing.T) {
	hw := &fakeBlock{}
	c := &Cipher{HW: hw}
	k1, k2 := [16]byte{1}, [16]byte{2}
	for i := 0; i < 3; i++ {
		kcv1 := keystore.KCV(c, &k1)
		var want, zero [16]byte
		g, _ := aes.NewCipher(k1[:])
		g.Encrypt(want[:], zero[:])
		if kcv1 != [3]byte{want[0], want[1], want[2]} {
			t.Fatalf("kcv %x, want %x", kcv1, want[:3])
		}
	}
	if hw.loads != 1 {
		t.Errorf("the same key loaded %d times, want 1", hw.loads)
	}
	var dst, src [16]byte
	c.Encrypt(&k2, &dst, &src)
	c.Encrypt(&k1, &dst, &src)
	if hw.loads != 3 {
		t.Errorf("%d key loads after k1 k2 k1, want 3", hw.loads)
	}
	// A key changed in place keeps its address: Forget makes it reload.
	k1[0] = 9
	c.Encrypt(&k1, &dst, &src)
	if hw.loads != 3 {
		t.Fatalf("reloaded without Forget (address is the same): %d", hw.loads)
	}
	c.Forget()
	c.Encrypt(&k1, &dst, &src)
	if hw.loads != 4 {
		t.Errorf("no reload after Forget: %d", hw.loads)
	}
}

func TestCipherCountsBlocksTheHardwareDidNotFinish(t *testing.T) {
	hw := &fakeBlock{fail: true}
	c := &Cipher{HW: hw}
	var key, dst, src [16]byte
	c.Encrypt(&key, &dst, &src)
	c.Encrypt(&key, &dst, &src)
	if c.Failures != 2 {
		t.Errorf("Failures %d", c.Failures)
	}
}

// The secure link on this Cipher reproduces what proto does on crypto/aes: the
// network layer of a frame sealed with one opens with the other.
func TestCipherCarriesTheNetworkLayer(t *testing.T) {
	key := [16]byte{0xA5, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15}
	hwC := &Cipher{HW: &fakeBlock{}}
	below := []byte{0x10, 0x20, 0x30, 0x40, 0x50}
	var sealed, opened [proto.MaxFrame]byte
	n := proto.SealNetwork(proto.CCM{C: hwC, Key: &key}, sealed[:], below, 77, 0x42)
	soft := &Cipher{HW: &fakeBlock{}}
	m, fcnt, ok := proto.OpenNetwork(proto.CCM{C: soft, Key: &key}, opened[:], sealed[:n])
	if !ok || fcnt != 77 || string(opened[:m]) != string(below) {
		t.Fatalf("open: ok=%v fcnt=%d % x", ok, fcnt, opened[:m])
	}
}

// ---- Rand ----

type seq struct {
	vals []uint32
	err  error
	i    int
}

func (s *seq) Uint32() (uint32, error) {
	if s.err != nil {
		return 0, s.err
	}
	v := s.vals[s.i%len(s.vals)]
	s.i++
	return v, nil
}

func TestRandStaysInRangeAndFallsBackToTheMidpoint(t *testing.T) {
	r := &Rand{Src: &seq{vals: []uint32{0, 0, 0xFFFFFFFF, 0xFFFFFFFF, 12345, 6789}}}
	lo, hi := proto.Time(100), proto.Time(700)
	seen := map[proto.Time]bool{}
	for i := 0; i < 30; i++ {
		v := r.Uniform(lo, hi)
		if v < lo || v > hi {
			t.Fatalf("Uniform(%d,%d) = %d", lo, hi, v)
		}
		seen[v] = true
	}
	if len(seen) < 3 {
		t.Errorf("only %d different values", len(seen))
	}
	if got := r.Uniform(500, 500); got != 500 {
		t.Errorf("empty range: %d", got)
	}
	if got := r.Uniform(500, 400); got != 500 {
		t.Errorf("inverted range: %d", got)
	}
	bad := &Rand{Src: &seq{err: errors.New("seed")}}
	if got := bad.Uniform(100, 300); got != 200 || bad.Errors != 1 {
		t.Errorf("RNG error: %d, errors %d", got, bad.Errors)
	}
}

// ---- GatedAir, GatedEmitter ----

type fakeAir struct {
	sent, preambled [][]byte
	rx              [][]byte
	rssi            int16
	receiving, tx   bool
}

func (a *fakeAir) Send(p []byte) bool {
	a.sent = append(a.sent, append([]byte(nil), p...))
	return true
}
func (a *fakeAir) SendPreamble(p []byte, n uint16) bool {
	a.preambled = append(a.preambled, append([]byte(nil), p...))
	return true
}
func (a *fakeAir) Receive() []byte {
	if len(a.rx) == 0 {
		return nil
	}
	p := a.rx[0]
	a.rx = a.rx[1:]
	return p
}
func (a *fakeAir) PacketRSSI() (int16, bool) { return a.rssi, true }
func (a *fakeAir) Receiving() bool           { return a.receiving }
func (a *fakeAir) Transmitting() bool        { return a.tx }

type plainAir struct{ sent int }

func (a *plainAir) Send([]byte) bool { a.sent++; return true }
func (a *plainAir) Receive() []byte  { return nil }

func TestGatedAirClosedNeverTransmits(t *testing.T) {
	air := &fakeAir{rx: [][]byte{{1, 2, 3}}, rssi: -77}
	g := &GatedAir{Air: air}
	if g.Send([]byte{9}) || g.SendPreamble([]byte{9}, 100) {
		t.Fatal("a closed gate sent")
	}
	if len(air.sent)+len(air.preambled) != 0 {
		t.Fatalf("the radio was handed %d packets", len(air.sent)+len(air.preambled))
	}
	if g.Refused != 2 {
		t.Errorf("Refused %d", g.Refused)
	}
	// It still listens.
	if p := g.Receive(); len(p) != 3 {
		t.Errorf("Receive: %v", p)
	}
	if v, ok := g.PacketRSSI(); !ok || v != -77 {
		t.Errorf("rssi %d %v", v, ok)
	}
}

func TestGatedAirOpenPassesThrough(t *testing.T) {
	air := &fakeAir{}
	g := &GatedAir{Air: air, Allow: true}
	if !g.Send([]byte{1}) || !g.SendPreamble([]byte{2}, 300) {
		t.Fatal("an open gate refused")
	}
	if len(air.sent) != 1 || len(air.preambled) != 1 {
		t.Errorf("sent %d, preambled %d", len(air.sent), len(air.preambled))
	}
	var _ radio.Air = g
	var _ radio.LongPreamble = g
	var _ radio.Levels = g
	var _ radio.Quiet = g
}

func TestGatedAirQuietFollowsTheRadio(t *testing.T) {
	air := &fakeAir{}
	g := &GatedAir{Air: air, Allow: true}
	if !g.Quiet() {
		t.Error("an idle radio is not quiet")
	}
	air.receiving = true
	if g.Quiet() {
		t.Error("quiet while a packet comes in")
	}
	air.receiving, air.tx = false, true
	if g.Quiet() {
		t.Error("quiet while transmitting")
	}
	// A radio that cannot tell is never taken for quiet; one without a
	// preamble option sends with its own.
	p := &plainAir{}
	gp := &GatedAir{Air: p, Allow: true}
	if gp.Quiet() {
		t.Error("an Air that cannot tell was taken for quiet")
	}
	if !gp.SendPreamble([]byte{1}, 400) || p.sent != 1 {
		t.Error("no fallback to Send")
	}
	if _, ok := gp.PacketRSSI(); ok {
		t.Error("an Air without levels reported one")
	}
}

type fakeEmitter struct {
	signals int
	until   proto.Time
}

func (e *fakeEmitter) Emit(s *radio.Signal, now proto.Time) bool {
	e.signals++
	e.until = now + s.Duration
	return true
}
func (e *fakeEmitter) Busy(now proto.Time) bool { return now < e.until }

func TestGatedEmitter(t *testing.T) {
	e := &fakeEmitter{}
	g := &GatedEmitter{E: e}
	sig := radio.Signal{Kind: radio.Carrier, PowerDBm: radio.MaxPower, Duration: 500 * proto.Millisecond}
	if g.Emit(&sig, 0) || e.signals != 0 || g.Busy(10) {
		t.Fatal("a closed gate started a signal")
	}
	g.Allow = true
	if !g.Emit(&sig, 0) || e.signals != 1 || !g.Busy(10) || g.Busy(600*proto.Millisecond) {
		t.Error("an open gate does not pass signals through")
	}
	if (&GatedEmitter{Allow: true}).Emit(&sig, 0) {
		t.Error("no emitter, yet a signal started")
	}
}

// ---- Serial and the console on it ----

type fakeUART struct {
	in  []byte
	out []byte
	// full: WriteByte fails after this many bytes (0: never).
	full int
}

func (u *fakeUART) Buffered() int { return len(u.in) }
func (u *fakeUART) ReadByte() (byte, error) {
	if len(u.in) == 0 {
		return 0, errors.New("empty")
	}
	b := u.in[0]
	u.in = u.in[1:]
	return b, nil
}
func (u *fakeUART) WriteByte(c byte) error {
	if u.full != 0 && len(u.out) >= u.full {
		return errors.New("full")
	}
	u.out = append(u.out, c)
	return nil
}

func TestSerialPortWritesAndReports(t *testing.T) {
	u := &fakeUART{}
	p := Serial{S: u}
	if !p.Connected() || p.Writable() < console.MaxOutput {
		t.Error("a UART is connected and writable")
	}
	if n := p.Write([]byte("abc")); n != 3 {
		t.Errorf("Write %d", n)
	}
	if n := p.WriteString("de"); n != 2 || string(u.out) != "abcde" {
		t.Errorf("WriteString %d %q", n, u.out)
	}
	u.full = 6
	if n := p.WriteString("xyz"); n != 1 {
		t.Errorf("a full UART took %d bytes", n)
	}
	u.in = []byte{'q'}
	if p.Buffered() != 1 {
		t.Error("Buffered")
	}
	if b, err := p.ReadByte(); err != nil || b != 'q' {
		t.Error("ReadByte")
	}
}

func TestConsoleRunsOverTheUART(t *testing.T) {
	u := &fakeUART{}
	var c console.Console
	c.Banner = "hello"
	c.Init(Serial{S: u})
	c.Handle("ping", "pong", func(c *console.Console, args [][]byte) { c.Println("pong") })
	c.Poll(0)
	c.Poll(console.GreetDelay + 1) // the banner
	u.in = []byte("ping\r")
	c.Poll(console.GreetDelay + 2)
	out := string(u.out)
	if !contains(out, "hello") || !contains(out, "pong") {
		t.Errorf("console output %q", out)
	}
}

func contains(s, sub string) bool {
	for i := 0; i+len(sub) <= len(s); i++ {
		if s[i:i+len(sub)] == sub {
			return true
		}
	}
	return false
}
