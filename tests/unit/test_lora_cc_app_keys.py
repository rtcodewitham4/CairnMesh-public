"""Task Q14: the end-to-end application layer under per-node keys."""

from __future__ import annotations

import struct

import pytest

pytest.importorskip("cryptography")

from cryptography.hazmat.primitives.ciphers.aead import AESCCM

from cairnmesh.frames import APP, KeepaliveFrame, ReplyFrame, ReportFrame, RequestFrame, WakeFrame, add_route
from cairnmesh.secure import AppKeys, SecureTransport, open_sealed, seal

from _session_helpers import LINE, build, open_session, run

NET = b"0123456789abcdef"
APP_KEY = bytes(range(0x10, 0x20))
G, NODE = 0x42, 0x01


class Wire:
    def __init__(self) -> None:
        self.sent: list[bytes] = []
        self.receiver = None

    def set_receiver(self, callback) -> None:
        self.receiver = callback

    def send(self, data: bytes) -> None:
        self.sent.append(bytes(data))


def pair(controller_keys=None, device_keys=None):
    """A controller and a device transport; frames move by hand.

    ``to_dev.sent`` holds what the controller put out, ``to_ctl.sent`` what
    the device did; ``to_ctl.receiver`` is the device's input and
    ``to_dev.receiver`` the controller's.
    """

    to_dev, to_ctl = Wire(), Wire()
    controller = SecureTransport(to_dev, NET, initial_fcnt=76, app=controller_keys or AppKeys({NODE: APP_KEY}))
    device = SecureTransport(to_ctl, NET, initial_fcnt=76, app=device_keys or AppKeys({NODE: APP_KEY}))
    heard = {"controller": [], "device": []}
    controller.set_receiver(heard["controller"].append)
    device.set_receiver(heard["device"].append)
    return controller, device, to_dev, to_ctl, heard


def request(sequence: int = 5) -> bytes:
    return RequestFrame(G, NODE, sequence, action=3, value=100, epoch=0x64).encode()


def test_vectors_for_both_implementations():
    controller, device, to_dev, to_ctl, heard = pair()
    controller.send(request())
    assert to_dev.sent[-1].hex(" ") == "a1 42 01 05 4d 00 00 00 42 56 0c 70 ab a6 4c b8 fe 84 33 49 5d"
    below, fcnt = open_sealed(AESCCM(NET, tag_length=4), to_dev.sent[-1])
    assert below.hex(" ") == "21 42 01 05 64 00 81 3b 63 79 97 7b" and fcnt == 77  # what a relay sees
    to_ctl.receiver(to_dev.sent[-1])
    assert heard["device"] == [request()]
    device.send(ReplyFrame(NODE, G, 5, result=0, epoch=0x64).encode())
    assert to_ctl.sent[-1].hex(" ") == "a2 01 42 05 4d 00 00 00 01 31 cf e4 a6 f2 3a b0 68 f2 04 56"
    to_dev.receiver(to_ctl.sent[-1])
    assert heard["controller"] == [ReplyFrame(NODE, G, 5, result=0, epoch=0x64).encode()]


def test_a_relay_with_the_network_key_cannot_read_or_change_the_payload():
    controller, device, to_dev, to_ctl, heard = pair()
    controller.send(request())
    aead = AESCCM(NET, tag_length=4)
    below, fcnt = open_sealed(aead, to_dev.sent[-1])
    assert below[0] & APP and bytes([3, 100]) not in below[5:]
    tampered = below[:6] + bytes([below[6] ^ 1]) + below[7:]
    to_ctl.receiver(seal(aead, tampered, fcnt))
    assert heard["device"] == [] and device.stats["app_failed"] == 1


def test_a_recorded_command_resealed_under_a_fresh_counter_is_refused():
    controller, device, to_dev, to_ctl, heard = pair()
    controller.send(request())
    to_ctl.receiver(to_dev.sent[-1])
    aead = AESCCM(NET, tag_length=4)
    below, fcnt = open_sealed(aead, to_dev.sent[-1])
    to_ctl.receiver(seal(aead, below, fcnt + 1000))  # a compromised relay replays it later
    assert len(heard["device"]) == 1 and device.stats["app_failed"] == 1
    to_ctl.receiver(to_dev.sent[-1])  # the very same frame again: the replay window
    assert len(heard["device"]) == 1 and device.stats["replayed"] == 1


def test_a_command_without_the_layer_is_dropped():
    _, device, to_dev, to_ctl, heard = pair()
    forged = SecureTransport(Wire(), NET, initial_fcnt=500)  # network key only
    to_ctl.receiver(forged.seal_frame(request()))
    report = ReportFrame(G, NODE, 1, subject=2, event=0, epoch=1).encode()
    to_ctl.receiver(forged.seal_frame(report))
    assert heard["device"] == [] and device.stats["app_missing"] == 2


def test_only_the_answer_to_a_request_carries_the_layer():
    controller, device, to_dev, to_ctl, heard = pair()
    aead = AESCCM(NET, tag_length=4)
    controller.send(WakeFrame(G, NODE, 9, epoch=0x64).encode())
    to_ctl.receiver(to_dev.sent[-1])
    device.send(ReplyFrame(NODE, G, 9, result=0, epoch=0x64).encode())  # answers the WAKE
    assert not open_sealed(aead, to_ctl.sent[-1])[0][0] & APP
    controller.send(request(10))
    to_ctl.receiver(to_dev.sent[-1])
    device.send(ReplyFrame(NODE, G, 10, result=0, epoch=0x64).encode())
    assert open_sealed(aead, to_ctl.sent[-1])[0][0] & APP
    # A network-only REPLY to that REQUEST is a forgery.
    forged = SecureTransport(Wire(), NET, initial_fcnt=900)
    to_dev.receiver(forged.seal_frame(ReplyFrame(NODE, G, 10, result=1, epoch=0x64).encode()))
    to_dev.receiver(to_ctl.sent[-1])
    assert heard["controller"] == [ReplyFrame(NODE, G, 10, result=0, epoch=0x64).encode()]
    assert controller.stats["app_missing"] == 1


def test_routed_frames_keep_their_trailer_outside_the_layer():
    controller, device, to_dev, to_ctl, heard = pair()
    routed = add_route(request(), (7, 8))
    controller.send(routed)
    below, _ = open_sealed(AESCCM(NET, tag_length=4), to_dev.sent[-1])
    assert below[-3:] == bytes([7, 8, 2])  # relays read the route as before
    to_ctl.receiver(to_dev.sent[-1])
    assert heard["device"] == [routed]


def test_keys_are_replaced_node_by_node_by_version():
    new = bytes(range(0x30, 0x40))
    keys = AppKeys({NODE: APP_KEY})
    keys.add(NODE, new, version=1)
    device_keys = AppKeys({NODE: APP_KEY})
    controller, device, to_dev, to_ctl, heard = pair(keys, device_keys)
    controller.send(request(1))
    to_ctl.receiver(to_dev.sent[-1])
    assert heard["device"] == [] and device.stats["app_no_key"] == 1  # the device has not got version 1 yet
    device_keys.add(NODE, new, version=1)
    controller.send(request(2))
    to_ctl.receiver(to_dev.sent[-1])
    assert heard["device"] == [request(2)]
    keys.use(NODE, 0)  # the old key still works while both hold it
    controller.send(request(3))
    to_ctl.receiver(to_dev.sent[-1])
    assert heard["device"][-1] == request(3)


def test_control_frames_stay_network_only():
    controller, _, to_dev, _, _ = pair()
    keepalive = add_route(KeepaliveFrame(G, NODE, 4, lease=30, epoch=1).encode(), (7,))
    controller.send(keepalive)
    below, _ = open_sealed(AESCCM(NET, tag_length=4), to_dev.sent[-1])
    assert below == keepalive and controller.stats["app_sealed"] == 0
    assert controller.sealed_length(request()) == 7 + 9 + 4 + 5


def test_sessions_and_floods_carry_the_layer_end_to_end():
    clock, network, controller = build(LINE, seed=1, gateway_options={"idle_timeout": None})
    assert network.app_keys is not None
    open_session(clock, network, controller, 5)
    routed = network.sessions.request(5, 3, 1)
    run(clock, network, controller, 30.0, routed.done)
    network.sessions.close_session(5)
    flooded = controller.request(4, 3, 2)  # no session: a flood
    run(clock, network, controller, 30.0, flooded.done)
    assert routed.result(timeout=0).ok and flooded.result(timeout=0).ok
    gateway = network.gateway.stats
    assert gateway["app_sealed"] >= 2 and gateway["app_opened"] >= 2 and gateway["app_failed"] == 0
    assert all(network.relays[node].stats["auth_failed"] == 0 for node in LINE)


def test_the_counter_binds_the_inner_layer():
    # The inner nonce carries the originator's FCNT, which relays keep.
    controller, _, to_dev, _, _ = pair()
    controller.send(request())
    controller.send(request())  # the same command again: a new counter, new bytes
    first, second = (open_sealed(AESCCM(NET, tag_length=4), raw) for raw in to_dev.sent)
    assert first[1] + 1 == second[1] and first[0][6:] != second[0][6:]
    assert struct.unpack("<I", to_dev.sent[0][4:8])[0] == first[1]


def test_every_application_layer_drop_reaches_on_reject():
    seen = []
    to_ctl = Wire()
    device = SecureTransport(to_ctl, NET, initial_fcnt=76, app=AppKeys({NODE: APP_KEY}), on_reject=lambda why, raw: seen.append(why))
    forged = SecureTransport(Wire(), NET, initial_fcnt=500)
    to_ctl.receiver(forged.seal_frame(request()))
    assert seen == ["application layer: app_missing"] and device.stats["app_missing"] == 1
