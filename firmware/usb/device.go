// Package usb is the USB device side of the node's debug console: a CDC-ACM
// serial port (the host sees /dev/ttyACM*, no driver needed). It holds the
// descriptors, the control endpoint's state machine and the serial port's
// buffers; the controller is behind Port (hw/usbhw on the STM32U083, a fake
// in the tests). Pure Go, no allocation after Init.
//
// Endpoints: 0 control, 1 bulk IN and OUT (the serial data), 2 interrupt
// IN (CDC notifications, never sent). All 64 bytes but EP2 (8).
package usb

// Endpoint numbers.
const (
	EPControl = 0
	EPData    = 1
	EPNotify  = 2

	MaxPacket       = 64
	NotifyMaxPacket = 8
)

// Port is the USB controller as the device sees it.
type Port interface {
	// SetAddress applies the device address (after SET_ADDRESS's status stage).
	SetAddress(addr byte)
	// OpenEndpoints enables EP1 (bulk IN / OUT) and EP2 (interrupt IN)
	// after SET_CONFIGURATION, data toggles reset.
	OpenEndpoints()
	// Send arms one IN packet on ep (at most the endpoint's max packet
	// size; empty: a zero-length packet). Sent follows when the host took it.
	Send(ep byte, data []byte)
	// Receive arms ep for the next OUT packet (on EP0: a data or status stage).
	Receive(ep byte)
	// StallControl stalls EP0 in both directions until the next SETUP.
	StallControl()
}

// Standard requests (USB 2.0 table 9-4) and the CDC ones used here.
const (
	reqGetStatus        = 0x00
	reqClearFeature     = 0x01
	reqSetAddress       = 0x05
	reqGetDescriptor    = 0x06
	reqGetConfiguration = 0x08
	reqSetConfiguration = 0x09
	reqSetInterface     = 0x0B

	cdcSetLineCoding       = 0x20
	cdcGetLineCoding       = 0x21
	cdcSetControlLineState = 0x22
	cdcSendBreak           = 0x23
)

// Stats counts what the device saw.
type Stats struct {
	Resets, Setups, Stalls uint32
	// RxDropped: OUT bytes that found the receive buffer full (cannot
	// happen: EP1 is re-armed only when a packet fits).
	RxDropped uint32
}

// Device is the USB device: one CDC-ACM serial port.
type Device struct {
	port   Port
	config byte

	// The control transfer in progress: IN data still to send, whether a
	// zero-length packet must end it, an OUT data stage expected.
	ctlData     []byte
	ctlZLP      bool
	ctlOut      byte // request of the OUT data stage, 0: none
	pendingAddr byte
	addrPending bool
	scratch     [8]byte

	lineCoding [7]byte
	dtr        bool

	rx      ring
	tx      ring
	txBusy  bool
	txFull  bool // the last packet sent was full: a ZLP ends the transfer
	rxArmed bool
	packet  [MaxPacket]byte

	desc  descriptors
	Stats Stats
}

// Init sets the device up; serial is the iSerialNumber string (ASCII, at
// most 24 characters: the chip's unique ID in hexadecimal).
func (d *Device) Init(p Port, serial string) {
	d.port = p
	d.desc.init(serial)
	// 115200 8N1 until the host says otherwise (dwDTERate LE, stop, parity, bits).
	d.lineCoding = [7]byte{0x00, 0xC2, 0x01, 0x00, 0, 0, 8}
}

// Configured reports whether the host configured the device.
func (d *Device) Configured() bool { return d.config != 0 }

// Connected reports whether a program on the host holds the port open
// (DTR set, SET_CONTROL_LINE_STATE).
func (d *Device) Connected() bool { return d.config != 0 && d.dtr }

// Reset handles a bus reset: back to address 0, unconfigured, EP0 ready.
func (d *Device) Reset() {
	d.Stats.Resets++
	d.config, d.dtr = 0, false
	d.ctlData, d.ctlZLP, d.ctlOut, d.addrPending = nil, false, 0, false
	d.txBusy, d.txFull, d.rxArmed = false, false, false
	d.rx.clear()
	d.tx.clear()
	d.port.Receive(EPControl)
}

// Setup handles the 8-byte SETUP packet on EP0.
func (d *Device) Setup(p []byte) {
	d.Stats.Setups++
	if len(p) < 8 {
		d.stall()
		return
	}
	typ, req := p[0], p[1]
	value := uint16(p[2]) | uint16(p[3])<<8
	length := uint16(p[6]) | uint16(p[7])<<8
	d.ctlData, d.ctlZLP, d.ctlOut = nil, false, 0

	switch typ {
	case 0x80: // device to host, standard, device
		switch req {
		case reqGetStatus:
			d.scratch[0], d.scratch[1] = 0, 0
			d.in(d.scratch[:2], length)
		case reqGetDescriptor:
			if desc := d.desc.lookup(byte(value>>8), byte(value)); desc != nil {
				d.in(desc, length)
			} else {
				d.stall() // device qualifier and others: full-speed only
			}
		case reqGetConfiguration:
			d.scratch[0] = d.config
			d.in(d.scratch[:1], length)
		default:
			d.stall()
		}
	case 0x00: // host to device, standard, device
		switch req {
		case reqSetAddress:
			d.pendingAddr, d.addrPending = byte(value&0x7F), true
			d.status()
		case reqSetConfiguration:
			switch value {
			case 0:
				d.config = 0
				d.status()
			case 1:
				d.config = 1
				d.port.OpenEndpoints()
				d.txBusy, d.txFull, d.rxArmed = false, false, false
				d.armData()
				d.status()
			default:
				d.stall()
			}
		default:
			d.stall()
		}
	case 0x81, 0x82: // GET_STATUS of an interface or endpoint: nothing halted
		if req != reqGetStatus {
			d.stall()
			return
		}
		d.scratch[0], d.scratch[1] = 0, 0
		d.in(d.scratch[:2], length)
	case 0x01: // host to device, standard, interface
		if req == reqSetInterface && value == 0 {
			d.status()
		} else {
			d.stall()
		}
	case 0x02: // host to device, standard, endpoint: CLEAR_FEATURE(ENDPOINT_HALT)
		if req == reqClearFeature && value == 0 {
			d.status() // never halted
		} else {
			d.stall()
		}
	case 0x21: // host to device, class, interface
		switch req {
		case cdcSetLineCoding:
			if length != 7 {
				d.stall()
				return
			}
			d.ctlOut = cdcSetLineCoding
			d.port.Receive(EPControl)
		case cdcSetControlLineState:
			d.dtr = value&1 != 0
			d.status()
		case cdcSendBreak:
			d.status()
		default:
			d.stall()
		}
	case 0xA1: // device to host, class, interface
		if req == cdcGetLineCoding {
			d.in(d.lineCoding[:], length)
		} else {
			d.stall()
		}
	default:
		d.stall()
	}
}

// Received handles an OUT packet on ep (the buffer is only valid during the call).
func (d *Device) Received(ep byte, data []byte) {
	switch ep {
	case EPControl:
		if d.ctlOut == cdcSetLineCoding && len(data) == 7 {
			copy(d.lineCoding[:], data)
			d.ctlOut = 0
			d.status()
			return
		}
		// The status stage of an IN transfer (or an unexpected packet): ready for the next.
		d.ctlOut = 0
		d.port.Receive(EPControl)
	case EPData:
		d.rxArmed = false
		if n := d.rx.write(data); n < len(data) {
			d.Stats.RxDropped += uint32(len(data) - n)
		}
		d.armData()
	}
}

// Sent handles the end of an IN packet on ep.
func (d *Device) Sent(ep byte) {
	switch ep {
	case EPControl:
		if d.addrPending {
			d.addrPending = false
			d.port.SetAddress(d.pendingAddr)
		}
		if d.ctlData != nil {
			d.sendControl()
		}
	case EPData:
		d.txBusy = false
		d.flush()
	}
}

// in starts an IN data stage with data, cut to the host's length; a ZLP
// ends it when it is shorter than asked and a multiple of the packet size.
func (d *Device) in(data []byte, length uint16) {
	if int(length) < len(data) {
		data = data[:length]
	}
	d.ctlZLP = len(data) < int(length) && len(data)%MaxPacket == 0
	d.ctlData = data
	d.sendControl()
	d.port.Receive(EPControl) // the status stage (or the host ending early)
}

// sendControl sends the next packet of the IN data stage. ctlData is nil
// when nothing is left; empty but not nil when only the ZLP is.
func (d *Device) sendControl() {
	data := d.ctlData
	n := min(len(data), MaxPacket)
	d.port.Send(EPControl, data[:n])
	d.ctlData = data[n:]
	if n < MaxPacket || (len(d.ctlData) == 0 && !d.ctlZLP) {
		d.ctlData = nil // a short packet (or the ZLP) ended it
	}
}

// status sends the zero-length IN status stage of a request without data.
func (d *Device) status() { d.port.Send(EPControl, nil) }

func (d *Device) stall() {
	d.Stats.Stalls++
	d.port.StallControl()
}

// armData lets the host send the next OUT packet when it fits.
func (d *Device) armData() {
	if d.config != 0 && !d.rxArmed && d.rx.free() >= MaxPacket {
		d.rxArmed = true
		d.port.Receive(EPData)
	}
}

// flush sends the next packet of queued output.
func (d *Device) flush() {
	if d.config == 0 || d.txBusy {
		return
	}
	n := d.tx.read(d.packet[:])
	if n == 0 {
		if d.txFull {
			// The last packet was full: a ZLP tells the host the transfer ended.
			d.txFull = false
			d.txBusy = true
			d.port.Send(EPData, nil)
		}
		return
	}
	d.txBusy = true
	d.txFull = n == MaxPacket
	d.port.Send(EPData, d.packet[:n])
}

// Read takes up to len(p) bytes the host sent; 0 when none.
func (d *Device) Read(p []byte) int {
	n := d.rx.read(p)
	d.armData()
	return n
}

// Buffered is the number of bytes waiting to be read.
func (d *Device) Buffered() int { return d.rx.len() }

// ReadByte takes one byte (ext.ByteStream); the error is never set, check
// Buffered first.
func (d *Device) ReadByte() (byte, error) {
	var b [1]byte
	d.Read(b[:])
	return b[0], nil
}

// Writable is the room left for output. A program that answers input
// (a console's echo) reads only while there is room for the answer: the
// input then waits in the receive buffer, and once that is full the host
// is held back (EP1 NAKs), so nothing is lost end to end.
func (d *Device) Writable() int { return d.tx.free() }

// Write queues p for the host and returns how many bytes fitted. Output
// before the host configured the device is dropped.
func (d *Device) Write(p []byte) int {
	if d.config == 0 {
		return 0
	}
	n := d.tx.write(p)
	d.flush()
	return n
}

// WriteString is Write for a string (no conversion to a byte slice: that
// would allocate).
func (d *Device) WriteString(s string) int {
	if d.config == 0 {
		return 0
	}
	n := d.tx.writeString(s)
	d.flush()
	return n
}
