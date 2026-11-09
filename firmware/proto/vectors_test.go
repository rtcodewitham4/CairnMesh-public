package proto

import (
	"bytes"
	"crypto/aes"
	"crypto/cipher"
	"encoding/hex"
	"encoding/json"
	"os"
	"testing"
)

// softCipher is the host's AES: crypto/aes, one block per key.
type softCipher struct{ blocks map[[16]byte]cipher.Block }

func (s *softCipher) Encrypt(key *[16]byte, dst, src *[16]byte) {
	if s.blocks == nil {
		s.blocks = map[[16]byte]cipher.Block{}
	}
	b, ok := s.blocks[*key]
	if !ok {
		b, _ = aes.NewCipher(key[:])
		s.blocks[*key] = b
	}
	b.Encrypt(dst[:], src[:])
}

func unhex(t *testing.T, s string) []byte {
	t.Helper()
	b, err := hex.DecodeString(s)
	if err != nil {
		t.Fatal(err)
	}
	return b
}

func key16(t *testing.T, s string) *[16]byte {
	var k [16]byte
	copy(k[:], unhex(t, s))
	return &k
}

type wireVectors struct {
	Version    string                       `json:"version"`
	NetworkKey string                       `json:"network_key"`
	AppKeys    map[string]map[string]string `json:"app_keys"`
	Controller byte                         `json:"controller"`
	Frames     []struct {
		Name     string `json:"name"`
		Plain    string `json:"plain"`
		Below    string `json:"below"`
		Wire     string `json:"wire"`
		Fcnt     uint32 `json:"fcnt"`
		TX       byte   `json:"tx"`
		AppLayer bool   `json:"app_layer"`
	} `json:"frames"`
	RelayPassesOn struct {
		From string `json:"from"`
		TX   byte   `json:"tx"`
		Wire string `json:"wire"`
	} `json:"relay_passes_on"`
	ReplayWindow struct {
		Steps []struct {
			Fcnt     uint32 `json:"fcnt"`
			Accepted bool   `json:"accepted"`
		} `json:"steps"`
	} `json:"replay_window"`
	AppReplay struct {
		Wire string `json:"wire"`
	} `json:"app_replay"`
}

func loadWire(t *testing.T) wireVectors {
	t.Helper()
	raw, err := os.ReadFile("../../tests/vectors/lora_v5.json")
	if err != nil {
		t.Fatal(err)
	}
	var v wireVectors
	if err := json.Unmarshal(raw, &v); err != nil {
		t.Fatal(err)
	}
	if v.Version != "v5" {
		t.Fatalf("vector file is %s, the firmware implements v5", v.Version)
	}
	return v
}

const vectorNode = 0x01

// TestWireVectors seals every frame of lora_v5.json the way vectors.build()
// does (one Secure per sender, counters from 77, the controller and node
// 0x01 with the application layer) and checks the bytes on air, what a relay
// sees below the security layer, and that a receiver opens it back.
func TestWireVectors(t *testing.T) {
	v := loadWire(t)
	c := &softCipher{}
	netKey := key16(t, v.NetworkKey)
	appKey := key16(t, v.AppKeys["0x01"]["0"])
	stacks := map[byte]*Secure{}
	stack := func(sender byte) *Secure {
		if s, ok := stacks[sender]; ok {
			return s
		}
		s := &Secure{}
		s.Init(c, netKey, 76, nil)
		if sender == v.Controller || sender == vectorNode {
			s.App = true
			s.Keys.Add(vectorNode, 0, appKey)
		}
		stacks[sender] = s
		return s
	}
	var byController, byNode [][]byte
	byName := map[string][]byte{}
	var out, opened [MaxFrame]byte
	for _, f := range v.Frames {
		plain, wire := unhex(t, f.Plain), unhex(t, f.Wire)
		s := stack(plain[1])
		switch f.Name {
		case "reply_to_request", "reply_routed", "reply_accepted", "reply_interrupted":
			for _, raw := range byController {
				s.OpenFrame(opened[:], raw)
			}
		case "result_ack":
			for _, raw := range byNode {
				s.OpenFrame(opened[:], raw)
			}
		}
		n, ok := s.SealFrame(out[:], plain)
		if !ok || !bytes.Equal(out[:n], wire) {
			t.Errorf("%s: sealed %x, want %x", f.Name, out[:n], wire)
			continue
		}
		sealed := append([]byte(nil), wire...)
		byName[f.Name] = sealed
		switch plain[1] {
		case v.Controller:
			byController = append(byController, sealed)
		case vectorNode:
			byNode = append(byNode, sealed)
		}

		m, fcnt, ok := OpenNetwork(CCM{C: c, Key: netKey}, opened[:], wire)
		if !ok || !bytes.Equal(opened[:m], unhex(t, f.Below)) || fcnt != f.Fcnt || Transmitter(wire) != f.TX {
			t.Errorf("%s: below %x fcnt %d, want %s fcnt %d", f.Name, opened[:m], fcnt, f.Below, f.Fcnt)
		}
		if (opened[0]&FlagApp != 0) != f.AppLayer {
			t.Errorf("%s: application layer %v, want %v", f.Name, opened[0]&FlagApp != 0, f.AppLayer)
		}

		// A fresh receiver holding node 0x01's key opens it to the plain frame.
		r := &Secure{App: true}
		r.Init(c, netKey, 0, nil)
		r.Keys.Add(vectorNode, 0, appKey)
		m, why := r.OpenFrame(opened[:], wire)
		if why != Accepted || !bytes.Equal(opened[:m], plain) {
			t.Errorf("%s: receiver got %x (reject %d), want %x", f.Name, opened[:m], why, plain)
		}
	}

	p := v.RelayPassesOn
	n, ok := Reseal(CCM{C: c, Key: netKey}, out[:], byName[p.From], p.TX)
	if !ok || !bytes.Equal(out[:n], unhex(t, p.Wire)) {
		t.Errorf("relay passes on: %x, want %s", out[:n], p.Wire)
	}

	var w ReplayWindow
	for i, step := range v.ReplayWindow.Steps {
		fresh := w.Check(step.Fcnt)
		if fresh {
			w.Accept(step.Fcnt)
		}
		if fresh != step.Accepted {
			t.Errorf("replay window step %d (fcnt %d): %v, want %v", i, step.Fcnt, fresh, step.Accepted)
		}
	}

	r := &Secure{App: true}
	r.Init(c, netKey, 0, nil)
	r.Keys.Add(vectorNode, 0, appKey)
	if _, why := r.OpenFrame(opened[:], unhex(t, v.AppReplay.Wire)); why != AppFailed {
		t.Errorf("app replay: reject %d, want AppFailed", why)
	}
}

func TestOpenFrameRejects(t *testing.T) {
	v := loadWire(t)
	c := &softCipher{}
	netKey := key16(t, v.NetworkKey)
	wire := unhex(t, v.Frames[0].Wire)
	var out [MaxFrame]byte
	s := &Secure{}
	s.Init(c, netKey, 0, nil)
	if _, why := s.OpenFrame(out[:], wire); why != Accepted {
		t.Fatalf("first copy: %d", why)
	}
	if _, why := s.OpenFrame(out[:], wire); why != Replayed {
		t.Errorf("second copy: %d, want Replayed", why)
	}
	bad := append([]byte(nil), wire...)
	bad[len(bad)-1] ^= 1
	if _, why := s.OpenFrame(out[:], bad); why != AuthFailed {
		t.Errorf("flipped MIC: %d, want AuthFailed", why)
	}
	if _, why := s.OpenFrame(out[:], unhex(t, v.Frames[0].Plain)); why != PlainDropped {
		t.Errorf("unsealed: %d, want PlainDropped", why)
	}
}

type failingStore struct{ calls int }

func (f *failingStore) ReserveTx(uint32) bool { f.calls++; return false }

type memStore struct{ bound uint32 }

func (m *memStore) ReserveTx(b uint32) bool { m.bound = b; return true }

func TestCounterReservedBeforeUse(t *testing.T) {
	c := &softCipher{}
	var k [16]byte
	var out [MaxFrame]byte
	plain := []byte{KindWake, 0x42, 0x01, 5, 9}

	s := &Secure{}
	f := &failingStore{}
	s.Init(c, &k, 1000, f)
	if _, ok := s.SealFrame(out[:], plain); ok || s.Fcnt() != 1000 {
		t.Fatalf("sealed although the counter was not stored (fcnt %d)", s.Fcnt())
	}

	m := &memStore{}
	s = &Secure{}
	s.Init(c, &k, 1000, m)
	// 1001 reserves up to 1001+256; 1258 is the first counter past it.
	for i := 0; i < TxReserve+2; i++ {
		if _, ok := s.SealFrame(out[:], plain); !ok {
			t.Fatal("seal failed")
		}
	}
	if m.bound != 1258+TxReserve || s.Fcnt() != 1258 {
		t.Errorf("bound %d, fcnt %d after %d frames from 1000", m.bound, s.Fcnt(), TxReserve+2)
	}
}

type senderMarks struct{ got []byte }

func (m *senderMarks) Mark(s byte, _ uint32) bool  { m.got = append(m.got, s); return true }
func (m *senderMarks) Visit(s byte, _ uint32) bool { m.got = append(m.got, s); return true }

// A device persists the controller's mark only (spec 5: flash wear).
func TestOnlyTheControllersMarkIsPersisted(t *testing.T) {
	c := &softCipher{}
	var k [16]byte
	var out, wire [MaxFrame]byte
	tx, rx := &Secure{}, &Secure{}
	tx.Init(c, &k, 0, nil)
	rx.Init(c, &k, 0, nil)
	store := &senderMarks{}
	rx.SetMarks(store, 0x42)
	for _, sender := range []byte{0x42, 0x07} {
		n, ok := tx.SealFrame(wire[:], []byte{KindWake, sender, 0x01, 5, 9})
		if !ok {
			t.Fatal("seal failed")
		}
		if _, why := rx.OpenFrame(out[:], wire[:n]); why != Accepted {
			t.Fatalf("sender %#x: %d", sender, why)
		}
	}
	rx.FlushMarks(0, true)
	if !bytes.Equal(store.got, []byte{0x42}) {
		t.Fatalf("stored marks of %x, want 42", store.got)
	}
	store.got = nil
	rx.EachMark(store)
	if !bytes.Equal(store.got, []byte{0x42}) {
		t.Fatalf("compaction keeps %x, want 42", store.got)
	}
}
