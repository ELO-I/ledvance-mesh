"""Mesh client: exchanges access messages with the network through a proxy node."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
import logging
import os
from typing import Any

from .beacon import SecureNetworkBeacon, decode_secure_network_beacon, encode_secure_network_beacon
from .bearer import Bearer
from .errors import MeshTimeoutError
from .messages import Opcode, split_opcode
from .network import NetworkPdu, decode_network_pdu, encode_network_pdu
from .proxy import ProxyPduType, encode_set_blacklist_filter
from .state import MeshNetwork
from .transport import (
    OutgoingMessage,
    SegmentProgress,
    SegmentReassembler,
    decode_segment_ack,
    decrypt_access_message,
    encode_segment_ack,
    encrypt_access_message,
    is_segmented,
    unsegmented_access_pdu,
)

_LOGGER = logging.getLogger(__name__)

DEFAULT_TTL = 7
REQUEST_TIMEOUT = 3.0
REQUEST_ATTEMPTS = 3

# How long to wait for the acknowledgement of a segmented message before resending the
# missing segments.
_SEGMENT_ACK_TIMEOUT = 0.6

# Delay without a new segment before acknowledging those already received (the sender then
# resends the missing ones), then delay after which an incomplete message is dropped.
_ACK_DELAY = 0.4
_INCOMPLETE_TIMEOUT = 10.0


@dataclass(frozen=True, slots=True)
class AccessMessage:
    """Access message received without being requested (published by a node)."""

    src: int
    dst: int
    opcode: int
    params: bytes


class MeshClient:
    """Client of a mesh network, connected to a proxy node through a bearer."""

    def __init__(
        self,
        bearer: Bearer,
        network: MeshNetwork,
        *,
        on_message: Callable[[AccessMessage], None] | None = None,
        on_beacon: Callable[[SecureNetworkBeacon], None] | None = None,
    ) -> None:
        self._bearer = bearer
        self._network = network
        self._on_message = on_message
        self._on_beacon = on_beacon
        self._pending: dict[tuple[int, int], list[asyncio.Future[bytes]]] = {}
        # Acknowledgements awaited for our segmented messages: (destination, SeqZero) -> queue.
        self._acks: dict[tuple[int, int], asyncio.Queue[int]] = {}
        self._reassembler = SegmentReassembler()
        self._timers: dict[int, asyncio.TimerHandle] = {}  # per source of a segmented message
        self._tasks: set[asyncio.Task[None]] = set()
        # Random start: two successive connections do not reuse the same identifiers.
        self._tid = os.urandom(1)[0]
        bearer.set_receiver(self._on_pdu)

    async def start(self) -> None:
        """Configure the proxy so that it forwards all network traffic to us."""
        network = self._network
        pdu = encode_set_blacklist_filter(
            network.keys, network.iv.tx_index, src=network.address, seq=network.sequence.next()
        )
        await self._bearer.send(ProxyPduType.CONFIGURATION, pdu)

    def close(self) -> None:
        """Cancel pending timers (to be called when the connection is dropped)."""
        for timer in self._timers.values():
            timer.cancel()
        self._timers.clear()
        for task in self._tasks:
            task.cancel()

    async def send_beacon(self) -> None:
        """Announce our IV index to the network (this is how an update spreads)."""
        beacon = encode_secure_network_beacon(self._network.keys, self._network.iv)
        await self._bearer.send(ProxyPduType.BEACON, beacon)

    def next_tid(self) -> int:
        """Transaction identifier (tells two successive commands apart)."""
        self._tid = (self._tid + 1) & 0xFF
        return self._tid

    async def request(
        self,
        destination: int,
        payload: bytes,
        response: Opcode,
        *,
        use_device_key: bool = False,
        timeout: float = REQUEST_TIMEOUT,
        attempts: int = REQUEST_ATTEMPTS,
    ) -> bytes:
        """Send `payload` to `destination` and return the parameters of the `response` message.

        The message is sent again after each timeout without an answer (radio is unreliable).
        """
        network = self._network
        device_key = network.device_keys[destination] if use_device_key else None
        loop = asyncio.get_running_loop()
        future: asyncio.Future[bytes] = loop.create_future()
        waiters = self._pending.setdefault((destination, int(response)), [])
        waiters.append(future)
        try:
            for attempt in range(1, attempts + 1):
                message = encrypt_access_message(
                    network.keys,
                    network.iv.tx_index,
                    src=network.address,
                    dst=destination,
                    payload=payload,
                    device_key=device_key,
                    seq=network.sequence.next(),
                )
                deadline = loop.time() + timeout
                await self._transmit(destination, message, future, deadline)
                try:
                    async with asyncio.timeout_at(deadline):
                        return await asyncio.shield(future)
                except TimeoutError:
                    _LOGGER.debug(
                        "no answer from %#06x (attempt %d/%d)", destination, attempt, attempts
                    )
            raise MeshTimeoutError(f"node {destination:#06x} does not answer")
        finally:
            waiters.remove(future)

    async def _transmit(
        self,
        destination: int,
        message: OutgoingMessage,
        response: asyncio.Future[bytes],
        deadline: float,
    ) -> None:
        """Send a message; if segmented, resend the segments the node did not receive."""
        missing = list(range(len(message.transport_pdus)))
        await self._send_segments(destination, message, missing, first=True)
        if not message.segmented:
            return
        loop = asyncio.get_running_loop()
        acks = self._acks[destination, message.seq_zero] = asyncio.Queue()
        try:
            while not response.done() and loop.time() + _SEGMENT_ACK_TIMEOUT < deadline:
                try:
                    async with asyncio.timeout(_SEGMENT_ACK_TIMEOUT):
                        block_ack = await acks.get()
                except TimeoutError:
                    block_ack = None  # neither acknowledgement nor answer: resend what is missing
                if block_ack == message.all_segments or block_ack == 0:
                    return  # all received, or the node refuses the message (busy): wait
                if block_ack is not None:
                    missing = [i for i in missing if not (block_ack >> i) & 1]
                if not response.done():
                    await self._send_segments(destination, message, missing, first=False)
        finally:
            del self._acks[destination, message.seq_zero]

    async def _send_segments(
        self, destination: int, message: OutgoingMessage, indexes: list[int], *, first: bool
    ) -> None:
        network = self._network
        for index in indexes:
            # The first PDU carries the nonce's sequence number; all others, retransmissions
            # included, take a new one.
            seq = message.first_seq if first and index == 0 else network.sequence.next()
            pdu = NetworkPdu(
                False, DEFAULT_TTL, seq, network.address, destination, message.transport_pdus[index]
            )
            await self._bearer.send(
                ProxyPduType.NETWORK, encode_network_pdu(network.keys, network.iv.tx_index, pdu)
            )

    # --- Reception -------------------------------------------------------------------------

    def _on_pdu(self, pdu_type: ProxyPduType, data: bytes) -> None:
        if pdu_type is ProxyPduType.NETWORK:
            self._on_network_pdu(data)
        elif pdu_type is ProxyPduType.BEACON:
            beacon = decode_secure_network_beacon(self._network.keys, data)
            if beacon is not None and self._on_beacon is not None:
                self._on_beacon(beacon)

    def _on_network_pdu(self, data: bytes) -> None:
        network = self._network
        iv_index = network.iv.rx_index(data[0] >> 7) if data else None
        if iv_index is None:
            return
        pdu = decode_network_pdu(network.keys, iv_index, data)
        if pdu is None or pdu.src == network.address:
            return
        to_us = pdu.dst == network.address
        if not to_us and pdu.dst not in network.groups:
            return
        if not network.accept(pdu.src, iv_index, pdu.seq):
            return  # already received (relay retransmission) or replayed
        if pdu.ctl:
            ack = decode_segment_ack(pdu)
            if ack is not None and (waiting := self._acks.get((pdu.src, ack.seq_zero))):
                waiting.put_nowait(ack.block_ack)
            return

        if is_segmented(pdu):
            progress = self._reassembler.feed(pdu, iv_index)
            if progress is None:
                return
            if to_us:
                self._acknowledge(pdu.src, progress)
            if progress.message is None:
                return
            message = progress.message
        else:
            message = unsegmented_access_pdu(pdu, iv_index)

        payload = decrypt_access_message(network.keys, message, network.device_keys)
        if not payload:
            return
        opcode, params = split_opcode(payload)
        for future in self._pending.get((pdu.src, opcode), ()):
            if not future.done():
                future.set_result(params)
                return
        if self._on_message is not None:
            self._on_message(AccessMessage(pdu.src, pdu.dst, opcode, params))

    def _acknowledge(self, src: int, progress: SegmentProgress) -> None:
        """Acknowledge: right away if the message is complete, otherwise after a pause."""
        if timer := self._timers.pop(src, None):
            timer.cancel()
        if progress.complete:
            self._spawn(self._send_ack(src, progress))
        else:
            loop = asyncio.get_running_loop()
            self._timers[src] = loop.call_later(_ACK_DELAY, self._on_ack_delay, src, progress)

    def _on_ack_delay(self, src: int, progress: SegmentProgress) -> None:
        self._spawn(self._send_ack(src, progress))
        loop = asyncio.get_running_loop()
        self._timers[src] = loop.call_later(_INCOMPLETE_TIMEOUT, self._on_incomplete, src)

    def _on_incomplete(self, src: int) -> None:
        del self._timers[src]
        self._reassembler.discard(src)
        _LOGGER.debug("segmented message from %#06x dropped: missing segments", src)

    async def _send_ack(self, src: int, progress: SegmentProgress) -> None:
        network = self._network
        pdu = encode_segment_ack(
            network.keys,
            network.iv.tx_index,
            src=network.address,
            dst=src,
            seq=network.sequence.next(),
            ttl=DEFAULT_TTL,
            seq_zero=progress.seq_zero,
            block_ack=progress.block_ack,
        )
        await self._bearer.send(ProxyPduType.NETWORK, pdu)

    def _spawn(self, coroutine: Coroutine[Any, Any, None]) -> None:
        task = asyncio.get_running_loop().create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._on_task_done)

    def _on_task_done(self, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and (error := task.exception()) is not None:
            _LOGGER.debug("acknowledgement not sent: %s", error)
