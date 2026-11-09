"""Read the ST-LINK virtual COM port and print the bytes.

Uses only the Python standard library, so no pyserial is needed.
Usage: monitor.py <port> <baud> [seconds]
"""

import os
import sys
import termios
import time

BAUD_RATES = {
    9600: termios.B9600,
    19200: termios.B19200,
    38400: termios.B38400,
    57600: termios.B57600,
    115200: termios.B115200,
    230400: termios.B230400,
}


def open_port(port, baud):
    fd = os.open(port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    _, _, _, _, _, _, cc = termios.tcgetattr(fd)
    cflag = termios.CS8 | termios.CREAD | termios.CLOCAL
    cc = list(cc)
    cc[termios.VMIN] = 0
    cc[termios.VTIME] = 0
    speed = BAUD_RATES[baud]
    termios.tcsetattr(fd, termios.TCSANOW, [0, 0, cflag, 0, speed, speed, cc])
    termios.tcflush(fd, termios.TCIFLUSH)
    return fd


def main():
    port = sys.argv[1] if len(sys.argv) > 1 else "/dev/ttyACM0"
    baud = int(sys.argv[2]) if len(sys.argv) > 2 else 115200
    seconds = float(sys.argv[3]) if len(sys.argv) > 3 else 0

    fd = open_port(port, baud)
    print("monitor: %s at %d baud. Press Ctrl-C to stop." % (port, baud))
    start = time.time()
    try:
        while seconds == 0 or time.time() - start < seconds:
            try:
                chunk = os.read(fd, 4096)
            except BlockingIOError:
                chunk = b""
            if chunk:
                sys.stdout.write(chunk.decode("utf-8", "replace"))
                sys.stdout.flush()
            else:
                time.sleep(0.02)
    except KeyboardInterrupt:
        pass
    finally:
        os.close(fd)


if __name__ == "__main__":
    main()
