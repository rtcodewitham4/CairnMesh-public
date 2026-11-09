package wiring

import (
	"testing"

	"cairnmesh/firmware/ext"
	"cairnmesh/firmware/node"
	"cairnmesh/firmware/node/nodetest"
	"cairnmesh/firmware/proto"
)

type productOutput struct{ on bool }

func (o *productOutput) Set(on bool) { o.on = on }

func TestProductionActionTableHasOnlyLED(t *testing.T) {
	out := &productOutput{}
	products := Products{LED: ext.Switch{Out: out, Code: ProductLEDAction, Claim: node.User}}
	bench := nodetest.New(nodetest.Options{}, products.Module())
	if !bench.Started {
		t.Fatalf("production modules did not start: %+v", bench.Stack.Fault)
	}

	led := bench.Request(ProductLEDAction, 1)
	if got := nodetest.Answer(bench.Sent(), led); got != proto.ResultOK {
		t.Fatalf("LED action answered %d, want OK", got)
	}
	if !out.on {
		t.Fatal("LED action did not set the output")
	}

	for action := byte(0x02); action <= 0x05; action++ {
		seq := bench.Request(action, 0)
		if got := nodetest.Answer(bench.Sent(), seq); got != proto.ResultError {
			t.Errorf("action 0x%02x answered %d, want ERROR outside the testbed build", action, got)
		}
	}
	if got := bench.Stack.Stats.UnknownAction; got != 4 {
		t.Fatalf("unknown actions = %d, want 4", got)
	}
}
