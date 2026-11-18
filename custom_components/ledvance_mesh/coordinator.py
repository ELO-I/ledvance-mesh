"""Coordinator: keeps the lamp states up to date and runs commands.

There is no periodic polling. Lamps publish their state on every change; their presence is
tracked through their Bluetooth advertisements and their messages (see `presence`). A full read
only happens when connecting to the network, when a lamp comes back, or on request.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
import time

from homeassistant.components import bluetooth
from homeassistant.components.bluetooth import BluetoothServiceInfoBleak
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import (
    CONF_ADDRESS,
    CONF_DEVICE_KEY,
    CONF_ELEMENT_COUNT,
    CONF_SETUP_VERSION,
    CONF_UNICAST,
    DOMAIN,
    EVENT_LAMP_RESTARTED,
    HEARTBEAT_INTERVAL,
    STATUS_GROUP_ADDRESS,
    SUBENTRY_TYPE_LIGHT,
)
from .gateway import GatewayError, MeshGateway
from .iv_update import IvUpdateManager
from .lamp import SETUP_VERSION, Lamp, LampColorMode, LampCommand, LampState
from .mesh import (
    AccessMessage,
    GattService,
    MeshError,
    MeshNetwork,
    MeshTimeoutError,
    SecureNetworkBeacon,
    get_composition,
    reset_node,
)
from .mesh.client import REQUEST_ATTEMPTS, REQUEST_TIMEOUT
from .mesh.messages import Opcode
from .presence import Presence, PresenceTracker
from .profile import SYLVANIA_SMART_PLUS_COLOR, LampProfile
from .store import MeshStateStore

_LOGGER = logging.getLogger(__name__)

_PRESENCE_INTERVAL = timedelta(seconds=5)
_MAINTENANCE_INTERVAL = timedelta(seconds=30)
# Question asked to a lamp whose advertisements stopped: an answer is expected quickly.
_PROBE_ATTEMPTS = 2
_PROBE_TIMEOUT = 1.5
# Delay before trying again to reach a lamp that did not answer (silent lamp coming back,
# pending configuration). It doubles on every failure: an unplugged or reset lamp must not use
# our sequence numbers forever.
_RETRY_DELAY_MIN = 10.0
_RETRY_DELAY_MAX = 3600.0
# After provisioning, the bulb restarts its mesh stack before answering.
_RESTART_DELAY = 3.0
# Lamp restart detection (see `_check_restart`). On start-up, the lamp publishes lightness,
# temperature and colour at once; the heartbeat only publishes the lightness.
_RESTART_OPCODES = frozenset({Opcode.CTL_STATUS, Opcode.HSL_STATUS})
# Statuses received less than this many seconds after a command: answer to the command.
_COMMAND_QUIET = 3.0
# A burst of statuses gives a single event.
_RESTART_DEBOUNCE = 5.0

type LedvanceMeshConfigEntry = ConfigEntry[LedvanceMeshCoordinator]


class UnsupportedDeviceError(HomeAssistantError):
    """The device does not implement the mesh models this integration needs."""


@dataclass(frozen=True, slots=True)
class ProvisionedLamp:
    """Result of adding a lamp: the content of its subentry."""

    address: str
    unicast: int
    element_count: int
    device_key: bytes
    setup_version: int

    def as_subentry_data(self) -> dict[str, str | int]:
        """Data to store in the config subentry."""
        return {
            CONF_ADDRESS: self.address,
            CONF_UNICAST: self.unicast,
            CONF_ELEMENT_COUNT: self.element_count,
            CONF_DEVICE_KEY: self.device_key.hex(),
            CONF_SETUP_VERSION: self.setup_version,
        }


def lamp_signature(entry: ConfigEntry) -> dict[str, tuple[str, int]]:
    """What, in the subentries, requires reloading the entry when it changes."""
    return {
        subentry_id: (subentry.title, subentry.data[CONF_UNICAST])
        for subentry_id, subentry in entry.subentries.items()
    }


class _Backoff:
    """Spaces out new attempts towards lamps that do not answer."""

    def __init__(self) -> None:
        self._next: dict[str, tuple[float, float]] = {}  # lamp -> (not before, next delay)

    def ready(self, lamp: str) -> bool:
        return time.monotonic() >= self._next.get(lamp, (0.0, 0.0))[0]

    def failed(self, lamp: str) -> None:
        delay = self._next.get(lamp, (0.0, _RETRY_DELAY_MIN))[1]
        self._next[lamp] = (time.monotonic() + delay, min(delay * 2, _RETRY_DELAY_MAX))

    def succeeded(self, lamp: str) -> None:
        self._next.pop(lamp, None)


class LedvanceMeshCoordinator(DataUpdateCoordinator[dict[str, LampState | None]]):
    """Lamp states, keyed by subentry id; None if the lamp is unreachable."""

    config_entry: LedvanceMeshConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: LedvanceMeshConfigEntry,
        network: MeshNetwork,
        store: MeshStateStore,
    ) -> None:
        super().__init__(hass, _LOGGER, config_entry=entry, name=DOMAIN)
        self.network = network
        self.store = store
        self.iv_update = IvUpdateManager(network, store)
        self.presence = PresenceTracker()
        self.gateway = MeshGateway(
            hass,
            network,
            on_message=self._on_message,
            on_beacon=self._on_beacon,
            on_connected=self._on_connected,
        )
        network.groups.add(STATUS_GROUP_ADDRESS)

        self.lamps: dict[str, Lamp] = {}
        self._bluetooth_addresses: dict[str, str] = {}
        self._subentry_by_address: dict[int, str] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        for subentry_id, subentry in entry.subentries.items():
            if subentry.subentry_type != SUBENTRY_TYPE_LIGHT:
                continue
            data = subentry.data
            lamp = Lamp(data[CONF_UNICAST], data[CONF_ELEMENT_COUNT], SYLVANIA_SMART_PLUS_COLOR)
            self.lamps[subentry_id] = lamp
            self._bluetooth_addresses[subentry_id] = data[CONF_ADDRESS]
            self._locks[subentry_id] = asyncio.Lock()
            self._subentry_by_address.update(dict.fromkeys(lamp.addresses, subentry_id))
            self.gateway.register_node(
                lamp.unicast, data[CONF_ADDRESS], bytes.fromhex(data[CONF_DEVICE_KEY])
            )
        self.data = dict.fromkeys(self.lamps)
        # Snapshot of the lamps at start-up: comparing it with the entry reveals a change.
        self.lamp_signature = lamp_signature(entry)
        self._recovering: set[str] = set()
        self._recover_backoff = _Backoff()
        self._configure_backoff = _Backoff()
        self._checking_presence = False
        self._last_command: dict[str, float] = {}
        self._last_restart: dict[str, float] = {}

    @callback
    def async_start(self) -> None:
        """Open the network connection, listen to lamp advertisements, start maintenance."""
        hass, entry = self.hass, self.config_entry
        if self.lamps:
            entry.async_create_background_task(
                hass, self.gateway.async_run(), f"{DOMAIN} connection"
            )
        for subentry_id, address in self._bluetooth_addresses.items():
            entry.async_on_unload(
                bluetooth.async_register_advertisement_callback(
                    hass, self._advertisement_listener(subentry_id), address
                )
            )
        entry.async_on_unload(
            async_track_time_interval(hass, self._async_check_presence, _PRESENCE_INTERVAL)
        )
        entry.async_on_unload(
            async_track_time_interval(hass, self._async_maintain, _MAINTENANCE_INTERVAL)
        )
        entry.async_on_unload(self.gateway.async_stop)

    # --- Reading and commands --------------------------------------------------------------

    async def _async_update_data(self) -> dict[str, LampState | None]:
        """Refresh on request: full read of every lamp."""
        await self.async_read_all()
        return self.data

    async def async_read_all(self) -> None:
        """Read the full state of every lamp; those that do not answer are unreachable."""
        for subentry_id in self.lamps:
            self._publish(subentry_id, await self._async_read(subentry_id))

    async def _async_read(self, subentry_id: str, *, probe: bool = False) -> LampState | None:
        """Read a lamp; None if it does not answer. `probe`: quick, impatient question."""
        lamp = self.lamps[subentry_id]
        attempts, timeout = (
            (_PROBE_ATTEMPTS, _PROBE_TIMEOUT) if probe else (REQUEST_ATTEMPTS, REQUEST_TIMEOUT)
        )
        async with self._locks[subentry_id]:
            try:
                client = await self.gateway.async_get_client()
                state = await lamp.read(
                    client, self._color_mode(subentry_id), attempts=attempts, timeout=timeout
                )
            except (GatewayError, MeshError) as err:
                _LOGGER.debug("lamp %#06x does not answer: %s", lamp.unicast, err)
                return None
        self.presence.message_received(subentry_id)
        return state

    async def async_apply(self, subentry_id: str, command: LampCommand) -> None:
        """Send a command to a lamp and publish the resulting state."""
        lamp = self.lamps[subentry_id]
        try:
            async with self._locks[subentry_id]:
                client = await self.gateway.async_get_client()
                current = self.data.get(subentry_id) or await lamp.read(
                    client, self._color_mode(subentry_id)
                )
                self._last_command[subentry_id] = time.monotonic()
                try:
                    state = await lamp.apply(client, command, current)
                finally:
                    # The lamp publishes its new state right after: this is not a restart.
                    self._last_command[subentry_id] = time.monotonic()
        except (GatewayError, MeshError) as err:
            if isinstance(err, MeshTimeoutError):
                self._publish(subentry_id, None)  # it answered none of the attempts
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="command_failed",
                translation_placeholders={"error": str(err)},
            ) from err
        self.presence.message_received(subentry_id)
        self._publish(subentry_id, state)

    def _color_mode(self, subentry_id: str) -> LampColorMode:
        state = self.data.get(subentry_id)
        return state.color_mode if state else self.store.color_mode(subentry_id)

    @callback
    def _publish(self, subentry_id: str, state: LampState | None) -> None:
        if state is not None:
            self.store.set_color_mode(subentry_id, state.color_mode)
        if self.data.get(subentry_id) != state:
            self.data = {**self.data, subentry_id: state}
            self.async_update_listeners()

    # --- What comes from the network -------------------------------------------------------

    @callback
    def _on_connected(self) -> None:
        """Connection (re)opened: publications may have been missed, read everything again."""
        self.config_entry.async_create_background_task(
            self.hass, self.async_read_all(), f"{DOMAIN} read"
        )

    @callback
    def _on_message(self, message: AccessMessage) -> None:
        """Status published by a lamp."""
        subentry_id = self._subentry_by_address.get(message.src)
        if subentry_id is None:
            return
        self.presence.message_received(subentry_id)
        current = self.data.get(subentry_id)
        if current is None:
            self._recover(subentry_id)
            return
        try:
            state = self.lamps[subentry_id].after_status(current, message.opcode, message.params)
        except MeshError as err:
            _LOGGER.debug("invalid status from %#06x: %s", message.src, err)
            return
        if state is None:
            return
        if (
            message.dst == STATUS_GROUP_ADDRESS
            and message.opcode in _RESTART_OPCODES
            and state == current
        ):
            self._check_restart(subentry_id)
        self._publish(subentry_id, state)

    @callback
    def _check_restart(self, subentry_id: str) -> None:
        """Temperature or colour status published without any value change.

        The heartbeat only publishes the lightness, and an identical command is not published:
        without a recent command, the lamp has just restarted (power cut then restored at the
        wall, even briefly). A burst of statuses gives a single event.
        """
        now = time.monotonic()
        if now - self._last_command.get(subentry_id, -_COMMAND_QUIET) < _COMMAND_QUIET:
            return
        if now - self._last_restart.get(subentry_id, -_RESTART_DEBOUNCE) < _RESTART_DEBOUNCE:
            return
        self._last_restart[subentry_id] = now
        entity_id = er.async_get(self.hass).async_get_entity_id("light", DOMAIN, subentry_id)
        _LOGGER.debug("restart of lamp %s detected", entity_id or subentry_id)
        self.hass.bus.async_fire(
            EVENT_LAMP_RESTARTED, {"entity_id": entity_id, "subentry_id": subentry_id}
        )

    def _advertisement_listener(
        self, subentry_id: str
    ) -> Callable[[BluetoothServiceInfoBleak], None]:
        @callback
        def on_advertisement(service_info: BluetoothServiceInfoBleak) -> None:
            """Bluetooth advertisement of a lamp: it is powered."""
            if GattService.PROXY.uuid not in service_info.service_uuids:
                return  # reset lamp: it is no longer part of the network
            self.presence.advertisement_received(subentry_id)
            if self.data.get(subentry_id) is None:
                self._recover(subentry_id)

        return on_advertisement

    @callback
    def _recover(self, subentry_id: str) -> None:
        """An unreachable lamp shows signs of life again: read its full state."""
        if subentry_id in self._recovering or not self._recover_backoff.ready(subentry_id):
            return
        self._recovering.add(subentry_id)
        self.config_entry.async_create_background_task(
            self.hass, self._async_recover(subentry_id), f"{DOMAIN} lamp back"
        )

    async def _async_recover(self, subentry_id: str) -> None:
        try:
            state = await self._async_read(subentry_id)
            if state is None:
                self._recover_backoff.failed(subentry_id)
            else:
                self._recover_backoff.succeeded(subentry_id)
                self._publish(subentry_id, state)
        finally:
            self._recovering.discard(subentry_id)

    @callback
    def _on_beacon(self, beacon: SecureNetworkBeacon) -> None:
        self.config_entry.async_create_background_task(
            self.hass, self.iv_update.async_on_beacon(beacon), f"{DOMAIN} beacon"
        )

    # --- Presence --------------------------------------------------------------------------

    async def _async_check_presence(self, _now: datetime) -> None:
        """Spot lamps that went silent (switched off at the wall, out of range)."""
        if self._checking_presence:
            return
        self._checking_presence = True
        try:
            proxy = self.gateway.proxy_address
            for subentry_id in self.lamps:
                if self.data.get(subentry_id) is None:
                    continue
                is_proxy = self._bluetooth_addresses[subentry_id] == proxy
                if is_proxy:
                    self.presence.advertisements_stopped(subentry_id)
                verdict = self.presence.assess(subentry_id, is_proxy=is_proxy)
                if verdict is Presence.DOUBTFUL:
                    # Advertisements stopped: real power cut, or just a reception gap?
                    self.presence.advertisements_stopped(subentry_id)
                    self._publish(subentry_id, await self._async_read(subentry_id, probe=True))
                elif verdict is Presence.GONE:
                    self._publish(subentry_id, None)
        finally:
            self._checking_presence = False

    # --- Maintenance -----------------------------------------------------------------------

    async def _async_maintain(self, _now: datetime) -> None:
        """Move the IV index forward and finish pending lamp configurations."""
        await self.iv_update.async_advance()
        if not self.gateway.connected:
            return
        if self.iv_update.must_announce:
            try:
                client = await self.gateway.async_get_client()
                await client.send_beacon()
            except (GatewayError, MeshError, OSError) as err:
                _LOGGER.debug("cannot announce the IV index: %s", err)
        subentries = self.config_entry.subentries
        for subentry_id in self.lamps:
            subentry = subentries.get(subentry_id)
            if (
                subentry is not None
                and subentry.data.get(CONF_SETUP_VERSION, 1) < SETUP_VERSION
                and self._configure_backoff.ready(subentry_id)
            ):
                await self._async_configure(subentry_id)

    async def _async_configure(self, subentry_id: str) -> None:
        """Apply the current configuration to a lamp lagging behind."""
        lamp = self.lamps[subentry_id]
        try:
            async with self._locks[subentry_id]:
                await self._async_configure_lamp(lamp)
        except (GatewayError, MeshError) as err:
            self._configure_backoff.failed(subentry_id)
            _LOGGER.debug("configuration of %#06x postponed: %s", lamp.unicast, err)
            return
        self._configure_backoff.succeeded(subentry_id)
        entry = self.config_entry
        subentry = entry.subentries[subentry_id]
        self.hass.config_entries.async_update_subentry(
            entry, subentry, data={**subentry.data, CONF_SETUP_VERSION: SETUP_VERSION}
        )
        _LOGGER.info("Lamp %s configured (version %d)", subentry.title, SETUP_VERSION)
        if self.data.get(subentry_id) is None:
            self._recover(subentry_id)

    async def _async_configure_lamp(self, lamp: Lamp) -> None:
        client = await self.gateway.async_get_client()
        await lamp.configure(
            client,
            self.network.keys,
            publish_address=STATUS_GROUP_ADDRESS,
            heartbeat=HEARTBEAT_INTERVAL,
        )

    # --- Adding and removing lamps ---------------------------------------------------------

    async def async_add_lamp(self, address: str) -> ProvisionedLamp:
        """Provision the unprovisioned lamp `address`, check it is supported, configure it.

        Once provisioned, the lamp belongs to the network: if its configuration fails (lamp
        briefly unreachable), it is retried automatically later. An unsupported device is reset
        and UnsupportedDeviceError is raised.
        """
        profile = SYLVANIA_SMART_PLUS_COLOR
        result = await self.gateway.async_provision(address, self.store.allocate_addresses)
        # The allocated address is now taken: it must survive a restart.
        await self.store.async_save()
        lamp = Lamp(result.unicast_address, result.element_count, profile)
        self.gateway.register_node(lamp.unicast, address, result.device_key)
        await asyncio.sleep(_RESTART_DELAY)
        try:
            await self._async_check_supported(lamp, profile)
        except UnsupportedDeviceError:
            await self._async_forget(lamp)
            raise
        except (GatewayError, MeshError) as err:
            # Composition not readable now: assume a supported lamp, configured later.
            _LOGGER.debug("composition of %s not read: %s", address, err)
        setup_version = 0
        try:
            await self._async_configure_lamp(lamp)
            setup_version = SETUP_VERSION
        except (GatewayError, MeshError) as err:
            _LOGGER.warning(
                "Lamp %s added but not configured yet (%s): it will be retried automatically",
                address,
                err,
            )
        return ProvisionedLamp(
            address, lamp.unicast, lamp.element_count, result.device_key, setup_version
        )

    async def _async_check_supported(self, lamp: Lamp, profile: LampProfile) -> None:
        client = await self.gateway.async_get_client()
        composition = await get_composition(client, lamp.unicast)
        if missing := profile.missing_models(composition):
            raise UnsupportedDeviceError(
                f"unsupported device (company {composition.company_id:#06x}, product "
                f"{composition.product_id:#06x}): missing {', '.join(missing)}"
            )

    async def _async_forget(self, lamp: Lamp) -> None:
        """Reset a lamp that will not be kept, so that it can be used elsewhere."""
        try:
            await reset_node(await self.gateway.async_get_client(), lamp.unicast)
        except (GatewayError, MeshError) as err:
            _LOGGER.warning("Lamp %#06x could not be reset (%s)", lamp.unicast, err)
        finally:
            self.gateway.unregister_node(lamp.unicast, lamp.element_count)

    async def async_remove_lamp(self, subentry_id: str) -> None:
        """Reset a lamp removed from Home Assistant, so that it can be added again.

        Raise GatewayError or MeshError if the lamp did not confirm its reset.
        """
        lamp = self.lamps[subentry_id]
        try:
            async with self._locks[subentry_id]:
                client = await self.gateway.async_get_client()
                await reset_node(client, lamp.unicast)
        finally:
            self.gateway.unregister_node(lamp.unicast, lamp.element_count)
