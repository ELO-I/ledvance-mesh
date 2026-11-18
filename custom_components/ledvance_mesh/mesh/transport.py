"""Lower and upper transport layers (Mesh Profile §3.5, §3.6).

Transmission: application encryption of an access message and segmentation.
Reception: segment reassembly, acknowledgement, decryption.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from cryptography.exceptions import InvalidTag

from .crypto import ccm_decrypt, ccm_encrypt
from .keys import NetworkKeys
from .network import NetworkPdu, encode_network_pdu

_NONCE_APPLICATION = 0x01
_NONCE_DEVICE = 0x02

_SHORT_MIC = 4
_LONG_MIC = 8
_MAX_UNSEGMENTED_UPPER_PDU = 15
_SEGMENT_SIZE = 12
_SEQ_ZERO_MASK = 0x1FFF
_OPCODE_SEGMENT_ACK = 0x00


@dataclass(frozen=True, slots=True)
class UpperTransportPdu:
    """Complete encrypted access message (reassembled if it was segmented)."""

    src: int
    dst: int
    iv_index: int
    seq: int  # sequence number of the first segment: the nonce's one (SeqAuth)
    app_key_id: int | None  # AppKey AID, None if the message uses a device key
    long_mic: bool
    data: bytes


def _nonce(kind: int, long_mic: bool, seq: int, src: int, dst: int, iv_index: int) -> bytes:
    return (
        bytes([kind, 0x80 if long_mic else 0])
        + seq.to_bytes(3, "big")
        + src.to_bytes(2, "big")
        + dst.to_bytes(2, "big")
        + iv_index.to_bytes(4, "big")
    )


# --- Transmission ------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OutgoingMessage:
    """Encrypted access message, split into transport PDUs ready to be sent."""

    first_seq: int  # nonce sequence number: the one of the first PDU sent
    transport_pdus: tuple[bytes, ...]

    @property
    def segmented(self) -> bool:
        """True if the message spans several PDUs (the destination then acknowledges it)."""
        return len(self.transport_pdus) > 1

    @property
    def seq_zero(self) -> int:
        """Identifier of the message in acknowledgements."""
        return self.first_seq & _SEQ_ZERO_MASK

    @property
    def all_segments(self) -> int:
        """Complete acknowledgement: one bit per segment."""
        return (1 << len(self.transport_pdus)) - 1


def encrypt_access_message(
    keys: NetworkKeys,
    iv_index: int,
    *,
    src: int,
    dst: int,
    payload: bytes,
    device_key: bytes | None,
    seq: int,
) -> OutgoingMessage:
    """Encrypt an access message and split it into segments if it is too long.

    With `device_key`, the message is encrypted with the destination's device key
    (configuration messages); otherwise with the AppKey.
    """
    use_app_key = device_key is None
    key = keys.app_key if device_key is None else device_key
    aid = keys.aid if use_app_key else 0
    akf = int(use_app_key)
    nonce = _nonce(
        _NONCE_APPLICATION if use_app_key else _NONCE_DEVICE, False, seq, src, dst, iv_index
    )
    upper_pdu = ccm_encrypt(key, nonce, payload, _SHORT_MIC)
    if len(upper_pdu) <= _MAX_UNSEGMENTED_UPPER_PDU:
        return OutgoingMessage(seq, (bytes([(akf << 6) | aid]) + upper_pdu,))

    segments = [upper_pdu[i : i + _SEGMENT_SIZE] for i in range(0, len(upper_pdu), _SEGMENT_SIZE)]
    last_index = len(segments) - 1
    header = (1 << 31) | (akf << 30) | (aid << 24) | ((seq & _SEQ_ZERO_MASK) << 10) | last_index
    return OutgoingMessage(
        seq,
        tuple(
            (header | (index << 5)).to_bytes(4, "big") + segment  # SegO: segment index
            for index, segment in enumerate(segments)
        ),
    )


@dataclass(frozen=True, slots=True)
class SegmentAck:
    """Acknowledgement received for one of our segmented messages."""

    seq_zero: int
    block_ack: int  # one bit per received segment; 0: the destination cannot receive it


def decode_segment_ack(pdu: NetworkPdu) -> SegmentAck | None:
    """Decode an acknowledgement; None if the control PDU is something else."""
    transport = pdu.transport_pdu
    if len(transport) != 7 or transport[0] != _OPCODE_SEGMENT_ACK:
        return None
    seq_zero = (int.from_bytes(transport[1:3], "big") >> 2) & _SEQ_ZERO_MASK
    return SegmentAck(seq_zero, int.from_bytes(transport[3:7], "big"))


def encode_segment_ack(
    keys: NetworkKeys,
    iv_index: int,
    *,
    src: int,
    dst: int,
    seq: int,
    ttl: int,
    seq_zero: int,
    block_ack: int,
) -> bytes:
    """Acknowledgement of received segments (`block_ack`: one bit per segment)."""
    transport = (
        bytes([_OPCODE_SEGMENT_ACK])
        + (seq_zero << 2).to_bytes(2, "big")  # OBO = 0, SeqZero, 2 reserved bits
        + block_ack.to_bytes(4, "big")
    )
    return encode_network_pdu(keys, iv_index, NetworkPdu(True, ttl, seq, src, dst, transport))


# --- Reception -----------------------------------------------------------------------------


def is_segmented(pdu: NetworkPdu) -> bool:
    """True if the network PDU carries a segment."""
    return bool(pdu.transport_pdu[0] & 0x80)


def unsegmented_access_pdu(pdu: NetworkPdu, iv_index: int) -> UpperTransportPdu:
    """Access message that fits in a single network PDU."""
    first = pdu.transport_pdu[0]
    return UpperTransportPdu(
        src=pdu.src,
        dst=pdu.dst,
        iv_index=iv_index,
        seq=pdu.seq,
        app_key_id=first & 0x3F if first & 0x40 else None,
        long_mic=False,
        data=pdu.transport_pdu[1:],
    )


@dataclass(frozen=True, slots=True)
class SegmentProgress:
    """Progress of the reception of a segmented message."""

    seq_zero: int
    block_ack: int  # one bit per received segment
    complete: bool
    message: UpperTransportPdu | None  # the message, the first time it is complete


@dataclass(slots=True)
class _Partial:
    seq_auth: int
    last_index: int
    segments: dict[int, bytes] = field(default_factory=dict)


class SegmentReassembler:
    """Reassemble segmented access messages, one in progress per source."""

    def __init__(self) -> None:
        self._partial: dict[int, _Partial] = {}
        self._completed: dict[int, int] = {}  # source -> SeqAuth of the last complete message

    def feed(self, pdu: NetworkPdu, iv_index: int) -> SegmentProgress | None:
        """Add a segment; None if it is inconsistent or belongs to an outdated message."""
        transport = pdu.transport_pdu
        if len(transport) < 5:
            return None
        header = int.from_bytes(transport[:4], "big")
        seq_zero = (header >> 10) & _SEQ_ZERO_MASK
        index, last_index = (header >> 5) & 0x1F, header & 0x1F
        # The first segment has the largest number <= this one whose low bits equal SeqZero.
        first_seq = pdu.seq - ((pdu.seq - seq_zero) & _SEQ_ZERO_MASK)
        if first_seq < 0 or index > last_index:
            return None
        seq_auth = (iv_index << 24) | first_seq
        all_segments = (1 << (last_index + 1)) - 1

        if self._completed.get(pdu.src) == seq_auth:
            # Retransmitted segment of a message already received: our acknowledgement got lost.
            return SegmentProgress(seq_zero, all_segments, True, None)
        partial = self._partial.get(pdu.src)
        if partial is None or seq_auth > partial.seq_auth:
            partial = self._partial[pdu.src] = _Partial(seq_auth, last_index)
        elif seq_auth < partial.seq_auth or last_index != partial.last_index:
            return None
        partial.segments[index] = transport[4:]
        block_ack = sum(1 << received for received in partial.segments)
        if block_ack != all_segments:
            return SegmentProgress(seq_zero, block_ack, False, None)

        del self._partial[pdu.src]
        self._completed[pdu.src] = seq_auth
        message = UpperTransportPdu(
            src=pdu.src,
            dst=pdu.dst,
            iv_index=iv_index,
            seq=first_seq,
            app_key_id=(header >> 24) & 0x3F if (header >> 30) & 1 else None,
            long_mic=bool((header >> 23) & 1),
            data=b"".join(partial.segments[i] for i in range(last_index + 1)),
        )
        return SegmentProgress(seq_zero, all_segments, True, message)

    def discard(self, src: int) -> None:
        """Drop the message in progress from `src` (missing segments never arrived)."""
        self._partial.pop(src, None)


def decrypt_access_message(
    keys: NetworkKeys, message: UpperTransportPdu, device_keys: Mapping[int, bytes]
) -> bytes | None:
    """Decrypt an access message (opcode + parameters); None if it is not for us."""
    if message.app_key_id is not None:
        if message.app_key_id != keys.aid:
            return None
        key, kind = keys.app_key, _NONCE_APPLICATION
    else:
        device_key = device_keys.get(message.src)
        if device_key is None:
            return None
        key, kind = device_key, _NONCE_DEVICE
    nonce = _nonce(kind, message.long_mic, message.seq, message.src, message.dst, message.iv_index)
    try:
        return ccm_decrypt(key, nonce, message.data, _LONG_MIC if message.long_mic else _SHORT_MIC)
    except InvalidTag:
        return None
