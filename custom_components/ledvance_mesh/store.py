"""Network state that changes over time: sequence, IV index, addresses, colour modes.

The network keys live in the config entry (they never change).
"""

from __future__ import annotations

import time
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import DOMAIN, FIRST_NODE_ADDRESS, LAST_NODE_ADDRESS
from .lamp import LampColorMode
from .mesh import IvState
from .sequence import PersistentSequence

STORAGE_VERSION = 1
_SAVE_DELAY = 1.0


class MeshStateStore:
    """Persists the dynamic state of a mesh network."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry_id}")
        self._next_address = FIRST_NODE_ADDRESS
        self._color_modes: dict[str, LampColorMode] = {}
        self.sequence = PersistentSequence(0, self._schedule_save)
        self.iv = IvState(0)
        self.iv_changed_at = time.time()  # epoch timestamp of the last IV state change

    async def async_load(self) -> None:
        """Load the state and immediately reserve a first block of sequence numbers."""
        data = await self._store.async_load() or {}
        self._next_address = data.get("next_address", FIRST_NODE_ADDRESS)
        self._color_modes = {
            subentry_id: LampColorMode(mode)
            for subentry_id, mode in data.get("color_modes", {}).items()
        }
        self.sequence = PersistentSequence(data.get("sequence_reserved", 0), self._schedule_save)
        self.iv = IvState(data.get("iv_index", 0), data.get("iv_update_in_progress", False))
        # Without a known date, the minimum phase duration counts from now.
        self.iv_changed_at = data.get("iv_changed_at", time.time())
        # The bound must be on disk before the first number is used.
        await self._store.async_save(self._data())

    def allocate_addresses(self, count: int) -> int:
        """Reserve `count` consecutive addresses and return the first one."""
        first = self._next_address
        if first + count - 1 > LAST_NODE_ADDRESS:
            raise ValueError("no mesh address left")
        self._next_address = first + count
        return first

    async def async_save(self) -> None:
        """Write the state now (to call after any address allocation that is used)."""
        await self._store.async_save(self._data())

    async def async_set_iv(self, iv: IvState) -> None:
        """Adopt a new IV state; the sequence restarts from zero if the transmit index changes."""
        if iv.tx_index != self.iv.tx_index:
            self.sequence.reset()
        self.iv = iv
        self.iv_changed_at = time.time()
        await self._store.async_save(self._data())

    def color_mode(self, subentry_id: str) -> LampColorMode:
        """Last known colour mode of a lamp."""
        return self._color_modes.get(subentry_id, LampColorMode.COLOR_TEMP)

    def set_color_mode(self, subentry_id: str, mode: LampColorMode) -> None:
        """Remember the colour mode of a lamp."""
        if self._color_modes.get(subentry_id) is not mode:
            self._color_modes[subentry_id] = mode
            self._schedule_save()

    def _schedule_save(self, *_: object) -> None:
        self._store.async_delay_save(self._data, _SAVE_DELAY)

    def _data(self) -> dict[str, Any]:
        return {
            "sequence_reserved": self.sequence.reserved,
            "iv_index": self.iv.index,
            "iv_update_in_progress": self.iv.update_in_progress,
            "iv_changed_at": self.iv_changed_at,
            "next_address": self._next_address,
            "color_modes": {k: str(v) for k, v in self._color_modes.items()},
        }
