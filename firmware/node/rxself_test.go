package node_test

import (
	"crypto/sha256"
	"fmt"
	"testing"

	"cairnmesh/firmware/node/nodetest"
	"cairnmesh/firmware/proto"
)

// Self-check of the receive test filter (rxhook_on.go, tag cairn_testfilter):
// with the default matrix (pass all) a node behaves exactly as one built
// without the filter. This test has no build tag and the same expected value
// in both builds: it runs in `go test ./node/` (no filter in the binary) and
// in `go test -tags cairn_testfilter ./node/` (filter in the binary, default
// matrix); both must produce one digest of everything the scenario shows
// (every frame on air, byte for byte, the counters, what ran).

const rxSelfDigest = "e1e8705111d6181d700f606345afe73530517bb007190561ec920a108e4d789d"

func rxScenario(opts nodetest.Options) string {
	a := &probe{code: 1}
	b := nodetest.New(opts, a)
	h := sha256.New()
	note := func(label string) {
		for _, f := range b.Raw() {
			fmt.Fprintf(h, "%s out %x\n", label, f)
		}
	}
	b.Request(1, 3)
	note("request")
	b.Retry(1, 1, 3) // a repeat of the same message ID
	note("retry")
	b.Request(1, 0xEE) // an ERROR result
	note("error")
	b.Advance(3*proto.Second, 100*proto.Millisecond)
	note("idle")
	if opts.Mesh {
		for i, tx := range []byte{0x07, 0x09, 0x07, 0x42, 0x0b} {
			b.Inject([]byte{proto.KindWake, ctrl, 0x09 + byte(i), byte(7 + i), 0}, tx)
			b.Advance(2*proto.Second, 10*proto.Millisecond)
			note(fmt.Sprintf("wake-%d", i))
		}
		b.Inject([]byte{proto.KindRequest | proto.FlagRouted, ctrl, nodetest.NodeID, 5, 0, 1, 3, 0x07, 1}, 0x07)
		note("routed")
		b.Advance(4*proto.Second, 10*proto.Millisecond)
		note("routed-after")
	}
	fmt.Fprintf(h, "ran %v sec %+v stack %+v\n", a.values, b.Stack.Sec.Stats, b.Stack.Stats)
	return fmt.Sprintf("%x", h.Sum(nil))
}

func TestRxFilterDefaultBehavesLikeNoFilter(t *testing.T) {
	got := rxScenario(nodetest.Options{}) + rxScenario(nodetest.Options{Mesh: true})
	sum := fmt.Sprintf("%x", sha256.Sum256([]byte(got)))
	if sum != rxSelfDigest {
		t.Fatalf("scenario digest %s, want %s (a filter in the build changed the node's behaviour, or the scenario changed)", sum, rxSelfDigest)
	}
}
