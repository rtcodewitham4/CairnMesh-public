package mesh

import (
	"crypto/aes"
	"crypto/cipher"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"math"
	"os"
	"testing"

	"cairnmesh/firmware/proto"
	"cairnmesh/firmware/radio/sx126x"
)

// The relay behaviour vectors (spec 16.2), replayed by the file's rules:
// time advances event by event (timers due before an instant, then the
// input, then timers due at it); every draw is the midpoint; airtime is
// SF7 / 125 kHz / CR 4/5; the relay's own frames and the layer above's
// share one FCNT from first_fcnt.

type softCipher struct{ block cipher.Block }

func (s *softCipher) Encrypt(key *[16]byte, dst, src *[16]byte) {
	if s.block == nil {
		s.block, _ = aes.NewCipher(key[:])
	}
	s.block.Encrypt(dst[:], src[:])
}

type vectorFile struct {
	Node       byte   `json:"node"`
	FirstFcnt  uint32 `json:"first_fcnt"`
	NetworkKey string `json:"network_key"`
	Cases      []vectorCase
}

type vectorCase struct {
	Name   string
	Config map[string]float64
	Asleep bool
	Steps  []struct {
		At   float64
		Rx   string
		RSSI *float64 `json:"rssi"`
		Send string
	}
	End    float64
	Expect []event
}

type event struct {
	At       float64        `json:"at"`
	Tx       string         `json:"tx,omitempty"`
	Preamble int            `json:"preamble,omitempty"`
	Up       string         `json:"up,omitempty"`
	Failed   map[string]any `json:"route_failed,omitempty"`
	Radio    map[string]any `json:"radio,omitempty"`
}

func seconds(s float64) proto.Time { return proto.Time(math.Round(s * 1e6)) }

// ported lists the cases whose behaviour the Go relay has so far, by phase.
var ported = map[string]bool{
	"flood_relayed_once_after_rssi_backoff":       true,
	"outward_flood_suppressed_by_three_copies":    true,
	"reply_follows_the_backbone_and_is_confirmed": true,
	"unconfirmed_reply_repeated_once":             true,
	"reply_off_the_backbone_dropped":              true,
	"reply_cancels_the_waiting_request":           true,
	"frame_for_this_node_goes_up_once":            true,
	"hello_is_one_hop":                            true,
	"replays_forgeries_and_plain_frames_dropped":  true,
	"own_frame_repeated_without_a_copy":           true,
	"own_frame_acknowledged_by_a_relay_copy":      true,
	// phase 2: routed frames (11.2)
	"routed_forward_acknowledged_by_next_hop":      true,
	"routed_repeats_then_route_error":              true,
	"routed_frame_not_on_route_ignored":            true,
	"routed_retry_answered_with_the_furthest_copy": true,
	"last_hop_into_gateway_acknowledged_by_echo":   true,
	"skipped_hop_waits_a_slot":                     true,
	"routed_frame_for_this_node_goes_up":           true,
	// phase 3: probes and JOIN (11.3, 12.2)
	"probe_relayed_with_own_hop":         true,
	"probe_keeps_the_best_copy":          true,
	"probe_passed_on_is_not_repeated":    true,
	"probe_target_answers_the_best_path": true,
	"join_relayed_like_a_probe":          true,
	// phase 4: sessions and waking (15.2, 14.3)
	"keepalive_moves_after_the_answer_and_reverts":      true,
	"wake_net_relayed_with_long_preamble":               true,
	"wake_net_repeated_for_a_silent_neighbour":          true,
	"wake_net_not_repeated_once_the_neighbour_is_heard": true,
	"dormant_node_hears_only_wake_frames":               true,
	// phase 5: traces and probes along a route (11.8, 15.1)
	"trace_relayed_and_its_answer_carries_our_record": true,
	"trace_target_answers_with_its_record":            true,
	"routed_probe_passed_on_by_the_next_listed_relay": true,
	"own_routed_probe_repeated_until_passed_on":       true,
	// 2026-10-27: the target answers every repeat (linger moves on)
	"target_answers_every_repeat_of_the_last_relay": true,
	"target_stops_answering_after_retry_cap":       true,
}

type got struct {
	at       proto.Time
	tx       []byte
	preamble uint16
	up       []byte
	radio    []byte // channel, SF
}

type bench struct {
	node   byte
	fcnt   uint32
	now    proto.Time
	trace  []got
	ccm    proto.CCM
	key    [16]byte
	plain  [proto.MaxFrame]byte
	sealed [proto.MaxFrame]byte
}

func (b *bench) Put(raw []byte, preamble uint16) {
	b.trace = append(b.trace, got{at: b.now, tx: append([]byte(nil), raw...), preamble: preamble})
}

// SealFrame is the node's one FCNT counter (the layer above's and the relay's).
func (b *bench) SealFrame(dst, plain []byte) (int, bool) {
	n := proto.SealNetwork(b.ccm, dst, plain, b.fcnt, b.node)
	b.fcnt++
	return n, true
}

func (b *bench) SetRadio(channel, sf uint8) {
	b.trace = append(b.trace, got{at: b.now, radio: []byte{channel, sf}})
}

func (b *bench) RouteFailed(plain []byte, next byte) {
	panic(fmt.Sprintf("route failed %x next %d: no vector expects it yet", plain, next))
}

func (b *bench) Up(raw []byte) {
	n, _, ok := proto.OpenNetwork(b.ccm, b.plain[:], raw)
	if !ok {
		panic("a frame went up that does not open")
	}
	b.trace = append(b.trace, got{at: b.now, up: append([]byte(nil), b.plain[:n]...)})
}

func loadVectors(t *testing.T) vectorFile {
	t.Helper()
	data, err := os.ReadFile("../../tests/vectors/lora_relay_behaviour.json")
	if err != nil {
		t.Fatal(err)
	}
	var v vectorFile
	if err := json.Unmarshal(data, &v); err != nil {
		t.Fatal(err)
	}
	return v
}

func airtime(sf uint8, n int) proto.Time { return sx126x.Airtime(sf, 125000, 5, 8, n) }

// replay runs one case and returns its trace.
func replay(t *testing.T, v vectorFile, c vectorCase) []got {
	t.Helper()
	b := &bench{node: v.Node, fcnt: v.FirstFcnt}
	keyBytes, _ := hex.DecodeString(v.NetworkKey)
	copy(b.key[:], keyBytes)
	b.ccm = proto.CCM{C: &softCipher{}, Key: &b.key}
	var r Relay
	cfg := DefaultConfig(v.Node, airtime)
	for name, value := range c.Config {
		switch name {
		case "wake_preamble":
			cfg.WakePreamble = uint16(value)
		default:
			t.Fatalf("config %s not known to the bench", name)
		}
	}
	r.Init(cfg, b.ccm, b, b, proto.Midpoint{})
	r.Asleep = c.Asleep
	r.Sealer, r.Failed = b, b

	advance := func(until proto.Time, inclusive bool) {
		for i := 0; i < 10000; i++ {
			due := max(b.now, r.NextDue())
			if due > until || (due == until && !inclusive) {
				return
			}
			b.now = due
			before, beforeDue := len(b.trace), r.NextDue()
			r.Tick(b.now)
			if r.NextDue() <= b.now && len(b.trace) == before && r.NextDue() == beforeDue {
				// NextDue must never name a time at which Tick does nothing:
				// firmware sleeping until NextDue would spin.
				t.Fatalf("NextDue %d at %d but Tick did nothing", r.NextDue(), b.now)
			}
		}
		t.Fatal("timers never settle")
	}
	for _, step := range c.Steps {
		advance(seconds(step.At), false)
		b.now = seconds(step.At)
		switch {
		case step.Rx != "":
			raw, _ := hex.DecodeString(step.Rx)
			rssi := NoRSSI
			if step.RSSI != nil {
				rssi = RSSI(math.Round(*step.RSSI * 1024))
			}
			r.OnRadio(raw, rssi, b.now)
		case step.Send != "":
			plain, _ := hex.DecodeString(step.Send)
			n, _ := b.SealFrame(b.sealed[:], plain)
			r.Send(b.sealed[:n], b.now)
		}
		r.Tick(b.now)
	}
	advance(seconds(c.End), true)
	return b.trace
}

// slack: Python counts seconds in floats, the firmware whole microseconds
// (the airtime formula alone rounds differently).
const slack = 5 * proto.Microsecond

func TestRelayBehaviourVectors(t *testing.T) {
	v := loadVectors(t)
	for _, c := range v.Cases {
		t.Run(c.Name, func(t *testing.T) {
			if !ported[c.Name] {
				t.Skip("not ported yet")
			}
			trace := replay(t, v, c)
			if len(trace) != len(c.Expect) {
				t.Fatalf("%d events, want %d:\n%s", len(trace), len(c.Expect), describe(trace, c.Expect))
			}
			for i, want := range c.Expect {
				g := trace[i]
				at := seconds(want.At)
				bad := g.at < at-slack || g.at > at+slack
				switch {
				case want.Tx != "":
					preamble := uint16(8)
					if want.Preamble != 0 {
						preamble = uint16(want.Preamble)
					}
					bad = bad || hex.EncodeToString(g.tx) != want.Tx || g.preamble != preamble
				case want.Up != "":
					bad = bad || hex.EncodeToString(g.up) != want.Up
				case want.Radio != nil:
					bad = bad || g.radio == nil || float64(g.radio[0]) != want.Radio["channel"] || float64(g.radio[1]) != want.Radio["sf"]
				default:
					t.Fatalf("event %d: kind not ported (%+v)", i, want)
				}
				if bad {
					t.Fatalf("event %d differs:\n%s", i, describe(trace, c.Expect))
				}
			}
		})
	}
}

func describe(trace []got, expect []event) string {
	s := "got:\n"
	for _, g := range trace {
		if g.tx != nil {
			s += fmt.Sprintf("  %.6f tx %x preamble %d\n", float64(g.at)/1e6, g.tx, g.preamble)
		} else if g.radio != nil {
			s += fmt.Sprintf("  %.6f radio %v\n", float64(g.at)/1e6, g.radio)
		} else {
			s += fmt.Sprintf("  %.6f up %x\n", float64(g.at)/1e6, g.up)
		}
	}
	s += "want:\n"
	for _, e := range expect {
		switch {
		case e.Tx != "":
			s += fmt.Sprintf("  %.6f tx %s preamble %d\n", e.At, e.Tx, e.Preamble)
		case e.Up != "":
			s += fmt.Sprintf("  %.6f up %s\n", e.At, e.Up)
		default:
			s += fmt.Sprintf("  %.6f %v %v\n", e.At, e.Failed, e.Radio)
		}
	}
	return s
}
