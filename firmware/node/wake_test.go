package node

import (
	"testing"

	"cairnmesh/firmware/mesh"
	"cairnmesh/firmware/proto"
)

func TestWakeRefillCapsLongGaps(t *testing.T) {
	const hour = 60 * 60 * proto.Second
	cases := []struct {
		name string
		gap  proto.Time
	}{
		{"1h", hour},
		{"4h", 4 * hour},
		{"5h", 5 * hour},
		{"12h", 12 * hour},
		{"24h", 24 * hour},
		{"48h", 48 * hour},
		{"7d", 7 * 24 * hour},
	}
	for _, tt := range cases {
		t.Run(tt.name, func(t *testing.T) {
			cfg := DefaultWakeConfig()
			w := Waker{
				cfg:      cfg,
				budget:   0,
				budgetAt: 0,
			}
			w.refill(tt.gap)
			if w.budget != cfg.NetAwakeQuota {
				t.Fatalf("budget after %s = %d, want full quota %d", tt.name, w.budget, cfg.NetAwakeQuota)
			}
		})
	}
}

func TestWakeNetAcceptedAfterLongDormancy(t *testing.T) {
	cfg := DefaultWakeConfig()
	w := Waker{
		cfg:      cfg,
		budget:   0,
		budgetAt: 0,
	}
	if !w.allow(24*60*60*proto.Second, []byte{proto.KindWakeNet}) {
		t.Fatal("WAKE_NET refused after 24 h")
	}
	if w.budget < proto.Second {
		t.Fatalf("budget after WAKE_NET = %d, want at least 1 s", w.budget)
	}
}

func TestWakeInitDoesNotCallOnWakeWhenAlreadyAwake(t *testing.T) {
	var relay mesh.Relay
	cfg := DefaultWakeConfig()
	cfg.Dormant = false
	calls := 0
	w := Waker{}
	w.init(cfg, &relay, nil, 1, func(proto.Time) { calls++ }, 0)
	if calls != 0 {
		t.Fatalf("onWake called %d times while initializing awake, want 0", calls)
	}
	w.set(true, proto.Second)
	w.set(false, 2*proto.Second)
	if calls != 1 {
		t.Fatalf("onWake called %d times after waking, want 1", calls)
	}
}
