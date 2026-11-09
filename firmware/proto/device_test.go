package proto

import (
	"encoding/hex"
	"encoding/json"
	"fmt"
	"math"
	"os"
	"strconv"
	"testing"
)

// The device behaviour vectors (spec 16.1), replayed with the rules of
// conformance.replay: time event by event, jitter off (Midpoint), the
// application a table per ACTION.

type behaviourCase struct {
	Name    string                     `json:"name"`
	Config  map[string]json.RawMessage `json:"config"`
	Actions map[string]struct {
		Time    float64 `json:"time"`
		Result  *byte   `json:"result"`
		Pending bool    `json:"pending"`
	} `json:"actions"`
	Steps []struct {
		At       float64    `json:"at"`
		RX       string     `json:"rx"`
		Reboot   bool       `json:"reboot"`
		Complete *completed `json:"complete"`
	} `json:"steps"`
	End    float64 `json:"end"`
	Expect []struct {
		At  float64 `json:"at"`
		TX  string  `json:"tx"`
		Run *struct {
			Action, Value byte
			Result        int
		} `json:"run"`
		Complete *completed `json:"complete"`
	} `json:"expect"`
}

type completed struct {
	Sender, Sequence, Epoch, Result byte
	Taken                           bool
}

func seconds(s float64) Time { return Time(math.Round(s * 1e6)) }

type tableApp struct {
	c     *behaviourCase
	trace *[]string
	now   *Time
}

func (a tableApp) Duration(action, _ byte) Time {
	return seconds(a.c.Actions[strconv.Itoa(int(action))].Time)
}

func (a tableApp) Perform(r Request) byte {
	entry := a.c.Actions[strconv.Itoa(int(r.Action))]
	result := byte(ResultOK)
	if entry.Result != nil {
		result = *entry.Result
	}
	logged := int(result)
	if entry.Pending {
		result, logged = ResultPending, -1
	}
	*a.trace = append(*a.trace, fmt.Sprintf("%d run %d %d -> %d", *a.now, r.Action, r.Value, logged))
	return result
}

type traceSender struct {
	trace *[]string
	now   *Time
}

func (s traceSender) Send(plain []byte) {
	*s.trace = append(*s.trace, fmt.Sprintf("%d tx %x", *s.now, plain))
}

func deviceConfig(t *testing.T, raw map[string]json.RawMessage) DeviceConfig {
	t.Helper()
	cfg := DefaultDeviceConfig(1)
	num := func(name string) float64 {
		var v float64
		if err := json.Unmarshal(raw[name], &v); err != nil {
			t.Fatalf("config %s: %v", name, err)
		}
		return v
	}
	for name, value := range raw {
		switch name {
		case "device_id":
			cfg.ID = byte(num(name))
		case "controller_id":
			cfg.Controller = byte(num(name))
		case "awake":
			if err := json.Unmarshal(value, &cfg.Awake); err != nil {
				t.Fatal(err)
			}
		case "heartbeat_interval":
			cfg.HeartbeatInterval = seconds(num(name))
		case "dedup_window":
			cfg.DedupWindow = int(num(name))
		case "dedup_ttl":
			if string(value) == "null" {
				cfg.DedupTTL = Never
			} else {
				cfg.DedupTTL = seconds(num(name))
			}
		case "max_senders":
			cfg.MaxSenders = int(num(name))
		case "max_open":
			cfg.MaxOpen = int(num(name))
		case "max_deferred":
			cfg.MaxDeferred = int(num(name))
		default:
			t.Fatalf("config %s is not mapped", name)
		}
	}
	return cfg
}

// memLog is a RecordLog in RAM that compacts (rewrites itself from
// EachRecord, as journal.Records does) every so many writes; 0: never.
type memLog struct {
	entries     []memEntry
	dev         *Device
	every, n    int
	failFrom    int // writes from this one on fail; 0: none
	compactions int
}

type memEntry struct {
	sender      byte
	id          uint16
	result      byte
	open, touch bool
}

func (l *memLog) add(e memEntry) bool {
	l.n++
	if l.failFrom > 0 && l.n >= l.failFrom {
		return false
	}
	if l.every > 0 && l.n%l.every == 0 {
		l.entries = l.entries[:0]
		l.compactions++
		l.dev.EachRecord(l)
	}
	l.entries = append(l.entries, e)
	return true
}

func (l *memLog) Write(sender byte, id uint16, result byte, open bool) bool {
	return l.add(memEntry{sender, id, result, open, false})
}

func (l *memLog) Touch(sender byte) bool { return l.add(memEntry{sender: sender, touch: true}) }

func (l *memLog) Record(sender byte, id uint16, result byte, open bool) bool {
	l.entries = append(l.entries, memEntry{sender, id, result, open, false})
	return true
}

// boot is a power cut: d starts over from the log alone.
func (l *memLog) boot(d *Device, cfg DeviceConfig, app Application, out FrameSender, now Time) {
	d.Init(cfg, app, out, Midpoint{}, now)
	for _, e := range l.entries {
		if e.touch {
			d.RestoreTouch(e.sender)
		} else {
			d.Restore(e.sender, e.id, e.result, e.open)
		}
	}
	d.Reset(now)
	l.dev = d
	d.SetLog(l)
}

func replayDevice(t *testing.T, c *behaviourCase) []string { return replayDeviceLog(t, c, nil) }

// replayDeviceLog replays a case; with log, every reboot is a power cut
// (the records come back from the log only).
func replayDeviceLog(t *testing.T, c *behaviourCase, log *memLog) []string {
	var trace []string
	var now Time
	var d Device
	cfg, app, out := deviceConfig(t, c.Config), tableApp{c, &trace, &now}, traceSender{&trace, &now}
	if log != nil {
		log.boot(&d, cfg, app, out, now)
	} else {
		d.Init(cfg, app, out, Midpoint{}, now)
	}
	advance := func(until Time, inclusive bool) {
		for i := 0; i < 10000; i++ {
			due := d.NextDue()
			if due > until || due == until && !inclusive {
				return
			}
			now = due
			d.Tick(now)
		}
		t.Fatalf("%s: timers never settle", c.Name)
	}
	for _, step := range c.Steps {
		at := seconds(step.At)
		advance(at, false)
		now = at
		if step.RX != "" {
			frame, err := hex.DecodeString(step.RX)
			if err != nil {
				t.Fatal(err)
			}
			d.OnFrame(frame, now)
		} else if step.Reboot && log != nil {
			log.boot(&d, cfg, app, out, now)
		} else if step.Reboot {
			d.Reset(now)
		} else if c := step.Complete; c != nil {
			taken := d.Complete(c.Sender, uint16(c.Epoch)<<8|uint16(c.Sequence), c.Result, now)
			trace = append(trace, fmt.Sprintf("%d complete %d %d %d %d -> %v", now, c.Sender, c.Sequence, c.Epoch, c.Result, taken))
		}
		d.Tick(now)
	}
	advance(seconds(c.End), true)
	return trace
}

func TestDeviceBehaviourVectors(t *testing.T) {
	for _, c := range loadBehaviourCases(t) {
		t.Run(c.Name, func(t *testing.T) { checkTrace(t, c, replayDevice(t, c)) })
	}
}

// Every reboot a power cut, the records read back from a log (spec 4.1):
// the same answers as records kept in RAM, whether the log compacts or not.
func TestDeviceBehaviourVectorsThroughLog(t *testing.T) {
	cases := loadBehaviourCases(t)
	reboots := 0
	for _, every := range []int{0, 1, 2, 3, 7} {
		for _, c := range cases {
			for _, s := range c.Steps {
				if s.Reboot && every == 0 {
					reboots++
				}
			}
			t.Run(fmt.Sprintf("%s/compact%d", c.Name, every), func(t *testing.T) {
				checkTrace(t, c, replayDeviceLog(t, c, &memLog{every: every}))
			})
		}
	}
	if reboots == 0 {
		t.Fatal("no case reboots")
	}
}

func loadBehaviourCases(t *testing.T) []*behaviourCase {
	raw, err := os.ReadFile("../../tests/vectors/lora_device_behaviour.json")
	if err != nil {
		t.Fatal(err)
	}
	var file struct {
		Cases []behaviourCase `json:"cases"`
	}
	if err := json.Unmarshal(raw, &file); err != nil {
		t.Fatal(err)
	}
	if len(file.Cases) == 0 {
		t.Fatal("no cases")
	}
	cases := make([]*behaviourCase, len(file.Cases))
	for i := range file.Cases {
		cases[i] = &file.Cases[i]
	}
	return cases
}

func checkTrace(t *testing.T, c *behaviourCase, got []string) {
	t.Helper()
	var want []string
	for _, e := range c.Expect {
		if e.Run != nil {
			want = append(want, fmt.Sprintf("%d run %d %d -> %d", seconds(e.At), e.Run.Action, e.Run.Value, e.Run.Result))
		} else if c := e.Complete; c != nil {
			want = append(want, fmt.Sprintf("%d complete %d %d %d %d -> %v", seconds(e.At), c.Sender, c.Sequence, c.Epoch, c.Result, c.Taken))
		} else {
			want = append(want, fmt.Sprintf("%d tx %s", seconds(e.At), e.TX))
		}
	}
	for j := 0; j < max(len(got), len(want)); j++ {
		var g, w string
		if j < len(got) {
			g = got[j]
		}
		if j < len(want) {
			w = want[j]
		}
		if g != w {
			t.Errorf("event %d: got %q, want %q", j, g, w)
		}
	}
}
