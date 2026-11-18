"""Bluetooth Mesh network layer: PDU encryption and obfuscation (Mesh Profile §3.4.4)."""

from __future__ import annotations

from dataclasses import dataclass

from cryptography.exceptions import InvalidTag

from .crypto import aes_ecb, ccm_decrypt, ccm_encrypt, xor
from .keys import NetworkKeys

_NONCE_NETWORK = 0x00
_NONCE_PROXY = 0x03

_MIC_ACCESS = 4  # access messages (CTL = 0)
_MIC_CONTROL = 8  # control messages (CTL = 1)
_MIN_PDU_LENGTH = 14


@dataclass(frozen=True, slots=True)
class NetworkPdu:
    """Plaintext content of a network PDU."""

    ctl: bool
    ttl: int
    seq: int
    src: int
    dst: int
    transport_pdu: bytes


def _nonce(pdu_kind: int, ctl_ttl: int, seq: int, src: int, iv_index: int) -> bytes:
    return (
        bytes([pdu_kind, ctl_ttl])
        + seq.to_bytes(3, "big")
        + src.to_bytes(2, "big")
        + b"\x00\x00"
        + iv_index.to_bytes(4, "big")
    )


def encode_network_pdu(
    keys: NetworkKeys, iv_index: int, pdu: NetworkPdu, *, proxy_configuration: bool = False
) -> bytes:
    """Encrypt and obfuscate a network PDU.

    `proxy_configuration` selects the nonce of proxy configuration messages.
    """
    ctl_ttl = (int(pdu.ctl) << 7) | pdu.ttl
    nonce = _nonce(
        _NONCE_PROXY if proxy_configuration else _NONCE_NETWORK,
        0 if proxy_configuration else ctl_ttl,
        pdu.seq,
        pdu.src,
        iv_index,
    )
    mic_length = _MIC_CONTROL if pdu.ctl else _MIC_ACCESS
    encrypted = ccm_encrypt(
        keys.encryption_key, nonce, pdu.dst.to_bytes(2, "big") + pdu.transport_pdu, mic_length
    )
    obfuscation = aes_ecb(keys.privacy_key, bytes(5) + iv_index.to_bytes(4, "big") + encrypted[:7])
    header = bytes([ctl_ttl]) + pdu.seq.to_bytes(3, "big") + pdu.src.to_bytes(2, "big")
    first_byte = ((iv_index & 1) << 7) | keys.nid
    return bytes([first_byte]) + xor(header, obfuscation) + encrypted


def decode_network_pdu(
    keys: NetworkKeys, iv_index: int, data: bytes, *, proxy_configuration: bool = False
) -> NetworkPdu | None:
    """Decrypt a network PDU. Return None if it is not from this network or is invalid."""
    if len(data) < _MIN_PDU_LENGTH or data[0] & 0x7F != keys.nid or data[0] >> 7 != iv_index & 1:
        return None
    encrypted = data[7:]
    obfuscation = aes_ecb(keys.privacy_key, bytes(5) + iv_index.to_bytes(4, "big") + encrypted[:7])
    header = xor(data[1:7], obfuscation)
    ctl, ttl = bool(header[0] >> 7), header[0] & 0x7F
    seq = int.from_bytes(header[1:4], "big")
    src = int.from_bytes(header[4:6], "big")
    nonce = _nonce(
        _NONCE_PROXY if proxy_configuration else _NONCE_NETWORK,
        0 if proxy_configuration else header[0],
        seq,
        src,
        iv_index,
    )
    try:
        plain = ccm_decrypt(
            keys.encryption_key, nonce, encrypted, _MIC_CONTROL if ctl else _MIC_ACCESS
        )
    except InvalidTag:
        return None
    return NetworkPdu(ctl, ttl, seq, src, int.from_bytes(plain[:2], "big"), plain[2:])
