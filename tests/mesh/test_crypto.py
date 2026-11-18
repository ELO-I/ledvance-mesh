"""Mesh Profile 1.0.1 test vectors (§8.1 and §8.3)."""

from custom_components.ledvance_mesh.mesh.crypto import k1, k2, k3, k4, s1
from custom_components.ledvance_mesh.mesh.keys import NetworkKeys
from custom_components.ledvance_mesh.mesh.network import (
    NetworkPdu,
    decode_network_pdu,
    encode_network_pdu,
)

h = bytes.fromhex


def test_s1() -> None:
    assert s1(b"test") == h("b73cefbd641ef2ea598c2b6efb62f79c")


def test_k1() -> None:
    assert k1(
        h("3216d1509884b533248541792b877f98"),
        h("2ba14ffa0df84a2831938d57d276cab4"),
        h("5a09d60797eeb4478aada59db3352a0d"),
    ) == h("f6ed15a8934afbe7d83e8dcb57fcf5d7")


def test_k2() -> None:
    nid, encryption, privacy = k2(h("f7a2a44f8e8a8029064f173ddc1e2b00"), b"\x00")
    assert nid == 0x7F
    assert encryption == h("9f589181a0f50de73c8070c7a6d27f46")
    assert privacy == h("4c715bd4a64b938f99b453351653124f")


def test_k3_k4() -> None:
    assert k3(h("f7a2a44f8e8a8029064f173ddc1e2b00")) == h("ff046958233db014")
    assert k4(h("3216d1509884b533248541792b877f98")) == 0x38


def test_network_pdu_matches_specification_sample() -> None:
    keys = NetworkKeys(h("7dd7364cd842ad18c17c2b820c84c3d6"), bytes(16))
    assert keys.nid == 0x68
    assert keys.encryption_key == h("0953fa93e7caac9638f58820220a398e")
    assert keys.privacy_key == h("8b84eedec100067d670971dd2aa700cf")
    pdu = NetworkPdu(True, 0, 1, 0x1201, 0xFFFD, h("034b50057e400000010000"))
    encoded = encode_network_pdu(keys, 0x12345678, pdu)
    assert encoded == h("68eca487516765b5e5bfdacbaf6cb7fb6bff871f035444ce83a670df")
    assert decode_network_pdu(keys, 0x12345678, encoded) == pdu
