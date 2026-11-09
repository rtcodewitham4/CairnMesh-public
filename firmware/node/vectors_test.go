package node_test

import (
	"encoding/hex"
	"encoding/json"
	"fmt"
	"math"
	"os"
	"strings"
	"testing"

	"cairnmesh/firmware/mesh"
	"cairnmesh/firmware/node"
	"cairnmesh/firmware/node/nodetest"
	"cairnmesh/firmware/proto"
	"cairnmesh/firmware/radio/sx126x"
)

// The node behaviour vectors (spec 16.3): a multi-hop node.Stack with its
// HELLO table, wake layer and discovery, replayed by the file's rules (those
// of the relay vectors, and every case starts with a boot at 0 s).

type nodeFile struct {
	Node       byte   `json:"node"`
	Controller byte   `json:"controller"`
	FirstFcnt  uint32 `json:"first_fcnt"`
	Cases      []nodeCase
}

type nodeCase struct {
	Name   string
	Config struct {
		HelloInterval *float64           `json:"hello_interval"`
		Watch         map[string]float64 `json:"watch"`
		Wake          map[string]float64 `json:"wake"`
		Discovery     map[string]float64 `json:"discovery"`
		Relay         map[string]float64 `json:"relay"`
	}
	Steps []struct {
		At     float64
		Rx     string
		RSSI   *float64 `json:"rssi"`
		Notify *byte    `json:"notify"`
	}
	End    float64
	Expect []nodeEvent
}

type nodeEvent struct {
	At       float64        `json:"at"`
	Tx       string         `json:"tx,omitempty"`
	Preamble int            `json:"preamble,omitempty"`
	Dormant  *bool          `json:"dormant,omitempty"`
	Radio    map[string]any `json:"radio,omitempty"`
}

func secs(s float64) proto.Time { return proto.Time(math.Round(s * 1e6)) }

// vectorAir is the node's radio on the bench: what is heard (with its RSSI)
// and a trace of what it does.
type vectorAir struct {
	now   *proto.Time
	in    [][]byte
	rssi  []int16
	last  int16
	trace []string
}

func (a *vectorAir) log(format string, args ...any) {
	a.trace = append(a.trace, fmt.Sprintf("%.6f ", float64(*a.now)/1e6)+fmt.Sprintf(format, args...))
}

func (a *vectorAir) Send(p []byte) bool { a.log("tx %x p=8", p); return true }

func (a *vectorAir) SendPreamble(p []byte, symbols uint16) bool {
	a.log("tx %x p=%d", p, symbols)
	return true
}

func (a *vectorAir) Receive() []byte {
	if len(a.in) == 0 {
		return nil
	}
	p := a.in[0]
	a.in, a.last, a.rssi = a.in[1:], a.rssi[0], a.rssi[1:]
	return p
}

func (a *vectorAir) PacketRSSI() (int16, bool)  { return a.last, true }
func (a *vectorAir) SetDormant(d bool)          { a.log("dormant %v", d) }
func (a *vectorAir) SetRadio(channel, sf uint8) { a.log("radio %d %d", channel, sf) }

func expected(e nodeEvent) string {
	at := fmt.Sprintf("%.6f ", float64(secs(e.At))/1e6)
	switch {
	case e.Tx != "":
		p := e.Preamble
		if p == 0 {
			p = 8
		}
		return at + fmt.Sprintf("tx %s p=%d", e.Tx, p)
	case e.Dormant != nil:
		return at + fmt.Sprintf("dormant %v", *e.Dormant)
	default:
		return at + fmt.Sprintf("radio %v %v", e.Radio["channel"], e.Radio["sf"])
	}
}

func applyWatch(t *testing.T, c *node.WatchConfig, opts map[string]float64) {
	for k, v := range opts {
		switch k {
		case "max_neighbours":
			c.MaxNeighbours = int(v)
		default:
			t.Fatalf("watch option %s", k)
		}
	}
}

func applyWake(t *testing.T, c *node.WakeConfig, opts map[string]float64) {
	for k, v := range opts {
		switch k {
		case "net_awake_quota":
			c.NetAwakeQuota = secs(v)
		default:
			t.Fatalf("wake option %s", k)
		}
	}
}

// The benches' stack and relay: large, never on the goroutine stack.
var (
	vectorStack node.Stack
	vectorRelay mesh.Relay
)

func replayNode(t *testing.T, file *nodeFile, c *nodeCase) []string {
	now := proto.Time(0)
	air := &vectorAir{now: &now}
	cfg := proto.DefaultDeviceConfig(file.Node)
	cfg.Controller = file.Controller
	cfg.Awake = true
	cfg.HeartbeatInterval = proto.Never
	mc := mesh.DefaultConfig(file.Node, func(sf uint8, n int) proto.Time { return sx126x.Airtime(sf, 125000, 5, 8, n) })
	for k, v := range c.Config.Relay {
		switch k {
		case "wake_preamble":
			mc.WakePreamble = uint16(v)
		default:
			t.Fatalf("relay option %s", k)
		}
	}
	conf := node.Config{
		Device: cfg, NetworkKey: &nodetest.NetworkKey, LastFcnt: file.FirstFcnt,
		Cipher: &nodetest.Cipher{}, Rand: proto.Midpoint{}, Air: air,
		Relay: &vectorRelay, Mesh: &mc, Epoch: file.Node, FixedEpoch: true,
	}
	if c.Config.HelloInterval != nil {
		w := node.DefaultWatchConfig()
		w.HelloInterval = secs(*c.Config.HelloInterval)
		applyWatch(t, &w, c.Config.Watch)
		conf.Watch = &w
	}
	if c.Config.Wake != nil {
		w := node.DefaultWakeConfig()
		applyWake(t, &w, c.Config.Wake)
		conf.Wake = &w
	}
	if c.Config.Discovery != nil {
		if len(c.Config.Discovery) != 0 {
			t.Fatalf("discovery options %v", c.Config.Discovery)
		}
		d := node.DefaultDiscoveryConfig()
		conf.Discovery = &d
	}
	s := &vectorStack
	*s = node.Stack{}
	vectorRelay = mesh.Relay{}
	s.Init(conf)
	if !s.Start(now) {
		t.Fatalf("start: %+v", s.Fault)
	}
	advance := func(until proto.Time, inclusive bool) {
		for i := 0; i < 10000; i++ {
			due := max(now, s.NextDue())
			if due > until || (due == until && !inclusive) {
				return
			}
			now = due
			before, beforeDue := len(air.trace), s.NextDue()
			s.Poll(now)
			if s.NextDue() <= now && len(air.trace) == before && s.NextDue() == beforeDue {
				t.Fatalf("NextDue %d at %d but Poll did nothing", s.NextDue(), now)
			}
		}
		t.Fatal("timers never settle")
	}
	for _, step := range c.Steps {
		at := secs(step.At)
		advance(at, false)
		now = at
		switch {
		case step.Rx != "":
			raw, _ := hex.DecodeString(step.Rx)
			air.in, air.rssi = append(air.in, raw), append(air.rssi, int16(*step.RSSI))
		case step.Notify != nil:
			s.Events.Notify(*step.Notify, now)
		}
		s.Poll(now)
	}
	advance(secs(c.End), true)
	return air.trace
}

func TestNodeBehaviourVectors(t *testing.T) {
	data, err := os.ReadFile("../../tests/vectors/lora_node_behaviour.json")
	if err != nil {
		t.Fatal(err)
	}
	var file nodeFile
	if err := json.Unmarshal(data, &file); err != nil {
		t.Fatal(err)
	}
	if len(file.Cases) != 24 {
		t.Fatalf("only %d cases", len(file.Cases))
	}
	for i := range file.Cases {
		c := &file.Cases[i]
		t.Run(c.Name, func(t *testing.T) {
			got := replayNode(t, &file, c)
			want := make([]string, len(c.Expect))
			for j, e := range c.Expect {
				want[j] = expected(e)
			}
			if strings.Join(got, "\n") != strings.Join(want, "\n") {
				t.Fatalf("trace differs\n got:\n  %s\nwant:\n  %s", strings.Join(got, "\n  "), strings.Join(want, "\n  "))
			}
		})
	}
}
