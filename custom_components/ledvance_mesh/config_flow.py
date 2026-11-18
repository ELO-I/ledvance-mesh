"""Mesh network creation, then adding and re-pairing lamps from the UI."""

from __future__ import annotations

import asyncio
from typing import Any, cast

from bleak.exc import BleakError
from bluetooth_data_tools import monotonic_time_coarse
import probatio

from homeassistant.components import bluetooth
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigEntryState,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryFlow,
    SubentryFlowResult,
)
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError

from .const import (
    ADVERTISEMENT_MAX_AGE,
    CONF_ADDRESS,
    CONF_APP_KEY,
    CONF_LOCAL_ADDRESS,
    CONF_NAME,
    CONF_NET_KEY,
    DOMAIN,
    LOCAL_ADDRESS,
    SUBENTRY_TYPE_LIGHT,
)
from .coordinator import LedvanceMeshConfigEntry, ProvisionedLamp
from .mesh import GattService, MeshError, NetworkKeys

DEFAULT_LAMP_NAME = "Lamp"
CONF_REFRESH = "refresh"


class LedvanceMeshConfigFlow(ConfigFlow, domain=DOMAIN):
    """Create a mesh network (single instance); lamps are added to it afterwards."""

    VERSION = 1
    MINOR_VERSION = 2

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Confirm the creation of a new network, with new random keys."""
        if user_input is None:
            return self.async_show_form(step_id="user")
        keys = NetworkKeys.generate()
        return self.async_create_entry(
            title="Ledvance Mesh",
            data={
                CONF_NET_KEY: keys.net_key.hex(),
                CONF_APP_KEY: keys.app_key.hex(),
                CONF_LOCAL_ADDRESS: LOCAL_ADDRESS,
            },
        )

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        """A lamp is a subentry of the network."""
        return {SUBENTRY_TYPE_LIGHT: LightSubentryFlowHandler}


class LightSubentryFlowHandler(ConfigSubentryFlow):
    """Pair a reset lamp: a new one, or a known lamp to pair again."""

    def __init__(self) -> None:
        self._address = ""
        self._name = DEFAULT_LAMP_NAME
        self._task: asyncio.Task[ProvisionedLamp] | None = None
        self._lamp: ProvisionedLamp | None = None
        self._error = ""

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        """Pick the lamp to add and its name."""
        if self._get_entry().state is not ConfigEntryState.LOADED:
            return self.async_abort(reason="entry_not_loaded")
        if user_input is not None and not user_input.get(CONF_REFRESH):
            self._address, self._name = user_input[CONF_ADDRESS], user_input[CONF_NAME]
            return await self.async_step_provision()
        known = {subentry.data[CONF_ADDRESS] for subentry in self._get_entry().subentries.values()}
        candidates = {
            address: label
            for address, label in self._unprovisioned_lamps().items()
            if address not in known
        }
        if not candidates:
            return await self.async_step_none_found()
        schema = probatio.Schema(
            {
                probatio.Required(CONF_ADDRESS): probatio.In(candidates),
                probatio.Required(CONF_NAME, default=DEFAULT_LAMP_NAME): str,
                probatio.Optional(CONF_REFRESH, default=False): bool,
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema)

    async def async_step_none_found(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """No lamp in range: submitting searches again."""
        if user_input is not None:
            return await self.async_step_user()
        return self.async_show_form(step_id="none_found")

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Pair again a known lamp that was reset, keeping its entity."""
        if self._get_entry().state is not ConfigEntryState.LOADED:
            return self.async_abort(reason="entry_not_loaded")
        subentry = self._get_reconfigure_subentry()
        errors = {}
        if user_input is not None:
            if subentry.data[CONF_ADDRESS] in self._unprovisioned_lamps():
                self._address = subentry.data[CONF_ADDRESS]
                return await self.async_step_provision()
            errors["base"] = "not_reset"
        return self.async_show_form(
            step_id="reconfigure",
            errors=errors,
            description_placeholders={"name": subentry.title},
        )

    async def async_step_provision(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Pair and configure the lamp, showing progress."""
        if self._task is None:
            self._task = self.hass.async_create_task(self._async_provision())
        if not self._task.done():
            return self.async_show_progress(
                step_id="provision", progress_action="provision", progress_task=self._task
            )
        try:
            self._lamp = self._task.result()
        except (HomeAssistantError, MeshError, BleakError, TimeoutError, ValueError) as err:
            self._error = str(err) or type(err).__name__
            return self.async_show_progress_done(next_step_id="failed")
        return self.async_show_progress_done(next_step_id="finish")

    async def async_step_finish(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Store the paired lamp: new subentry, or update of the existing one."""
        assert self._lamp is not None
        data = self._lamp.as_subentry_data()
        if self.source == "reconfigure":
            return self.async_update_and_abort(
                self._get_entry(), self._get_reconfigure_subentry(), data=data
            )
        return self.async_create_entry(title=self._name, data=data, unique_id=self._lamp.address)

    async def async_step_failed(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Report the pairing failure."""
        return self.async_abort(
            reason="provisioning_failed", description_placeholders={"error": self._error}
        )

    async def _async_provision(self) -> ProvisionedLamp:
        entry = cast(LedvanceMeshConfigEntry, self._get_entry())
        return await entry.runtime_data.async_add_lamp(self._address)

    def _unprovisioned_lamps(self) -> dict[str, str]:
        """Reset lamps in range (they advertise the provisioning service)."""
        now = monotonic_time_coarse()
        return {
            info.address: f"{info.name} ({info.address}, {info.rssi} dBm)"
            for info in bluetooth.async_discovered_service_info(self.hass, connectable=True)
            if GattService.PROVISIONING.uuid in info.service_uuids
            and now - info.time <= ADVERTISEMENT_MAX_AGE
        }
