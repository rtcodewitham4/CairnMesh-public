package keystore

import (
	"bytes"
	"crypto/aes"
	stdcrc "hash/crc32"
	"testing"

	"cairnmesh/firmware/node/nodetest"
)

func sample() Record {
	r := Record{Node: 7, AppKeyVersion: 2, Network: nodetest.NetworkKey, App: nodetest.AppKey}
	return r
}

func TestRoundTrip(t *testing.T) {
	r := sample()
	var b [RecordSize]byte
	r.Encode(&b)
	if crc32(b[:60]) != stdcrc.ChecksumIEEE(b[:60]) {
		t.Fatal("crc32 differs from hash/crc32")
	}
	var got Record
	if !got.Decode(b[:]) || got != r {
		t.Fatalf("decode %+v", got)
	}
}

func TestRejects(t *testing.T) {
	r := sample()
	var b [RecordSize]byte
	r.Encode(&b)
	erased := bytes.Repeat([]byte{0xFF}, RecordSize)
	var got Record
	if got.Decode(erased) {
		t.Error("erased flash accepted")
	}
	for i := 0; i < 60; i++ {
		c := b
		c[i] ^= 0x10
		if got.Decode(c[:]) {
			t.Errorf("flipped bit in byte %d accepted", i)
		}
	}
	for _, id := range []byte{0, 0x42, 0xFF} {
		bad := r
		bad.Node = id
		bad.Encode(&b)
		if got.Decode(b[:]) {
			t.Errorf("node %#x accepted", id)
		}
	}
}

func TestKCV(t *testing.T) {
	var c nodetest.Cipher
	key := nodetest.NetworkKey
	block, _ := aes.NewCipher(key[:])
	var want [16]byte
	block.Encrypt(want[:], make([]byte, 16))
	if got := KCV(&c, &key); !bytes.Equal(got[:], want[:3]) {
		t.Fatalf("kcv %x, want %x", got, want[:3])
	}
}
