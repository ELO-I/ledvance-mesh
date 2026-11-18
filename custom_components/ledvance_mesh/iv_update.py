"""Following and driving the IV Update procedure for our node.

Following: when an authenticated network beacon announces a newer IV index, we adopt it.
Driving: when our sequence numbers run low, we start the update, announce it to the network,
then complete it once the network has followed. Each phase lasts at least 96 hours.
"""

from __future__ import annotations

from collections.abc import Callable
import logging
import time

from .mesh import IvState, MeshNetwork, SecureNetworkBeacon
from .mesh.iv import MIN_PHASE_DURATION, SEQUENCE_UPDATE_THRESHOLD
from .store import MeshStateStore

_LOGGER = logging.getLogger(__name__)


class IvUpdateManager:
    """Moves the network IV index forward, keeping it persisted."""

    def __init__(
        self,
        network: MeshNetwork,
        store: MeshStateStore,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._network = network
        self._store = store
        self._clock = clock
        # Last state announced by the proxy node (None until a beacon arrives).
        self._proxy_iv: IvState | None = None

    @property
    def proxy_iv(self) -> IvState | None:
        """Last IV state announced by the network."""
        return self._proxy_iv

    @property
    def must_announce(self) -> bool:
        """True if the network has not followed our state yet: our beacon must be sent."""
        return self._proxy_iv is not None and self._proxy_iv != self._network.iv

    async def async_on_beacon(self, beacon: SecureNetworkBeacon) -> None:
        """Take an authenticated network beacon into account."""
        self._proxy_iv = IvState(beacon.iv_index, beacon.iv_update)
        new = self._network.iv.after_beacon(beacon.iv_index, beacon.iv_update)
        if new != self._network.iv:
            _LOGGER.info("IV index: the network announces %s, following it", new)
            await self._async_set(new)

    async def async_advance(self) -> None:
        """Start or complete an update when the conditions are met (periodic call)."""
        iv = self._network.iv
        if self._clock() - self._store.iv_changed_at < MIN_PHASE_DURATION:
            return
        if iv.update_in_progress:
            # Only go back to normal (transmitting with the new index) if the network knows the
            # new index: otherwise the lamps would reject our messages.
            if self._proxy_iv is not None and self._proxy_iv.index == iv.index:
                _LOGGER.info("IV index: update complete, index %d", iv.index)
                await self._async_set(iv.complete_update())
        elif self._store.sequence.value >= SEQUENCE_UPDATE_THRESHOLD:
            _LOGGER.info("IV index: sequence numbers running low, update started")
            await self._async_set(iv.begin_update())

    async def _async_set(self, iv: IvState) -> None:
        self._network.iv = iv
        await self._store.async_set_iv(iv)
