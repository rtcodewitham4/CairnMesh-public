package usb

// Vendor and product ID: pid.codes' test PID (0x1209:0x0001), allowed for
// private testing only; a product shipped to others needs its own.
const (
	VendorID  = 0x1209
	ProductID = 0x0001
)

var deviceDescriptor = [18]byte{
	18, 1, // bLength, DEVICE
	0x00, 0x02, // bcdUSB 2.00
	0x02, 0x00, 0x00, // CDC device class; subclass and protocol in the interfaces
	MaxPacket, // bMaxPacketSize0
	VendorID & 0xFF, VendorID >> 8,
	ProductID & 0xFF, ProductID >> 8,
	0x00, 0x01, // bcdDevice 1.00
	1, 2, 3, // iManufacturer, iProduct, iSerialNumber
	1, // bNumConfigurations
}

// configDescriptor is one configuration: the CDC-ACM communication
// interface (protocol 0, so that modem managers leave the port alone) with
// its notification endpoint, and the data interface with the bulk pair.
var configDescriptor = [67]byte{
	9, 2, 67, 0, // CONFIGURATION, wTotalLength
	2, 1, 0, // bNumInterfaces, bConfigurationValue, iConfiguration
	0x80, 50, // bus powered, 100 mA

	9, 4, 0, 0, 1, 0x02, 0x02, 0x00, 0, // INTERFACE 0: CDC, ACM, no protocol, 1 endpoint
	5, 0x24, 0x00, 0x10, 0x01, // CDC header, bcdCDC 1.10
	5, 0x24, 0x01, 0x00, 0x01, // call management: none, data interface 1
	4, 0x24, 0x02, 0x02, // ACM: line coding and serial state
	5, 0x24, 0x06, 0x00, 0x01, // union: master 0, slave 1
	7, 5, 0x80 | EPNotify, 0x03, NotifyMaxPacket, 0, 16, // EP2 IN interrupt, 16 ms

	9, 4, 1, 0, 2, 0x0A, 0x00, 0x00, 0, // INTERFACE 1: CDC data, 2 endpoints
	7, 5, EPData, 0x02, MaxPacket, 0, 0, // EP1 OUT bulk
	7, 5, 0x80 | EPData, 0x02, MaxPacket, 0, 0, // EP1 IN bulk
}

var (
	langString    = [4]byte{4, 3, 0x09, 0x04} // English (US)
	vendorString  = "CairnMesh"
	productString = "CairnMesh node console"
)

// maxSerial is the longest serial number string (the 96-bit UID in hex).
const maxSerial = 24

// descriptors holds the string descriptors, built once (UTF-16LE).
type descriptors struct {
	vendor  [2 + 2*len("CairnMesh")]byte
	product [2 + 2*len("CairnMesh node console")]byte
	serial  [2 + 2*maxSerial]byte
	nSerial int
}

func utf16(dst []byte, s string) int {
	dst[0], dst[1] = byte(2+2*len(s)), 3
	for i := 0; i < len(s); i++ {
		dst[2+2*i], dst[3+2*i] = s[i], 0
	}
	return 2 + 2*len(s)
}

func (d *descriptors) init(serial string) {
	if len(serial) > maxSerial {
		serial = serial[:maxSerial]
	}
	utf16(d.vendor[:], vendorString)
	utf16(d.product[:], productString)
	d.nSerial = utf16(d.serial[:], serial)
}

// lookup returns the descriptor of type typ, index index, or nil.
func (d *descriptors) lookup(typ, index byte) []byte {
	switch typ {
	case 1:
		return deviceDescriptor[:]
	case 2:
		return configDescriptor[:]
	case 3:
		switch index {
		case 0:
			return langString[:]
		case 1:
			return d.vendor[:]
		case 2:
			return d.product[:]
		case 3:
			return d.serial[:d.nSerial]
		}
	}
	return nil
}
