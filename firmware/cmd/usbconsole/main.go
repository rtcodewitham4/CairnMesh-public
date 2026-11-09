// Command usbconsole brings the USB debug console up on its own: the board
// enumerates as a CDC-ACM serial port (pid.codes test VID:PID 1209:0001,
// /dev/ttyACM* on Linux) and answers a few commands, one per line:
//
//	help    the commands
//	uid     the chip's unique ID (also the USB serial number)
//	uptime  seconds since boot
//	stats   USB events and errors
//
// The ST-LINK serial port (USART2) logs the USB events. On the NUCLEO the
// USB lines are wired by hand: D+ to CN10-12 (PA12), D− to CN10-14 (PA11),
// GND; VBUS stays unconnected.
package main

import (
	"machine"
	"runtime"
	"time"

	"cairnmesh/firmware/hw/cyc"
	"cairnmesh/firmware/hw/usbhw"
	"cairnmesh/firmware/usb"
)

var (
	ctl    usbhw.Controller
	dev    usb.Device
	serial [24]byte
	line   [64]byte
	n      int
	num    [12]byte
)

func main() {
	machine.UART2.Configure(machine.UARTConfig{TX: machine.PA2, RX: machine.PA3, BaudRate: 115200})
	if cyc.To48MHz() {
		// The runtime set TIM16 and the UART up for 4 MHz (see README).
		machine.TIM16.Device.PSC.Set(48/4 - 1)
		cyc.SetUART2Baud(115200)
	}
	println("usbconsole start")
	uid := usbhw.Serial(&serial)
	dev.Init(&ctl, uid)
	if !ctl.Init() {
		println("usb: HSI48 did not start")
		for {
			time.Sleep(time.Hour)
		}
	}
	println("usb attached, uid", uid)

	boot := time.Now()
	wasConfigured, wasConnected := false, false
	for {
		ctl.Poll(&dev)
		if c := dev.Configured(); c != wasConfigured {
			wasConfigured = c
			println("usb configured", c, "resets", ctl.Resets, "setups", ctl.Setups)
		}
		if c := dev.Connected(); c != wasConnected {
			wasConnected = c
			println("usb port open", c)
			if c {
				dev.WriteString("CairnMesh node console. Type help.\r\n> ")
			}
		}
		// Read only while the answer fits (a command's reply is under 80 bytes):
		// otherwise input waits and the host is held back, nothing is lost.
		for dev.Buffered() > 0 && dev.Writable() >= 80 {
			b, _ := dev.ReadByte()
			switch {
			case b == '\r' || b == '\n':
				if b == '\r' {
					dev.WriteString("\r\n")
					command(line[:n], boot)
					dev.WriteString("> ")
				}
				n = 0
			case b == 0x7F || b == 0x08: // backspace
				if n > 0 {
					n--
					dev.WriteString("\b \b")
				}
			case n < len(line) && b >= ' ':
				line[n] = b
				n++
				dev.Write(line[n-1 : n]) // echo
			}
		}
		runtime.Gosched()
	}
}

func command(cmd []byte, boot time.Time) {
	switch string(cmd) {
	case "":
	case "help":
		dev.WriteString("help uid uptime stats\r\n")
	case "uid":
		dev.Write(serial[:])
		dev.WriteString("\r\n")
	case "uptime":
		dev.Write(itoa(uint32(time.Since(boot) / time.Second)))
		dev.WriteString(" s\r\n")
	case "stats":
		dev.WriteString("resets ")
		dev.Write(itoa(ctl.Resets))
		dev.WriteString(" setups ")
		dev.Write(itoa(ctl.Setups))
		dev.WriteString(" outs ")
		dev.Write(itoa(ctl.Outs))
		dev.WriteString(" ins ")
		dev.Write(itoa(ctl.Ins))
		dev.WriteString(" stalls ")
		dev.Write(itoa(dev.Stats.Stalls))
		dev.WriteString(" rxdropped ")
		dev.Write(itoa(dev.Stats.RxDropped))
		dev.WriteString("\r\n")
	default:
		dev.WriteString("unknown command; type help\r\n")
	}
}

// itoa formats v into num (no allocation).
func itoa(v uint32) []byte {
	i := len(num)
	for {
		i--
		num[i] = byte('0' + v%10)
		v /= 10
		if v == 0 {
			return num[i:]
		}
	}
}
