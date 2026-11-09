"""Message id = EPOCH | SEQUENCE: a restarted controller is a new session.

Before EPOCH, a controller restarted within the device's ``dedup_ttl`` could
pick a sequence number the device still remembered; the device took the new
request for a retry, replayed the old result and never ran it (about
``dedup_window / 256`` per restart). Same style as ``test_link_demo.py``:
fake clocks, synchronous delivery, no sleeping.
"""

from __future__ import annotations

import time

import pytest

from cairnmesh.controller import Controller
from cairnmesh.device import Device
from cairnmesh.events import ReplyIgnored
from cairnmesh.frames import ReplyFrame, RequestFrame, WakeFrame, decode_frame


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class End:
    """One end of a lossless link that delivers synchronously to ``peer``."""

    def __init__(self) -> None:
        self.receiver = None
        self.peer: End | None = None
        self.sent: list[bytes] = []

    def set_receiver(self, callback) -> None:
        self.receiver = callback

    def send(self, data: bytes) -> None:
        self.sent.append(data)
        if self.peer is not None and self.peer.receiver is not None:
            self.peer.receiver(data)


def awake_device() -> tuple[End, Device]:
    end = End()
    return end, Device(end, 1, awake=True, clock=FakeClock())


def controller_for(device_end: End, **options) -> Controller:
    """A (re)started controller on the same radio as ``device_end``."""

    end = End()
    end.peer, device_end.peer = device_end, end
    return Controller(end, clock=FakeClock(), **options)


# ------------------------------------------------------------- wire format


def test_epoch_is_on_the_wire_and_survives_a_round_trip():
    frames = [
        WakeFrame(sender_id=0x42, target_id=1, sequence=7, epoch=0xA5),
        RequestFrame(sender_id=0x42, target_id=1, sequence=7, epoch=0xA5, action=3, value=100),
        ReplyFrame(sender_id=1, target_id=0x42, sequence=7, epoch=0xA5, result=0),
    ]
    for frame, length in zip(frames, (5, 7, 6)):
        data = frame.encode()
        assert len(data) == length and data[3:5] == bytes([7, 0xA5])
        assert decode_frame(data) == frame
        assert frame.message_id == 0xA507


def test_frames_in_the_old_layout_are_rejected():
    with pytest.raises(ValueError):
        decode_frame(bytes([0x01, 0x42, 0x01, 0x05, 0x03, 0x64]))  # REQUEST without EPOCH


def test_epoch_out_of_range_is_refused():
    with pytest.raises(ValueError):
        RequestFrame(sender_id=0x42, target_id=1, sequence=0, epoch=256, action=1, value=0)
    with pytest.raises(ValueError):
        Controller(End(), epoch=-1)


# ---------------------------------------------------------- restart safety


def test_a_restarted_controller_is_not_mistaken_for_a_retry():
    device_end, device = awake_device()
    before = controller_for(device_end, epoch=10, initial_sequence=7)
    assert before.request(1, 3, 1).result(timeout=0).ok

    # Restart inside the device's dedup_ttl, worst case: the same sequence.
    after = controller_for(device_end, epoch=11, initial_sequence=7)
    assert after.request(1, 3, 2).result(timeout=0).ok
    assert device.handled_count == 2


def test_the_same_epoch_shows_what_the_message_id_prevents():
    device_end, device = awake_device()
    controller_for(device_end, epoch=10, initial_sequence=7).request(1, 3, 1)
    outcome = controller_for(device_end, epoch=10, initial_sequence=7).request(1, 3, 2)

    # Answered OK, but the second command never ran: a silent loss.
    assert outcome.result(timeout=0).ok
    assert device.handled_count == 1


def test_retries_within_one_run_are_still_deduplicated():
    device_end, device = awake_device()
    controller = Controller(End(), epoch=10, initial_sequence=7, clock=FakeClock())
    controller.request(1, 3, 1)  # no replies come back
    controller.clock.now += controller.timeout
    controller.tick()

    first, retry = controller.transport.sent
    assert first == retry  # same EPOCH and SEQUENCE
    device.on_frame(first)
    device.on_frame(retry)
    assert device.handled_count == 1
    assert decode_frame(device_end.sent[-1]).epoch == 10  # replayed reply


def test_a_wake_reply_echoes_the_epoch():
    device_end, device = awake_device()
    controller = controller_for(device_end, epoch=0xEE)
    assert controller.wake(1).result(timeout=0).ok
    assert decode_frame(device_end.sent[-1]).epoch == 0xEE


def test_a_reply_from_before_the_restart_does_not_resolve_a_new_request():
    controller = Controller(End(), epoch=11, initial_sequence=7, clock=FakeClock())
    ignored = []
    controller.events.subscribe(
        lambda e: ignored.append(e) if isinstance(e, ReplyIgnored) else None
    )
    future = controller.request(1, 3, 1)

    stale = ReplyFrame(sender_id=1, target_id=0x42, sequence=7, epoch=10, result=0)
    controller.on_frame(stale.encode())
    assert not future.done()
    assert ignored and "会话代" in ignored[0].reason

    fresh = ReplyFrame(sender_id=1, target_id=0x42, sequence=7, epoch=11, result=0)
    controller.on_frame(fresh.encode())
    assert future.result(timeout=0).ok


# ------------------------------------------------------------ default epoch


def test_default_epochs_differ_for_any_two_starts_within_256_seconds(monkeypatch):
    epochs = set()
    for second in range(1_790_000_000, 1_790_000_000 + 256):
        monkeypatch.setattr(time, "time", lambda s=second: s + 0.5)
        epochs.add(Controller(End()).epoch)
    # 256 consecutive seconds give 256 distinct epochs, so two runs started
    # at least 1 s apart within the device's dedup_ttl never share one.
    assert len(epochs) == 256
