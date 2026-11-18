"""Transport layer: application encryption, segmentation, reassembly, acknowledgements."""

from custom_components.ledvance_mesh.mesh.keys import NetworkKeys
from custom_components.ledvance_mesh.mesh.network import NetworkPdu, decode_network_pdu
from custom_components.ledvance_mesh.mesh.transport import (
    SegmentAck,
    SegmentReassembler,
    decode_segment_ack,
    decrypt_access_message,
    encode_segment_ack,
    encrypt_access_message,
    unsegmented_access_pdu,
)

KEYS = NetworkKeys(bytes(range(16)), bytes(range(16, 32)))
DEVICE_KEY = bytes(range(100, 116))
SRC, DST = 0x0010, 0x0001


def _encrypt(payload: bytes, device_key: bytes | None = None, seq: int = 10):  # type: ignore[no-untyped-def]
    return encrypt_access_message(
        KEYS, 0, src=SRC, dst=DST, payload=payload, device_key=device_key, seq=seq
    )


def _pdu(transport: bytes, seq: int) -> NetworkPdu:
    return NetworkPdu(False, 5, seq, SRC, DST, transport)


def test_short_message_roundtrips_with_the_right_key_only() -> None:
    message = _encrypt(b"\x82\x04\x01")
    assert not message.segmented
    upper = unsegmented_access_pdu(_pdu(message.transport_pdus[0], 10), 0)
    assert decrypt_access_message(KEYS, upper, {}) == b"\x82\x04\x01"
    other = NetworkKeys(KEYS.net_key, bytes(range(200, 216)))
    assert decrypt_access_message(other, upper, {}) is None

    with_device_key = _encrypt(b"\x80\x4a", DEVICE_KEY)
    upper = unsegmented_access_pdu(_pdu(with_device_key.transport_pdus[0], 10), 0)
    assert decrypt_access_message(KEYS, upper, {SRC: DEVICE_KEY}) == b"\x80\x4a"
    assert decrypt_access_message(KEYS, upper, {SRC: bytes(16)}) is None
    assert decrypt_access_message(KEYS, upper, {}) is None


def test_long_message_is_reassembled_whatever_the_arrival_order() -> None:
    payload = bytes(range(40))
    message = _encrypt(payload, DEVICE_KEY, seq=0x1FFE)  # SeqZero close to the 13-bit wrap
    assert len(message.transport_pdus) == 4 and message.all_segments == 0b1111
    reassembler = SegmentReassembler()
    progress = None
    for arrival, index in enumerate((2, 0, 0, 3, 1)):  # out of order and duplicate
        seq = message.first_seq + 10 + arrival  # retransmissions take other numbers
        progress = reassembler.feed(_pdu(message.transport_pdus[index], seq), 0)
        assert progress is not None and progress.seq_zero == message.seq_zero
    assert progress is not None and progress.complete and progress.message is not None
    assert decrypt_access_message(KEYS, progress.message, {SRC: DEVICE_KEY}) == payload

    # A segment resent afterwards is recognised: complete, but not delivered a second time.
    again = reassembler.feed(_pdu(message.transport_pdus[1], message.first_seq + 30), 0)
    assert again is not None and again.complete and again.message is None


def test_incomplete_message_reports_which_segments_arrived() -> None:
    message = _encrypt(bytes(40), DEVICE_KEY, seq=100)
    reassembler = SegmentReassembler()
    reassembler.feed(_pdu(message.transport_pdus[0], 100), 0)
    progress = reassembler.feed(_pdu(message.transport_pdus[3], 103), 0)
    assert progress is not None and not progress.complete and progress.block_ack == 0b1001


def test_segment_ack_roundtrip() -> None:
    data = encode_segment_ack(
        KEYS, 0, src=DST, dst=SRC, seq=7, ttl=5, seq_zero=0x1ABC, block_ack=0b101
    )
    pdu = decode_network_pdu(KEYS, 0, data)
    assert pdu is not None and pdu.ctl
    assert decode_segment_ack(pdu) == SegmentAck(0x1ABC, 0b101)
