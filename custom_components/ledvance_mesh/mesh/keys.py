"""Mesh network keys and the values derived from them."""

from __future__ import annotations

import os

from .crypto import k1, k2, k3, k4, s1

KEY_LENGTH = 16


class NetworkKeys:
    """A NetKey and an AppKey, with their derived identifiers."""

    def __init__(self, net_key: bytes, app_key: bytes) -> None:
        if len(net_key) != KEY_LENGTH or len(app_key) != KEY_LENGTH:
            raise ValueError("a mesh key is 16 bytes long")
        self.net_key = net_key
        self.app_key = app_key
        self.nid, self.encryption_key, self.privacy_key = k2(net_key, b"\x00")
        self.network_id = k3(net_key)
        self.beacon_key = k1(net_key, s1(b"nkbk"), b"id128\x01")
        self.aid = k4(app_key)

    @classmethod
    def generate(cls) -> NetworkKeys:
        """Create new random keys."""
        return cls(os.urandom(KEY_LENGTH), os.urandom(KEY_LENGTH))

    @classmethod
    def from_hex(cls, net_key: str, app_key: str) -> NetworkKeys:
        """Rebuild the keys from their hexadecimal form."""
        return cls(bytes.fromhex(net_key), bytes.fromhex(app_key))
