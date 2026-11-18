"""Bulb model profile: native units, ranges and mesh models to configure.

Sylvania SMART+ bulbs do not use the 16-bit scales of the Bluetooth Mesh standard: hue is in
degrees (0-360), saturation in percent (0-100) and lightness goes from 1 to 50. Only brightness
and white temperature need a conversion.
"""

from __future__ import annotations

from dataclasses import dataclass

from .mesh import CompositionData
from .mesh.messages import Model

_HA_MAX_BRIGHTNESS = 255


@dataclass(frozen=True, slots=True)
class LampProfile:
    """Characteristics of a bulb model."""

    model: str
    min_brightness: int
    max_brightness: int
    min_kelvin: int
    max_kelvin: int
    # (element offset from the node address, model) to bind to the AppKey.
    bindings: tuple[tuple[int, Model], ...]
    # Models that publish their state on every change.
    publications: tuple[tuple[int, Model], ...]
    # Among them, the one that also publishes at a regular interval (the lamp's heartbeat).
    heartbeat: tuple[int, Model]

    def brightness_to_wire(self, brightness: int) -> int:
        """Convert a Home Assistant brightness (1-255) to the bulb value."""
        wire = round(brightness / _HA_MAX_BRIGHTNESS * self.max_brightness)
        return max(self.min_brightness, min(self.max_brightness, wire))

    def brightness_from_wire(self, wire: int) -> int:
        """Convert a non-zero bulb lightness to a Home Assistant brightness (1-255)."""
        brightness = round(wire / self.max_brightness * _HA_MAX_BRIGHTNESS)
        return max(1, min(_HA_MAX_BRIGHTNESS, brightness))

    def clamp_kelvin(self, kelvin: int) -> int:
        """Bring a white temperature back into the bulb range."""
        return max(self.min_kelvin, min(self.max_kelvin, kelvin))

    def missing_models(self, composition: CompositionData) -> list[str]:
        """Models this profile needs that the device does not implement (empty: supported)."""
        return [
            f"{model.name} (element {offset})"
            for offset, model in self.bindings
            if not composition.has_model(offset, model)
        ]


# Ranges checked on the bulb: it reports a lightness range of 1 to 50 (Light Lightness Range).
# For white it reports 1000-12000 K, but the output no longer changes below 2700 K (checked by
# eye). The 6500 K upper bound is the usual value for this kind of bulb; it was not checked.
SYLVANIA_SMART_PLUS_COLOR = LampProfile(
    model="SMART+ Bluetooth Full Color A19",
    min_brightness=1,
    max_brightness=50,
    min_kelvin=2700,
    max_kelvin=6500,
    bindings=(
        (0, Model.ONOFF_SERVER),
        (0, Model.LIGHTNESS_SERVER),
        (0, Model.CTL_SERVER),
        (0, Model.HSL_SERVER),
        (1, Model.CTL_TEMPERATURE_SERVER),
        (2, Model.HSL_HUE_SERVER),
        (3, Model.HSL_SATURATION_SERVER),
    ),
    publications=(
        (0, Model.LIGHTNESS_SERVER),
        (0, Model.CTL_SERVER),
        (0, Model.HSL_SERVER),
    ),
    heartbeat=(0, Model.LIGHTNESS_SERVER),
)
