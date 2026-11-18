"""Proxy protocol: SAR segmentation."""

import pytest

from custom_components.ledvance_mesh.mesh.proxy import (
    ProxyPduAssembler,
    ProxyPduType,
    split_proxy_pdu,
)


def _reassemble(frames: list[bytes]) -> list[tuple[ProxyPduType, bytes]]:
    assembler = ProxyPduAssembler()
    return [done for frame in frames if (done := assembler.feed(frame))]


def test_small_pdu_fits_in_one_frame() -> None:
    frames = split_proxy_pdu(ProxyPduType.NETWORK, b"abc", 20)
    assert frames == [b"\x00abc"]
    assert _reassemble(frames) == [(ProxyPduType.NETWORK, b"abc")]


@pytest.mark.parametrize("size", [1, 19, 38, 39, 40, 100])
def test_large_pdu_roundtrips_through_sar(size: int) -> None:
    payload = bytes(i % 251 for i in range(size))
    frames = split_proxy_pdu(ProxyPduType.PROVISIONING, payload, 20)
    assert all(len(frame) <= 20 for frame in frames)
    assert _reassemble(frames) == [(ProxyPduType.PROVISIONING, payload)]


def test_sar_flags_are_first_continuation_last() -> None:
    frames = split_proxy_pdu(ProxyPduType.NETWORK, bytes(50), 20)
    assert [frame[0] >> 6 for frame in frames] == [1, 2, 3]


def test_continuation_without_start_is_dropped() -> None:
    assert ProxyPduAssembler().feed(bytes([(3 << 6) | 0, 1, 2])) is None


def test_two_pdus_in_a_row() -> None:
    frames = split_proxy_pdu(ProxyPduType.NETWORK, bytes(30), 20) + split_proxy_pdu(
        ProxyPduType.CONFIGURATION, b"xy", 20
    )
    assert [t for t, _ in _reassemble(frames)] == [ProxyPduType.NETWORK, ProxyPduType.CONFIGURATION]
