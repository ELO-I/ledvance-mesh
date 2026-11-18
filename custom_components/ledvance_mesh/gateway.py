"""Bluetooth gateway: keeps a connection open with a proxy node of the network.

The connection is permanent: the states published by the lamps arrive through it. When it drops,
it is reopened towards the best received lamp. All connections go through the Home Assistant
Bluetooth stack, so the server adapter and ESPHome Bluetooth proxies both work.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import logging

from bleak import BleakClient
from bleak.backends.device import BLEDevice
from bleak.exc import BleakError
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection
from bluetooth_data_tools import monotonic_time_coarse

from homeassistant.components import bluetooth
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from .const import ADVERTISEMENT_MAX_AGE
from .mesh import (
    AccessMessage,
    GattBearer,
    GattService,
    MeshClient,
    MeshError,
    MeshNetwork,
    Provisioner,
    ProvisioningResult,
    SecureNetworkBeacon,
)

_LOGGER = logging.getLogger(__name__)

_CONNECT_ATTEMPTS = 3
_UNKNOWN_RSSI = -127
# Delay between two reconnection attempts: it doubles on every failure, up to the maximum.
_RECONNECT_DELAY_MIN = 5.0
_RECONNECT_DELAY_MAX = 60.0


class GatewayError(HomeAssistantError):
    """No Bluetooth connection could be established with the network."""


class MeshGateway:
    """Persistent connection to a proxy node of the network, and pairing of new lamps."""

    def __init__(
        self,
        hass: HomeAssistant,
        network: MeshNetwork,
        *,
        on_message: Callable[[AccessMessage], None],
        on_beacon: Callable[[SecureNetworkBeacon], None],
        on_connected: Callable[[], None],
    ) -> None:
        self._hass = hass
        self._network = network
        self._on_message = on_message
        self._on_beacon = on_beacon
        self._on_connected = on_connected
        self._node_addresses: dict[int, str] = {}  # mesh address -> Bluetooth address
        self._lock = asyncio.Lock()
        self._disconnected = asyncio.Event()
        self._ble_client: BleakClient | None = None
        self._client: MeshClient | None = None
        self._proxy_address: str | None = None

    def register_node(self, unicast: int, address: str, device_key: bytes) -> None:
        """Declare a lamp of the network (proxy candidate, whose device key is known)."""
        self._node_addresses[unicast] = address
        self._network.device_keys[unicast] = device_key

    def unregister_node(self, unicast: int, element_count: int) -> None:
        """Forget a lamp removed from the network."""
        self._node_addresses.pop(unicast, None)
        self._network.forget_node(unicast, element_count)

    @property
    def connected(self) -> bool:
        """True if a proxy connection is open."""
        return self._ble_client is not None and self._ble_client.is_connected

    @property
    def proxy_address(self) -> str | None:
        """Bluetooth address of the proxy lamp, if the connection is open."""
        return self._proxy_address if self.connected else None

    async def async_run(self) -> None:
        """Keep the connection open, forever (background task)."""
        delay = _RECONNECT_DELAY_MIN
        while True:
            try:
                await self.async_get_client()
            except GatewayError as err:
                _LOGGER.debug("%s; retrying in %d s", err, delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, _RECONNECT_DELAY_MAX)
                continue
            delay = _RECONNECT_DELAY_MIN
            await self._disconnected.wait()

    async def async_get_client(self) -> MeshClient:
        """Return the mesh client, connecting to the best received lamp if needed."""
        async with self._lock:
            if self._client is not None and self.connected:
                return self._client
            await self._async_close()
            for address in self._candidate_addresses():
                try:
                    client = await self._async_open_proxy(address)
                except (BleakError, TimeoutError, OSError, MeshError, GatewayError) as err:
                    _LOGGER.debug("cannot connect to %s: %s", address, err)
                    await self._async_close()
                    continue
                self._on_connected()
                return client
        raise GatewayError("no lamp of the network is reachable over Bluetooth")

    async def async_stop(self) -> None:
        """Release the Bluetooth connection."""
        async with self._lock:
            await self._async_close()

    async def async_provision(
        self, address: str, allocate_address: Callable[[int], int]
    ) -> ProvisioningResult:
        """Provision the unprovisioned lamp `address` into the network."""
        ble_client = await self._async_connect(address)
        bearer = GattBearer(ble_client, GattService.PROVISIONING)
        try:
            await bearer.start()
            return await Provisioner(bearer).provision(
                self._network.keys.net_key, self._network.iv, allocate_address
            )
        finally:
            await self._async_disconnect(ble_client)

    # --- Connections ------------------------------------------------------------------------

    async def _async_open_proxy(self, address: str) -> MeshClient:
        self._ble_client = await self._async_connect(address)
        bearer = GattBearer(self._ble_client, GattService.PROXY)
        await bearer.start()
        self._client = MeshClient(
            bearer, self._network, on_message=self._on_message, on_beacon=self._on_beacon
        )
        await self._client.start()
        self._proxy_address = address
        self._disconnected.clear()
        _LOGGER.info("Connected to the mesh network through lamp %s", address)
        return self._client

    async def _async_connect(self, address: str) -> BleakClient:
        """Open a GATT connection through the Home Assistant Bluetooth stack."""

        def current_device() -> BLEDevice:
            device = bluetooth.async_ble_device_from_address(self._hass, address, connectable=True)
            if device is None:
                raise GatewayError(f"{address} is out of range")
            return device

        return await establish_connection(
            BleakClientWithServiceCache,
            current_device(),
            address,
            disconnected_callback=self._on_ble_disconnected,
            max_attempts=_CONNECT_ATTEMPTS,
            ble_device_callback=current_device,
            # The exposed service changes after provisioning: do not trust the cache.
            use_services_cache=False,
        )

    def _on_ble_disconnected(self, ble_client: BleakClient) -> None:
        if ble_client is self._ble_client:
            _LOGGER.info("Connection to the mesh network lost")
            self._forget_connection()

    def _forget_connection(self) -> None:
        if self._client is not None:
            self._client.close()
        self._ble_client = self._client = None
        self._disconnected.set()

    async def _async_close(self) -> None:
        ble_client = self._ble_client
        self._forget_connection()
        if ble_client is not None:
            await self._async_disconnect(ble_client)

    @staticmethod
    async def _async_disconnect(ble_client: BleakClient) -> None:
        try:
            await ble_client.disconnect()
        except (BleakError, TimeoutError, OSError) as err:
            _LOGGER.debug("disconnection: %s", err)

    def _candidate_addresses(self) -> list[str]:
        """Bluetooth addresses of visible lamps: recently heard first, then by signal strength."""
        now = monotonic_time_coarse()
        visible = []
        for address in self._node_addresses.values():
            info = bluetooth.async_last_service_info(self._hass, address, connectable=True)
            if info is not None:
                recent = now - info.time <= ADVERTISEMENT_MAX_AGE
                rssi = info.rssi if info.rssi is not None else _UNKNOWN_RSSI
                visible.append((recent, rssi, address))
        return [address for _recent, _rssi, address in sorted(visible, reverse=True)]
