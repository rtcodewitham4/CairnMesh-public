package subghz

import "testing"

func TestLatchQuietStaysQuiet(t *testing.T) {
	var l Latch
	for i := 0; i < 3; i++ {
		if f, r := l.Take(); f || r {
			t.Fatalf("idle Take = %v %v", f, r)
		}
	}
}

func TestLatchFireThenRearmOnce(t *testing.T) {
	var l Latch
	l.Fire()
	if f, r := l.Take(); !f || r {
		t.Fatalf("after Fire: fired=%v rearm=%v, want fired only", f, r)
	}
	// The poll that saw fired clears the radio; the next poll unmasks the line.
	if f, r := l.Take(); f || !r {
		t.Fatalf("second Take: fired=%v rearm=%v, want rearm only", f, r)
	}
	if f, r := l.Take(); f || r {
		t.Fatalf("third Take: fired=%v rearm=%v, want nothing", f, r)
	}
}

func TestLatchManyFiresBeforeTakeCountOnce(t *testing.T) {
	var l Latch
	l.Fire()
	l.Fire()
	if f, _ := l.Take(); !f {
		t.Fatal("not fired")
	}
	if f, r := l.Take(); f || !r {
		t.Fatalf("fired=%v rearm=%v", f, r)
	}
}

func TestLatchFireAfterRearmIsSeen(t *testing.T) {
	var l Latch
	l.Fire()
	l.Take()
	l.Take() // rearm: the line is open again
	l.Fire() // the radio's next interrupt
	if f, r := l.Take(); !f || r {
		t.Fatalf("fired=%v rearm=%v, want fired", f, r)
	}
}

// A Fire between the poll that saw fired and the rearm cannot happen (the line
// is masked), but if it did, the fire is reported before the rearm.
func TestLatchFireBetweenTakeAndRearm(t *testing.T) {
	var l Latch
	l.Fire()
	l.Take()
	l.Fire()
	if f, r := l.Take(); !f || r {
		t.Fatalf("fired=%v rearm=%v, want fired", f, r)
	}
	if f, r := l.Take(); f || !r {
		t.Fatalf("fired=%v rearm=%v, want rearm", f, r)
	}
}

func TestLatchPrime(t *testing.T) {
	var l Latch
	l.Prime()
	if f, _ := l.Take(); !f {
		t.Fatal("Prime did not report fired")
	}
}
