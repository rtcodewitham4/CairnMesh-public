package benchactions_test

import (
	"testing"

	"cairnmesh/firmware/node/nodetest"
	"cairnmesh/firmware/proto"
	"cairnmesh/firmware/testbed/benchactions"
)

func resultOf(frames [][]byte, seq byte) int {
	for _, f := range frames {
		if f[0] == proto.KindResult && f[3] == seq {
			return int(f[5])
		}
	}
	return -1
}

func TestAllBenchActionsAreRegistered(t *testing.T) {
	bench := nodetest.New(nodetest.Options{}, &benchactions.Actions{})
	for code := byte(2); code <= 5; code++ {
		bench.Request(code, 0)
	}
	if got := bench.Stack.Stats.UnknownAction; got != 0 {
		t.Fatalf("actions 0x02-0x05 produced %d unknown-action results", got)
	}
}

func TestBenchActionResults(t *testing.T) {
	bench := nodetest.New(nodetest.Options{}, &benchactions.Actions{})

	fails := bench.Request(3, 0)
	if got := nodetest.Answer(bench.Sent(), fails); got != proto.ResultError {
		t.Fatalf("action 0x03 answered %d, want ERROR", got)
	}
	instant := bench.Request(4, 0)
	if got := nodetest.Answer(bench.Sent(), instant); got != proto.ResultOK {
		t.Fatalf("action 0x04 answered %d, want OK", got)
	}

	later := bench.Request(5, 20)
	if got := nodetest.Answer(bench.Sent(), later); got != proto.ResultAccepted {
		t.Fatalf("action 0x05 answered %d, want ACCEPTED", got)
	}
	bench.Advance(1990*proto.Millisecond, 10*proto.Millisecond)
	bench.Advance(20*proto.Millisecond, 10*proto.Millisecond)
	if got := resultOf(bench.Sent(), later); got != proto.ResultOK {
		t.Fatalf("action 0x05 completed with %d, want OK", got)
	}
}
