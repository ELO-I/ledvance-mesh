"""Light entity of a Ledvance / Sylvania SMART+ Bluetooth Mesh lamp."""

from __future__ import annotations

from typing import Any

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_TEMP_KELVIN,
    ATTR_HS_COLOR,
    ATTR_TRANSITION,
    ColorMode,
    LightEntity,
    LightEntityFeature,
)
from homeassistant.config_entries import ConfigSubentry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_ADDRESS, DOMAIN
from .coordinator import LedvanceMeshConfigEntry, LedvanceMeshCoordinator
from .lamp import LampColorMode, LampCommand, LampState

_COLOR_MODES = {LampColorMode.HS: ColorMode.HS, LampColorMode.COLOR_TEMP: ColorMode.COLOR_TEMP}

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: LedvanceMeshConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create one entity per lamp (each lamp is a subentry)."""
    coordinator = entry.runtime_data
    for subentry_id, subentry in entry.subentries.items():
        if subentry_id in coordinator.lamps:
            async_add_entities(
                [LedvanceMeshLight(coordinator, subentry)], config_subentry_id=subentry_id
            )


class LedvanceMeshLight(CoordinatorEntity[LedvanceMeshCoordinator], LightEntity):
    """A colour and tunable white lamp."""

    _attr_has_entity_name = True
    _attr_name = None
    _attr_supported_color_modes = {ColorMode.HS, ColorMode.COLOR_TEMP}
    _attr_supported_features = LightEntityFeature.TRANSITION

    def __init__(self, coordinator: LedvanceMeshCoordinator, subentry: ConfigSubentry) -> None:
        super().__init__(coordinator)
        self._subentry_id = subentry.subentry_id
        profile = coordinator.lamps[subentry.subentry_id].profile
        address = subentry.data[CONF_ADDRESS]
        self._attr_unique_id = subentry.subentry_id
        self._attr_min_color_temp_kelvin = profile.min_kelvin
        self._attr_max_color_temp_kelvin = profile.max_kelvin
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, address)},
            connections={(CONNECTION_BLUETOOTH, address)},
            name=subentry.title,
            manufacturer="Ledvance / Sylvania",
            model=profile.model,
        )

    @property
    def _state(self) -> LampState | None:
        return self.coordinator.data.get(self._subentry_id)

    @property
    def available(self) -> bool:
        """The lamp is available as long as it shows signs of life."""
        return self._state is not None

    @property
    def is_on(self) -> bool | None:
        """True if the lamp is on."""
        return self._state.on if self._state else None

    @property
    def brightness(self) -> int | None:
        """Brightness (1-255)."""
        return self._state.brightness if self._state else None

    @property
    def color_mode(self) -> ColorMode | None:
        """Active mode: colour or white."""
        return _COLOR_MODES[self._state.color_mode] if self._state else None

    @property
    def hs_color(self) -> tuple[float, float] | None:
        """Hue (°) and saturation (%)."""
        return self._state.hs_color if self._state else None

    @property
    def color_temp_kelvin(self) -> int | None:
        """White temperature, in kelvin."""
        return self._state.color_temp_kelvin if self._state else None

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the lamp on, with the requested settings."""
        await self.coordinator.async_apply(
            self._subentry_id,
            LampCommand(
                on=True,
                brightness=kwargs.get(ATTR_BRIGHTNESS),
                hs_color=kwargs.get(ATTR_HS_COLOR),
                color_temp_kelvin=kwargs.get(ATTR_COLOR_TEMP_KELVIN),
                transition=kwargs.get(ATTR_TRANSITION),
            ),
        )

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the lamp off."""
        await self.coordinator.async_apply(
            self._subentry_id, LampCommand(on=False, transition=kwargs.get(ATTR_TRANSITION))
        )
