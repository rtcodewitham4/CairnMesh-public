package stackwater

import "testing"

func TestParseArgs(t *testing.T) {
	if !ParseArgs([][]byte{[]byte("stack")}) {
		t.Fatal("stack without arguments was refused")
	}
	if ParseArgs(nil) || ParseArgs([][]byte{[]byte("stack"), []byte("extra")}) {
		t.Fatal("stack with the wrong argument count was accepted")
	}
}

func TestScanSystemFindsLowestChangedWord(t *testing.T) {
	const base = 0x20000000
	words := []uint32{SystemFill, SystemFill, SystemFill, 7, SystemFill, 9, SystemFill, SystemFill}
	got := ScanSystem(words, base)
	if !got.Found || !got.CanaryOK {
		t.Fatalf("not found: %+v", got)
	}
	if got.Low != base+3*4 || got.Used != 20 || got.Free != 12 || got.Top != base+32 {
		t.Fatalf("got %+v", got)
	}
}

func TestScanSystemUntouched(t *testing.T) {
	words := make([]uint32, SystemStackWords)
	for i := range words {
		words[i] = SystemFill
	}
	got := ScanSystem(words, 0x20000000)
	if !got.Found || got.Used != 0 || got.Free != SystemStackSize || got.Low != got.Top {
		t.Fatalf("got %+v", got)
	}
}

func TestFindTaskCanaryUsesHighestCandidate(t *testing.T) {
	window := make([]uint32, 64)
	window[3] = TaskCanary
	window[40] = TaskCanary
	if got := FindTaskCanary(window); got != 40 {
		t.Fatalf("candidate index %d, want 40", got)
	}
	window[40] = 0
	if got := FindTaskCanary(window); got != 3 {
		t.Fatalf("candidate index %d, want 3", got)
	}
}

func TestScanTask(t *testing.T) {
	const base = 0x20002000
	words := make([]uint32, 16)
	words[0] = TaskCanary
	words[5] = 0
	words[7] = 0x1234
	words[10] = 0x5678
	got := ScanTask(words, base)
	if !got.Found || !got.CanaryOK {
		t.Fatalf("not found: %+v", got)
	}
	if got.Low != base+7*4 || got.Used != 36 || got.Free != 24 || got.Top != base+64 {
		t.Fatalf("got %+v", got)
	}
}

func TestScanTaskEmptyAndCanaryMismatch(t *testing.T) {
	words := make([]uint32, TaskStackWords)
	words[0] = TaskCanary
	got := ScanTask(words, 0x20002000)
	if !got.Found || got.Used != 0 || got.Free != TaskStackSize-4 || got.Low != got.Top {
		t.Fatalf("empty task stack: %+v", got)
	}

	words[0] = 0
	got = ScanTask(words, 0x20002000)
	if got.Found || got.CanaryOK {
		t.Fatalf("bad canary accepted: %+v", got)
	}
}
