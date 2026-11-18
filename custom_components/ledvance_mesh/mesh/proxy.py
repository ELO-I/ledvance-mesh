"""Bluetooth Mesh Proxy protocol (Mesh Profile §6): GATT encapsulation and SAR segmentation."""

from __future__ import annotations

from enum import IntEnum

from .keys import NetworkKeys
from .network import NetworkPdu, encode_network_pdu


class ProxyPduType(IntEnum):
    """Proxy PDU type (6 least significant bits of the header)."""

    NETWORK = 0
    BEACON = 1
    CONFIGURATION = 2
    PROVISIONING = 3


class _Sar(IntEnum):
    COMPLETE = 0
    FIRST = 1
    CONTINUATION = 2
    LAST = 3


def split_proxy_pdu(pdu_type: ProxyPduType, payload: bytes, max_frame_size: int) -> list[bytes]:
    """Split `payload` into GATT frames of at most `max_frame_size` bytes (header included)."""
    room = max_frame_size - 1
    if len(payload) <= room:
        return [bytes([(_Sar.COMPLETE << 6) | pdu_type]) + payload]
    chunks = [payload[i : i + room] for i in range(0, len(payload), room)]
    frames = []
    for index, chunk in enumerate(chunks):
        if index == 0:
            sar = _Sar.FIRST
        elif index == len(chunks) - 1:
            sar = _Sar.LAST
        else:
            sar = _Sar.CONTINUATION
        frames.append(bytes([(sar << 6) | pdu_type]) + chunk)
    return frames


class ProxyPduAssembler:
    """Reassemble received GATT frames into complete proxy PDUs."""

    def __init__(self) -> None:
        self._type: ProxyPduType | None = None
        self._buffer = bytearray()

    def feed(self, frame: bytes) -> tuple[ProxyPduType, bytes] | None:
        """Add a frame; return (type, content) once a PDU is complete."""
        sar, pdu_type = _Sar(frame[0] >> 6), ProxyPduType(frame[0] & 0x3F)
        if sar is _Sar.COMPLETE:
            return pdu_type, bytes(frame[1:])
        if sar is _Sar.FIRST:
            self._type, self._buffer = pdu_type, bytearray(frame[1:])
            return None
        if self._type is None:  # continuation without a start: a frame was lost
            return None
        self._buffer.extend(frame[1:])
        if sar is _Sar.LAST:
            complete = (self._type, bytes(self._buffer))
            self._type, self._buffer = None, bytearray()
            return complete
        return None


def encode_set_blacklist_filter(keys: NetworkKeys, iv_index: int, *, src: int, seq: int) -> bytes:
    """Build a Set Filter Type message (empty blacklist): the proxy then forwards everything.

    Without it, the default filter (empty whitelist) blocks the nodes' answers.
    """
    set_filter_type, blacklist = 0x00, 0x01
    pdu = NetworkPdu(True, 0, seq, src, 0x0000, bytes([set_filter_type, blacklist]))
    return encode_network_pdu(keys, iv_index, pdu, proxy_configuration=True)
