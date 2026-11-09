package main

import (
	"os"
	"testing"
)

// The on-board table must follow the vector file: make vectors after it changed.
func TestSelftestTableIsCurrent(t *testing.T) {
	want, err := generate("../../../tests/vectors/lora_v4.json")
	if err != nil {
		t.Fatal(err)
	}
	got, err := os.ReadFile("../../cmd/selftest/vectors_gen.go")
	if err != nil {
		t.Fatal(err)
	}
	if string(got) != want {
		t.Fatal("cmd/selftest/vectors_gen.go is stale: run make vectors")
	}
}
