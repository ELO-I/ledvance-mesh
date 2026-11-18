"""Diagnostics: network state, without any key."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from .const import CONF_APP_KEY, CONF_DEVICE_KEY, CONF_NET_KEY
from .coordinator import LedvanceMeshConfigEntry

_REDACTED_KEYS = {CONF_NET_KEY, CONF_APP_KEY, CONF_DEVICE_KEY}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: LedvanceMeshConfigEntry
) -> dict[str, Any]:
    """Return the entry diagnostics, keys redacted."""
    coordinator = entry.runtime_data
    iv = coordinator.network.iv
    return {
        "entry": async_redact_data(dict(entry.data), _REDACTED_KEYS),
        "network": {
            "proxy": coordinator.gateway.proxy_address,
            "iv_index": iv.index,
            "iv_update_in_progress": iv.update_in_progress,
            "iv_announced_by_network": str(coordinator.iv_update.proxy_iv),
            "next_sequence_number": coordinator.store.sequence.value,
        },
        "lamps": {
            subentry_id: {
                **async_redact_data(dict(subentry.data), _REDACTED_KEYS),
                "state": str(coordinator.data.get(subentry_id)),
                "seconds_since_last_message": coordinator.presence.message_age(subentry_id),
                "seconds_since_last_advertisement": coordinator.presence.advertisement_age(
                    subentry_id
                ),
            }
            for subentry_id, subentry in entry.subentries.items()
        },
    }
