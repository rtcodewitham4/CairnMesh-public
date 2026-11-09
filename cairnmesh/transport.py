"""The only interface the protocol needs from the physical link.

Controller and Device talk to a ``Transport`` and nothing else, so the
simulated medium in ``channel.py`` can later be replaced by a serial port
or a LoRa radio driver without touching protocol code. Ready-made ones:

    SerialTransport (serial_link.py)  SLIP framing + CRC16 over a serial port
    SecureTransport (secure.py)       AES-CCM envelope around any transport

The transport is responsible for framing (one ``bytes`` object per frame);
it never interprets frame contents.
"""

from __future__ import annotations

from typing import Callable, Protocol

Receiver = Callable[[bytes], None]


class Transport(Protocol):
    def send(self, data: bytes) -> None:
        """Transmit one encoded frame. Delivery is not guaranteed."""

    def set_receiver(self, callback: Receiver) -> None:
        """Register the callback invoked once per received frame."""
