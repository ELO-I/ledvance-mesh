"""Secure Network beacon (Mesh Profile §3.9.3): announces the network IV index.

A proxy sends one on connection, then on every change; we may also send one to announce an
IV index update.
"""

from __future__ import annotations

from dataclasses import dataclass

from .crypto import aes_cmac
from .iv import IvState
from .keys import NetworkKeys

_TYPE_SECURE_NETWORK = 0x01
_FLAG_KEY_REFRESH = 0x01
_FLAG_IV_UPDATE = 0x02
_LENGTH = 22
_AUTH_LENGTH = 8


@dataclass(frozen=True, slots=True)
class SecureNetworkBeacon:
    """Content of an authenticated beacon of our network."""

    iv_index: int
    iv_update: bool
    key_refresh: bool


def _authentication(keys: NetworkKeys, body: bytes) -> bytes:
    return aes_cmac(keys.beacon_key, body)[:_AUTH_LENGTH]


def encode_secure_network_beacon(keys: NetworkKeys, iv: IvState) -> bytes:
    """Build the beacon announcing state `iv`."""
    flags = _FLAG_IV_UPDATE if iv.update_in_progress else 0
    body = bytes([flags]) + keys.network_id + iv.index.to_bytes(4, "big")
    return bytes([_TYPE_SECURE_NETWORK]) + body + _authentication(keys, body)


def decode_secure_network_beacon(keys: NetworkKeys, data: bytes) -> SecureNetworkBeacon | None:
    """Decode a beacon; None if it has another type, belongs to another network or is forged."""
    if len(data) != _LENGTH or data[0] != _TYPE_SECURE_NETWORK:
        return None
    body, authentication = data[1:14], data[14:]
    if body[1:9] != keys.network_id or _authentication(keys, body) != authentication:
        return None
    return SecureNetworkBeacon(
        iv_index=int.from_bytes(body[9:13], "big"),
        iv_update=bool(body[0] & _FLAG_IV_UPDATE),
        key_refresh=bool(body[0] & _FLAG_KEY_REFRESH),
    )
