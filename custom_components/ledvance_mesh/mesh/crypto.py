"""Bluetooth Mesh cryptographic primitives (Mesh Profile 1.0.1, §3.8.2)."""

from __future__ import annotations

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.ciphers.aead import AESCCM
from cryptography.hazmat.primitives.cmac import CMAC

ZERO_KEY = bytes(16)


def xor(a: bytes, b: bytes) -> bytes:
    """Bytewise exclusive OR (truncated to the shortest input)."""
    return bytes(x ^ y for x, y in zip(a, b, strict=False))


def aes_ecb(key: bytes, block: bytes) -> bytes:
    """Encrypt one 16-byte block with AES-ECB."""
    encryptor = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    return encryptor.update(block) + encryptor.finalize()


def aes_cmac(key: bytes, message: bytes) -> bytes:
    """Compute the AES-CMAC of `message`."""
    cmac = CMAC(algorithms.AES(key))
    cmac.update(message)
    return cmac.finalize()


def ccm_encrypt(key: bytes, nonce: bytes, plaintext: bytes, mic_length: int) -> bytes:
    """Encrypt with AES-CCM; the `mic_length`-byte MIC is appended."""
    return AESCCM(key, tag_length=mic_length).encrypt(nonce, plaintext, None)


def ccm_decrypt(key: bytes, nonce: bytes, ciphertext: bytes, mic_length: int) -> bytes:
    """Decrypt with AES-CCM. Raise `cryptography.exceptions.InvalidTag` if the MIC is wrong."""
    return AESCCM(key, tag_length=mic_length).decrypt(nonce, ciphertext, None)


def s1(message: bytes) -> bytes:
    """Salt generation function s1."""
    return aes_cmac(ZERO_KEY, message)


def k1(n: bytes, salt: bytes, p: bytes) -> bytes:
    """Key derivation function k1."""
    return aes_cmac(aes_cmac(salt, n), p)


def k2(n: bytes, p: bytes) -> tuple[int, bytes, bytes]:
    """Derive from a NetKey: (NID, encryption key, privacy key)."""
    t = aes_cmac(s1(b"smk2"), n)
    t1 = aes_cmac(t, p + b"\x01")
    t2 = aes_cmac(t, t1 + p + b"\x02")
    t3 = aes_cmac(t, t2 + p + b"\x03")
    output = (int.from_bytes(t1 + t2 + t3, "big") % (1 << 263)).to_bytes(33, "big")
    return output[0], output[1:17], output[17:33]


def k3(n: bytes) -> bytes:
    """Derive the 64-bit Network ID from a NetKey."""
    return aes_cmac(aes_cmac(s1(b"smk3"), n), b"id64\x01")[8:]


def k4(n: bytes) -> int:
    """Derive the 6-bit AID from an AppKey."""
    return aes_cmac(aes_cmac(s1(b"smk4"), n), b"id6\x01")[15] & 0x3F
