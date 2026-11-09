package usb

import (
	"bytes"
	"fmt"
	"testing"
)

// fakePort records what the device asks of the controller.
type fakePort struct {
	log  []string
	sent [][]byte // EP0 IN packets
	data [][]byte // EP1 IN packets
	addr byte
}

func (f *fakePort) SetAddress(a byte) { f.addr = a; f.log = append(f.log, fmt.Sprint("addr ", a)) }
func (f *fakePort) OpenEndpoints()    { f.log = append(f.log, "open") }
func (f *fakePort) StallControl()     { f.log = append(f.log, "stall") }
func (f *fakePort) Receive(ep byte)   { f.log = append(f.log, fmt.Sprint("rx ", ep)) }
func (f *fakePort) Send(ep byte, d []byte) {
	f.log = append(f.log, fmt.Sprint("tx ", ep, " ", len(d)))
	c := append([]byte(nil), d...)
	if ep == EPControl {
		f.sent = append(f.sent, c)
	} else {
		f.data = append(f.data, c)
	}
}

func setup(typ, req byte, value, index, length uint16) []byte {
	return []byte{typ, req, byte(value), byte(value >> 8), byte(index), byte(index >> 8), byte(length), byte(length >> 8)}
}

// controlIn runs a control read to the end (the host takes every packet)
// and returns the data the host got.
func controlIn(d *Device, f *fakePort, p []byte) []byte {
	f.sent = nil
	d.Setup(p)
	var got []byte
	for i := 0; i < len(f.sent); i++ {
		got = append(got, f.sent[i]...)
		d.Sent(EPControl)
	}
	d.Received(EPControl, nil) // status OUT
	return got
}

func newDevice() (*Device, *fakePort) {
	f := &fakePort{}
	d := &Device{}
	d.Init(f, "00590047393650022037374E")
	d.Reset()
	return d, f
}

func TestEnumeration(t *testing.T) {
	d, f := newDevice()
	// Linux: GET_DESCRIPTOR(device, 64) first, then reset, SET_ADDRESS, the full descriptors.
	if got := controlIn(d, f, setup(0x80, 6, 0x0100, 0, 64)); !bytes.Equal(got, deviceDescriptor[:]) {
		t.Fatalf("device descriptor %x", got)
	}
	d.Reset()
	d.Setup(setup(0x00, 5, 7, 0, 0))
	if f.addr != 0 {
		t.Fatal("address applied before the status stage")
	}
	d.Sent(EPControl)
	if f.addr != 7 {
		t.Fatalf("address %d", f.addr)
	}
	if got := controlIn(d, f, setup(0x80, 6, 0x0200, 0, 9)); !bytes.Equal(got, configDescriptor[:9]) {
		t.Fatalf("config header %x", got)
	}
	got := controlIn(d, f, setup(0x80, 6, 0x0200, 0, 255))
	if !bytes.Equal(got, configDescriptor[:]) || len(f.sent) != 2 || len(f.sent[0]) != 64 {
		t.Fatalf("full config: %d bytes in %d packets", len(got), len(f.sent))
	}
	serial := controlIn(d, f, setup(0x80, 6, 0x0303, 0x0409, 255))
	if len(serial) != 2+48 || serial[2] != '0' || serial[1] != 3 {
		t.Fatalf("serial %x", serial)
	}
	if got := controlIn(d, f, setup(0x80, 6, 0x0300, 0, 255)); !bytes.Equal(got, langString[:]) {
		t.Fatalf("languages %x", got)
	}
	f.log = nil
	d.Setup(setup(0x80, 6, 0x0600, 0, 10)) // device qualifier: full-speed only
	if f.log[len(f.log)-1] != "stall" {
		t.Fatalf("qualifier: %v", f.log)
	}
	d.Setup(setup(0x00, 9, 1, 0, 0))
	if !d.Configured() || f.log[len(f.log)-3] != "open" {
		t.Fatalf("configure: %v", f.log)
	}
	// cdc_acm: SET_LINE_CODING (OUT data stage), SET_CONTROL_LINE_STATE(DTR).
	d.Setup(setup(0x21, 0x20, 0, 0, 7))
	f.sent = nil
	d.Received(EPControl, []byte{0x80, 0x25, 0, 0, 0, 0, 8})
	if len(f.sent) != 1 || len(f.sent[0]) != 0 {
		t.Fatal("line coding not acknowledged")
	}
	if got := controlIn(d, f, setup(0xA1, 0x21, 0, 0, 7)); got[0] != 0x80 || got[1] != 0x25 {
		t.Fatalf("line coding %x", got)
	}
	if d.Connected() {
		t.Fatal("connected before DTR")
	}
	d.Setup(setup(0x21, 0x22, 3, 0, 0))
	if !d.Connected() {
		t.Fatal("DTR not seen")
	}
}

func TestZeroLengthPacketEndsAnExactMultiple(t *testing.T) {
	d, f := newDevice()
	data := make([]byte, 64)
	d.in(data, 255)
	if len(f.sent) != 1 {
		t.Fatal("first packet")
	}
	d.Sent(EPControl)
	if len(f.sent) != 2 || len(f.sent[1]) != 0 {
		t.Fatalf("no ZLP after a full last packet: %d packets", len(f.sent))
	}
	d.Sent(EPControl)
	if len(f.sent) != 2 {
		t.Fatal("more after the ZLP")
	}
	// Exactly what the host asked: no ZLP.
	f.sent = nil
	d.in(data, 64)
	d.Sent(EPControl)
	if len(f.sent) != 1 {
		t.Fatalf("%d packets for an exact request", len(f.sent))
	}
}

func configured() (*Device, *fakePort) {
	d, f := newDevice()
	d.Setup(setup(0x00, 9, 1, 0, 0))
	d.Setup(setup(0x21, 0x22, 1, 0, 0))
	f.log, f.sent = nil, nil
	return d, f
}

func TestSerialData(t *testing.T) {
	d, f := configured()
	d.Received(EPData, []byte("help\r"))
	var buf [16]byte
	if n := d.Read(buf[:]); string(buf[:n]) != "help\r" {
		t.Fatalf("read %q", buf[:n])
	}
	// 100 bytes out: 64, then 36 once the first went.
	out := bytes.Repeat([]byte{'x'}, 100)
	if d.Write(out) != 100 || len(f.data) != 1 || len(f.data[0]) != 64 {
		t.Fatal("first packet")
	}
	d.Sent(EPData)
	if len(f.data) != 2 || len(f.data[1]) != 36 {
		t.Fatal("second packet")
	}
	d.Sent(EPData)
	// Exactly 64: a ZLP ends it.
	f.data = nil
	d.Write(out[:64])
	d.Sent(EPData)
	if len(f.data) != 2 || len(f.data[1]) != 0 {
		t.Fatalf("no ZLP after a full packet: %d", len(f.data))
	}
	d.Sent(EPData)
	if d.WriteString("ok\r\n") != 4 || string(f.data[2]) != "ok\r\n" {
		t.Fatal("WriteString")
	}
}

func TestReceiveFlowControl(t *testing.T) {
	d, f := configured()
	packet := bytes.Repeat([]byte{'a'}, 64)
	for i := 0; i < ringSize/64; i++ {
		d.Received(EPData, packet)
	}
	f.log = nil
	// Full: EP1 stays NAKed until the program reads.
	if d.rxArmed || d.Buffered() != ringSize {
		t.Fatal("re-armed with no room")
	}
	var buf [64]byte
	d.Read(buf[:])
	if !d.rxArmed || f.log[len(f.log)-1] != "rx 1" {
		t.Fatalf("not re-armed after a read: %v", f.log)
	}
	if d.Stats.RxDropped != 0 {
		t.Fatal("dropped")
	}
}

func TestUnconfiguredDropsOutput(t *testing.T) {
	d, f := newDevice()
	if d.Write([]byte("x")) != 0 || len(f.data) != 0 {
		t.Fatal("output before configuration")
	}
	d.Setup(setup(0x40, 1, 0, 0, 0)) // vendor request: stall
	if d.Stats.Stalls != 1 {
		t.Fatal("vendor request not stalled")
	}
}
