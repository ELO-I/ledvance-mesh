"""Network layer: round trip and rejections."""

import pytest

from custom_components.ledvance_mesh.mesh.keys import NetworkKeys
from custom_components.ledvance_mesh.mesh.network import (
    NetworkPdu,
    decode_network_pdu,
    encode_network_pdu,
)

KEYS = NetworkKeys(bytes(range(16)), bytes(range(16, 32)))
OTHER = NetworkKeys(bytes(range(32, 48)), bytes(range(48, 64)))
PDU = NetworkPdu(False, 7, 0x00ABCD, 0x0001, 0x0010, b"\x40" + b"payload!")


def test_roundtrip() -> None:
    assert decode_network_pdu(KEYS, 0, encode_network_pdu(KEYS, 0, PDU)) == PDU


def test_proxy_configuration_roundtrip() -> None:
    control = NetworkPdu(True, 0, 5, 0x0001, 0x0000, b"\x00\x01")
    data = encode_network_pdu(KEYS, 0, control, proxy_configuration=True)
    assert decode_network_pdu(KEYS, 0, data, proxy_configuration=True) == control
    assert decode_network_pdu(KEYS, 0, data) is None  # wrong nonce


@pytest.mark.parametrize("keys", [OTHER])
def test_foreign_network_is_rejected(keys: NetworkKeys) -> None:
    assert decode_network_pdu(keys, 0, encode_network_pdu(KEYS, 0, PDU)) is None


def test_corrupted_pdu_is_rejected() -> None:
    data = bytearray(encode_network_pdu(KEYS, 0, PDU))
    data[-1] ^= 1
    assert decode_network_pdu(KEYS, 0, bytes(data)) is None


def test_truncated_pdu_is_rejected() -> None:
    assert decode_network_pdu(KEYS, 0, b"\x00" * 5) is None


def test_wrong_iv_index_is_rejected() -> None:
    assert decode_network_pdu(KEYS, 1, encode_network_pdu(KEYS, 0, PDU)) is None
