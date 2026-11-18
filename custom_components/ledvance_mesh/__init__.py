"""Ledvance Mesh integration: Ledvance / Sylvania SMART+ Bluetooth Mesh lamps."""

from __future__ import annotations

import logging

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from .const import CONF_APP_KEY, CONF_LOCAL_ADDRESS, CONF_NET_KEY
from .coordinator import LedvanceMeshConfigEntry, LedvanceMeshCoordinator, lamp_signature
from .mesh import MeshError, MeshNetwork, NetworkKeys
from .store import MeshStateStore

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.LIGHT]

# Former entry data (minor version 1): the IV index now lives in the store.
_LEGACY_IV_INDEX = "iv_index"


async def async_setup_entry(hass: HomeAssistant, entry: LedvanceMeshConfigEntry) -> bool:
    """Start the mesh network of a config entry."""
    store = MeshStateStore(hass, entry.entry_id)
    await store.async_load()
    network = MeshNetwork(
        keys=NetworkKeys.from_hex(entry.data[CONF_NET_KEY], entry.data[CONF_APP_KEY]),
        address=entry.data[CONF_LOCAL_ADDRESS],
        sequence=store.sequence,
        iv=store.iv,
    )
    coordinator = LedvanceMeshCoordinator(hass, entry, network, store)
    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    coordinator.async_start()
    entry.async_on_unload(entry.add_update_listener(_async_entry_updated))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: LedvanceMeshConfigEntry) -> bool:
    """Stop the mesh network."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_migrate_entry(hass: HomeAssistant, entry: LedvanceMeshConfigEntry) -> bool:
    """Upgrade an entry created by an earlier version."""
    if entry.version > 1:
        return False  # entry created by a newer version of the integration
    if entry.minor_version < 2:
        data = {key: value for key, value in entry.data.items() if key != _LEGACY_IV_INDEX}
        hass.config_entries.async_update_entry(entry, data=data, minor_version=2)
    return True


async def _async_entry_updated(hass: HomeAssistant, entry: LedvanceMeshConfigEntry) -> None:
    """Reload the entry when a lamp is added, removed, renamed or paired again.

    A removed lamp is reset first, so that it can be added again. Updates made by the
    integration itself (configuration version) do not reload anything.
    """
    coordinator = entry.runtime_data
    if lamp_signature(entry) == coordinator.lamp_signature:
        return
    for subentry_id, lamp in coordinator.lamps.items():
        if subentry_id in entry.subentries:
            continue
        try:
            await coordinator.async_remove_lamp(subentry_id)
        except (HomeAssistantError, MeshError) as err:
            _LOGGER.warning(
                "Lamp %#06x removed, but its reset was not confirmed (%s): if it does not show up"
                " in 'Add lamp', switch it on and off 5 times",
                lamp.unicast,
                err,
            )
    hass.config_entries.async_schedule_reload(entry.entry_id)
