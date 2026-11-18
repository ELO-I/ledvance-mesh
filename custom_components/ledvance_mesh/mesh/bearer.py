"""Bearers: the link that carries proxy PDUs."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from enum import Enum
import logging
from typing import Any, Protocol

from .proxy import ProxyPduAssembler, ProxyPduType, split_proxy_pdu

_LOGGER = logging.getLogger(__name__)

PduReceiver = Callable[[ProxyPduType, bytes], None]

# Minimum GATT frame size (default MTU of 23 minus the 3-byte ATT header).
_MIN_FRAME_SIZE = 20
# Pause between two frames of the same PDU: some BLE stacks drop write-without-response bursts.
_FRAME_INTERVAL = 0.02


class Bearer(Protocol):
    """A link able to send and receive proxy PDUs."""

    def set_receiver(self, receiver: PduReceiver) -> None:
        """Register the function called for each received proxy PDU."""

    async def send(self, pdu_type: ProxyPduType, payload: bytes) -> None:
        """Send a proxy PDU."""


class GattService(Enum):
    """Mesh GATT service and its two characteristics (in, out)."""

    PROVISIONING = (
        "00001827-0000-1000-8000-00805f9b34fb",
        "00002adb-0000-1000-8000-00805f9b34fb",
        "00002adc-0000-1000-8000-00805f9b34fb",
    )
    PROXY = (
        "00001828-0000-1000-8000-00805f9b34fb",
        "00002add-0000-1000-8000-00805f9b34fb",
        "00002ade-0000-1000-8000-00805f9b34fb",
    )

    @property
    def uuid(self) -> str:
        """Service UUID."""
        return self.value[0]

    @property
    def data_in(self) -> str:
        """Write characteristic (client to device)."""
        return self.value[1]

    @property
    def data_out(self) -> str:
        """Notify characteristic (device to client)."""
        return self.value[2]


class GattClient(Protocol):
    """Subset of `bleak.BleakClient` used by the bearer."""

    @property
    def mtu_size(self) -> int:
        """Negotiated MTU."""

    async def start_notify(self, char: str, callback: Callable[[Any, bytearray], Any], /) -> None:
        """Enable notifications on a characteristic."""

    async def stop_notify(self, char: str, /) -> None:
        """Disable notifications on a characteristic."""

    async def write_gatt_char(self, char: str, data: bytes, response: bool, /) -> None:
        """Write to a characteristic."""


class GattBearer:
    """GATT bearer (provisioning or proxy) over an already connected BLE client."""

    def __init__(self, client: GattClient, service: GattService) -> None:
        self._client = client
        self._service = service
        self._assembler = ProxyPduAssembler()
        self._receiver: PduReceiver | None = None
        # A PDU split into several frames must not be interleaved with another one.
        self._send_lock = asyncio.Lock()

    def set_receiver(self, receiver: PduReceiver) -> None:
        """Register the function called for each received proxy PDU."""
        self._receiver = receiver

    async def start(self) -> None:
        """Enable the device notifications."""
        await self._client.start_notify(self._service.data_out, self._on_notification)

    async def send(self, pdu_type: ProxyPduType, payload: bytes) -> None:
        """Send a proxy PDU, segmented according to the MTU."""
        frame_size = max(_MIN_FRAME_SIZE, self._client.mtu_size - 3)
        async with self._send_lock:
            for frame in split_proxy_pdu(pdu_type, payload, frame_size):
                await self._client.write_gatt_char(self._service.data_in, frame, False)
                await asyncio.sleep(_FRAME_INTERVAL)

    def _on_notification(self, _characteristic: Any, data: bytearray) -> None:
        try:
            complete = self._assembler.feed(bytes(data))
        except ValueError:
            _LOGGER.debug("invalid proxy frame ignored: %s", bytes(data).hex())
            return
        if complete is not None and self._receiver is not None:
            self._receiver(*complete)
