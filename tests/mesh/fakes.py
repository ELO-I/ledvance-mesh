"""Fake devices to test the mesh stack without a radio."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import os
import struct

from cryptography.hazmat.primitives.asymmetric import ec

from custom_components.ledvance_mesh.mesh.beacon import encode_secure_network_beacon
from custom_components.ledvance_mesh.mesh.crypto import aes_cmac, ccm_decrypt, k1, s1
from custom_components.ledvance_mesh.mesh.iv import IvState
from custom_components.ledvance_mesh.mesh.keys import NetworkKeys
from custom_components.ledvance_mesh.mesh.messages import encode_opcode, split_opcode
from custom_components.ledvance_mesh.mesh.network import (
    NetworkPdu,
    decode_network_pdu,
    encode_network_pdu,
)
from custom_components.ledvance_mesh.mesh.proxy import ProxyPduType
from custom_components.ledvance_mesh.mesh.transport import (
    SegmentAck,
    SegmentReassembler,
    decode_segment_ack,
    decrypt_access_message,
    encode_segment_ack,
    encrypt_access_message,
    is_segmented,
    unsegmented_access_pdu,
)


class Counter:
    """Simple sequence source."""

    def __init__(self, start: int = 0) -> None:
        self.value = start

    def next(self) -> int:
        value = self.value
        self.value += 1
        return value


class FakeNodeBearer:
    """Bearer linked to a fake node: it receives our messages, acknowledges them and answers.

    `handler(opcode, params)` returns the answer as (opcode, params), or None
    not to answer.
    """

    def __init__(
        self,
        keys: NetworkKeys,
        *,
        node_address: int,
        client_address: int,
        device_key: bytes,
        handler: Callable[[int, bytes], tuple[int, bytes] | None],
        iv: IvState | None = None,
    ) -> None:
        self.keys, self.node_address, self.client_address = keys, node_address, client_address
        self.device_key, self.handler = device_key, handler
        self.iv = iv or IvState(0)
        self.counter = Counter(1)
        self.sent: list[tuple[ProxyPduType, bytes]] = []
        self.received_acks: list[SegmentAck] = []
        self.drop_segments: set[int] = set()  # indexes of segments to lose once
        self._reassembler = SegmentReassembler()
        self._receiver: Callable[[ProxyPduType, bytes], None] | None = None

    def set_receiver(self, receiver: Callable[[ProxyPduType, bytes], None]) -> None:
        self._receiver = receiver

    async def send(self, pdu_type: ProxyPduType, payload: bytes) -> None:
        self.sent.append((pdu_type, payload))
        if pdu_type is not ProxyPduType.NETWORK:
            return
        iv_index = self.iv.rx_index(payload[0] >> 7)
        assert iv_index is not None
        pdu = decode_network_pdu(self.keys, iv_index, payload)
        assert pdu is not None
        if pdu.dst != self.node_address:
            return  # message for another node
        if pdu.ctl:
            ack = decode_segment_ack(pdu)
            assert ack is not None
            self.received_acks.append(ack)
            return
        if is_segmented(pdu):
            index = (int.from_bytes(pdu.transport_pdu[:4], "big") >> 5) & 0x1F
            if index in self.drop_segments:
                self.drop_segments.discard(index)
                return
            progress = self._reassembler.feed(pdu, iv_index)
            assert progress is not None
            self._emit(
                encode_segment_ack(
                    self.keys,
                    self.iv.tx_index,
                    src=self.node_address,
                    dst=pdu.src,
                    seq=self.counter.next(),
                    ttl=5,
                    seq_zero=progress.seq_zero,
                    block_ack=progress.block_ack,
                )
            )
            if progress.message is None:
                return
            message = progress.message
        else:
            message = unsegmented_access_pdu(pdu, iv_index)
        payload = decrypt_access_message(self.keys, message, {pdu.src: self.device_key})
        assert payload is not None, "the node could not decrypt the request"
        used_device_key = message.app_key_id is None
        reply = self.handler(*split_opcode(payload))
        if reply is not None:
            self.transmit(*reply, dst=self.client_address, use_device_key=used_device_key)

    def transmit(
        self, opcode: int, params: bytes, *, dst: int, use_device_key: bool = False
    ) -> None:
        """Send a message from the node (answer, or publication to a group)."""
        message = encrypt_access_message(
            self.keys,
            self.iv.tx_index,
            src=self.node_address,
            dst=dst,
            payload=encode_opcode(opcode) + params,
            device_key=self.device_key if use_device_key else None,
            seq=self.counter.next(),
        )
        for index, transport in enumerate(message.transport_pdus):
            seq = message.first_seq if index == 0 else self.counter.next()
            pdu = NetworkPdu(False, 5, seq, self.node_address, dst, transport)
            self._emit(encode_network_pdu(self.keys, self.iv.tx_index, pdu))

    def _emit(self, pdu: bytes, pdu_type: ProxyPduType = ProxyPduType.NETWORK) -> None:
        assert self._receiver is not None
        asyncio.get_running_loop().call_soon(self._receiver, pdu_type, pdu)

    def beacon(self) -> None:
        """Announce the node IV state, as a proxy does on connection."""
        self._emit(encode_secure_network_beacon(self.keys, self.iv), ProxyPduType.BEACON)


class FakeProvisioneeBearer:
    """Bearer linked to a fake device that lets itself be provisioned (device side)."""

    ELEMENT_COUNT = 4

    def __init__(self) -> None:
        self.provisioned: tuple[bytes, int, int] | None = None  # (NetKey, IV index, address)
        self.device_key: bytes | None = None
        self._receiver: Callable[[ProxyPduType, bytes], None] | None = None
        self._private_key = ec.generate_private_key(ec.SECP256R1())
        self._invite = self._start = self._provisioner_public = b""
        self._capabilities = bytes([self.ELEMENT_COUNT, 0, 1, 0, 1, 0, 0, 0, 0, 0, 0])
        self._device_public = self._public_bytes()
        self._random = os.urandom(16)
        self._secret = self._confirmation_key = self._confirmation_salt = b""
        self._provisioner_confirmation = self._provisioner_random = b""

    def _public_bytes(self) -> bytes:
        numbers = self._private_key.public_key().public_numbers()
        return numbers.x.to_bytes(32, "big") + numbers.y.to_bytes(32, "big")

    def set_receiver(self, receiver: Callable[[ProxyPduType, bytes], None]) -> None:
        self._receiver = receiver

    def _reply(self, opcode: int, params: bytes = b"") -> None:
        assert self._receiver is not None
        asyncio.get_running_loop().call_soon(
            self._receiver, ProxyPduType.PROVISIONING, bytes([opcode]) + params
        )

    async def send(self, pdu_type: ProxyPduType, payload: bytes) -> None:
        assert pdu_type is ProxyPduType.PROVISIONING
        opcode, params = payload[0], payload[1:]
        if opcode == 0x00:  # Invite
            self._invite = params
            self._reply(0x01, self._capabilities)
        elif opcode == 0x02:  # Start
            self._start = params
        elif opcode == 0x03:  # provisioner public key
            self._provisioner_public = params
            peer = ec.EllipticCurvePublicNumbers(
                int.from_bytes(params[:32], "big"),
                int.from_bytes(params[32:], "big"),
                ec.SECP256R1(),
            ).public_key()
            self._secret = self._private_key.exchange(ec.ECDH(), peer)
            inputs = self._invite + self._capabilities + self._start + params + self._device_public
            self._confirmation_salt = s1(inputs)
            self._confirmation_key = k1(self._secret, self._confirmation_salt, b"prck")
            self._reply(0x03, self._device_public)
        elif opcode == 0x05:  # provisioner confirmation
            self._provisioner_confirmation = params
            self._reply(0x05, aes_cmac(self._confirmation_key, self._random + bytes(16)))
        elif opcode == 0x06:  # provisioner random
            assert (
                aes_cmac(self._confirmation_key, params + bytes(16))
                == self._provisioner_confirmation
            )
            self._provisioner_random = params
            self._reply(0x06, self._random)
        elif opcode == 0x07:  # provisioning data
            salt = s1(self._confirmation_salt + self._provisioner_random + self._random)
            session_key = k1(self._secret, salt, b"prsk")
            nonce = k1(self._secret, salt, b"prsn")[3:]
            data = ccm_decrypt(session_key, nonce, params, 8)
            net_key, _index, flags, iv_index, address = (
                data[:16],
                *struct.unpack(">HBIH", data[16:]),
            )
            self.provisioned = (net_key, iv_index, address)
            self.flags = flags
            self.device_key = k1(self._secret, salt, b"prdk")
            self._reply(0x08)
