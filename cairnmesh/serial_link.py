"""Serial framing and a ``Transport`` for a real module.

A byte stream has no frame boundaries, so every frame is sent as

    END | SLIP-escaped(frame || CRC16) | END

SLIP is RFC 1055 (END 0xC0, ESC 0xDB, ESC_END 0xDC, ESC_ESC 0xDD). The CRC is
CRC16-CCITT-FALSE (poly 0x1021, init 0xFFFF), big-endian on the wire. This is
the same format as ``firmware/link`` on the STM32U083 (checked on the board
with ``firmware/tools/serial_bench.py``), so the two interoperate.

``SerialTransport`` only needs an object with ``read(n)`` and ``write(data)``
(``serial.Serial`` or any stand-in); it never interprets frame contents.
"""

from __future__ import annotations

import binascii
import threading
from typing import Callable, Optional

from .transport import Receiver

END = 0xC0
ESC = 0xDB
ESC_END = 0xDC
ESC_ESC = 0xDD

#: Most a gateway bridge message puts before a LoRa packet (spec 6, ``bridge.py``).
MAX_HEADER = 8
#: Largest unframed payload: a LoRa packet (at most 255 B), a bridge header, the CRC.
MAX_FRAME = 255 + MAX_HEADER + 2


def crc16(data: bytes) -> int:
    """CRC16-CCITT-FALSE; ``crc16(b"123456789") == 0x29B1``."""

    return binascii.crc_hqx(data, 0xFFFF)


def slip_encode(frame: bytes) -> bytes:
    """Append the CRC to ``frame`` and wrap it as one SLIP packet."""

    body = bytes(frame) + crc16(frame).to_bytes(2, "big")
    out = bytearray([END])
    for byte in body:
        if byte == END:
            out += bytes([ESC, ESC_END])
        elif byte == ESC:
            out += bytes([ESC, ESC_ESC])
        else:
            out.append(byte)
    out.append(END)
    return bytes(out)


class SlipDecoder:
    """Collect stream bytes into frames; bad packets are counted, not raised."""

    def __init__(self) -> None:
        self._buf = bytearray()
        self._esc = False
        self._overflow = False
        self.frames = 0
        self.crc_errors = 0
        self.overruns = 0

    def feed(self, data: bytes) -> list[bytes]:
        """Consume ``data`` and return every complete, CRC-valid frame in it."""

        frames: list[bytes] = []
        for byte in data:
            if byte == END:
                frame = self._finish()
                if frame is not None:
                    frames.append(frame)
                continue
            if self._esc:
                self._esc = False
                if byte == ESC_END:
                    byte = END
                elif byte == ESC_ESC:
                    byte = ESC
            elif byte == ESC:
                self._esc = True
                continue
            if len(self._buf) >= MAX_FRAME:
                self._overflow = True
            else:
                self._buf.append(byte)
        return frames

    def _finish(self) -> Optional[bytes]:
        buf, overflow = bytes(self._buf), self._overflow
        self._buf.clear()
        self._esc = self._overflow = False
        if overflow:
            self.overruns += 1
            return None
        if not buf:
            return None  # empty packet between two END bytes
        if len(buf) < 3 or crc16(buf[:-2]) != int.from_bytes(buf[-2:], "big"):
            self.crc_errors += 1
            return None
        self.frames += 1
        return buf[:-2]


class SerialTransport:
    """``Transport`` over a serial port, with a background reader thread.

    ``on_error`` is called with any exception raised by the port or by the
    receiver; a port failure also stops the reader.
    """

    def __init__(
        self,
        port,
        *,
        on_error: Optional[Callable[[Exception], None]] = None,
    ) -> None:
        self._port = port
        self._on_error = on_error
        self._receiver: Optional[Receiver] = None
        self._decoder = SlipDecoder()
        self._write_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    @classmethod
    def open(cls, path: str, baud: int = 115200, **options) -> "SerialTransport":
        """Open ``path`` with pyserial (an optional dependency)."""

        try:
            import serial
        except ImportError as exc:  # pragma: no cover - depends on the environment
            raise ImportError("SerialTransport.open() needs pyserial: pip install pyserial") from exc
        return cls(serial.Serial(path, baud, timeout=0.05), **options)

    @property
    def decoder(self) -> SlipDecoder:
        """Framing counters (``frames``, ``crc_errors``, ``overruns``)."""

        return self._decoder

    def set_error_handler(self, callback: Optional[Callable[[Exception], None]]) -> None:
        self._on_error = callback

    def set_receiver(self, callback: Receiver) -> None:
        self._receiver = callback

    def send(self, data: bytes) -> None:
        packet = slip_encode(data)
        with self._write_lock:
            self._port.write(packet)
            flush = getattr(self._port, "flush", None)
            if flush is not None:
                flush()

    # -------------------------------------------------------------- lifecycle

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._read_loop, name="serial-rx", daemon=True
        )
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None
        close = getattr(self._port, "close", None)
        if close is not None:
            close()

    def __enter__(self) -> "SerialTransport":
        self.start()
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def _read_loop(self) -> None:
        while not self._stop.is_set():
            try:
                waiting = getattr(self._port, "in_waiting", 0)
                chunk = self._port.read(waiting or 1)
            except Exception as exc:  # noqa: BLE001 - the port is gone; stop reading
                self._report(exc)
                return
            if not chunk:
                continue
            for frame in self._decoder.feed(chunk):
                receiver = self._receiver
                if receiver is None:
                    continue
                try:
                    receiver(frame)
                except Exception as exc:  # noqa: BLE001 - keep reading
                    self._report(exc)

    def _report(self, exc: Exception) -> None:
        if self._on_error is not None:
            self._on_error(exc)
