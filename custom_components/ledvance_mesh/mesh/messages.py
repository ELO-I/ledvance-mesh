"""Access layer messages: opcodes, command encoding, status decoding.

Values handled here are wire values, as exchanged with the device. Converting them to user
units is the job of the device profile.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
import struct

from .errors import ConfigurationError, MeshError


class Opcode(IntEnum):
    """Opcodes used by the client."""

    CONFIG_APPKEY_ADD = 0x00
    CONFIG_COMPOSITION_DATA_STATUS = 0x02
    CONFIG_MODEL_PUBLICATION_SET = 0x03
    CONFIG_APPKEY_STATUS = 0x8003
    CONFIG_COMPOSITION_DATA_GET = 0x8008
    CONFIG_MODEL_PUBLICATION_STATUS = 0x8019
    CONFIG_MODEL_APP_BIND = 0x803D
    CONFIG_MODEL_APP_STATUS = 0x803E
    CONFIG_NODE_RESET = 0x8049
    CONFIG_NODE_RESET_STATUS = 0x804A

    ONOFF_SET = 0x8202
    ONOFF_STATUS = 0x8204

    LIGHTNESS_SET = 0x824C
    LIGHTNESS_STATUS = 0x824E
    LIGHTNESS_LAST_GET = 0x8253
    LIGHTNESS_LAST_STATUS = 0x8254

    CTL_GET = 0x825D
    CTL_SET = 0x825E
    CTL_STATUS = 0x8260

    HSL_GET = 0x826D
    HSL_SET = 0x8276
    HSL_STATUS = 0x8278


class Model(IntEnum):
    """SIG model identifiers."""

    ONOFF_SERVER = 0x1000
    LIGHTNESS_SERVER = 0x1300
    CTL_SERVER = 0x1303
    CTL_TEMPERATURE_SERVER = 0x1306
    HSL_SERVER = 0x1307
    HSL_HUE_SERVER = 0x130A
    HSL_SATURATION_SERVER = 0x130B


# --- Opcodes -------------------------------------------------------------------------------


def encode_opcode(opcode: int) -> bytes:
    """Encode an opcode on 1, 2 or 3 bytes."""
    if opcode <= 0x7E:
        return bytes([opcode])
    if opcode <= 0xBFFF:
        return opcode.to_bytes(2, "big")
    return opcode.to_bytes(3, "big")


def split_opcode(payload: bytes) -> tuple[int, bytes]:
    """Split an access message into (opcode, parameters)."""
    first = payload[0]
    if first & 0x80 == 0:
        return first, payload[1:]
    if first & 0xC0 == 0x80:
        return int.from_bytes(payload[:2], "big"), payload[2:]
    return int.from_bytes(payload[:3], "big"), payload[3:]


# --- Transition time -----------------------------------------------------------------------

_RESOLUTIONS_MS = (100, 1_000, 10_000, 600_000)
_MAX_STEPS = 62
_UNKNOWN_STEPS = 0x3F


def encode_transition_time(seconds: float) -> int:
    """Encode a duration as a Transition Time (6 bits of steps + 2 bits of resolution)."""
    milliseconds = round(seconds * 1000)
    for code, resolution in enumerate(_RESOLUTIONS_MS):
        steps = round(milliseconds / resolution)
        if steps <= _MAX_STEPS:
            return (code << 6) | steps
    return (len(_RESOLUTIONS_MS) - 1) << 6 | _MAX_STEPS


def _transition_fields(transition: float | None) -> bytes:
    """Optional Transition Time and Delay fields (zero delay)."""
    return b"" if transition is None else bytes([encode_transition_time(transition), 0])


def _message(opcode: Opcode, params: bytes = b"") -> bytes:
    return encode_opcode(opcode) + params


# --- Light commands ------------------------------------------------------------------------


def onoff_set(on: bool, tid: int, transition: float | None = None) -> bytes:
    """Generic OnOff Set (acknowledged)."""
    return _message(Opcode.ONOFF_SET, bytes([int(on), tid]) + _transition_fields(transition))


def lightness_set(lightness: int, tid: int, transition: float | None = None) -> bytes:
    """Light Lightness Set (acknowledged)."""
    params = struct.pack("<HB", lightness, tid) + _transition_fields(transition)
    return _message(Opcode.LIGHTNESS_SET, params)


def lightness_last_get() -> bytes:
    """Light Lightness Last Get: last non-zero lightness (useful while the light is off)."""
    return _message(Opcode.LIGHTNESS_LAST_GET)


def ctl_get() -> bytes:
    """Light CTL Get."""
    return _message(Opcode.CTL_GET)


def ctl_set(lightness: int, temperature: int, tid: int, transition: float | None = None) -> bytes:
    """Light CTL Set (acknowledged), zero Delta UV."""
    params = struct.pack("<HHhB", lightness, temperature, 0, tid) + _transition_fields(transition)
    return _message(Opcode.CTL_SET, params)


def hsl_get() -> bytes:
    """Light HSL Get."""
    return _message(Opcode.HSL_GET)


def hsl_set(
    lightness: int, hue: int, saturation: int, tid: int, transition: float | None = None
) -> bytes:
    """Light HSL Set (acknowledged)."""
    params = struct.pack("<HHHB", lightness, hue, saturation, tid) + _transition_fields(transition)
    return _message(Opcode.HSL_SET, params)


# --- Light statuses ------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OnOffStatus:
    """Generic OnOff Status. `target` is only present during a transition."""

    present: bool
    target: bool | None

    @property
    def value(self) -> bool:
        """Target state during a transition, current state otherwise."""
        return self.present if self.target is None else self.target

    @classmethod
    def parse(cls, params: bytes) -> OnOffStatus:
        """Decode the status parameters."""
        _require(params, 1, "OnOff Status")
        return cls(bool(params[0]), bool(params[1]) if len(params) >= 3 else None)


@dataclass(frozen=True, slots=True)
class LightnessStatus:
    """Light Lightness Status."""

    present: int
    target: int | None

    @property
    def value(self) -> int:
        """Target value during a transition, current value otherwise."""
        return self.present if self.target is None else self.target

    @classmethod
    def parse(cls, params: bytes) -> LightnessStatus:
        """Decode the status parameters."""
        _require(params, 2, "Lightness Status")
        return cls(
            int.from_bytes(params[0:2], "little"),
            int.from_bytes(params[2:4], "little") if len(params) >= 5 else None,
        )


def parse_lightness_last(params: bytes) -> int:
    """Decode a Light Lightness Last Status."""
    _require(params, 2, "Lightness Last Status")
    return int.from_bytes(params[0:2], "little")


@dataclass(frozen=True, slots=True)
class CtlStatus:
    """Light CTL Status (lightness and temperature)."""

    lightness: int
    temperature: int
    target_lightness: int | None
    target_temperature: int | None

    @property
    def settled_lightness(self) -> int:
        """Target lightness during a transition, current lightness otherwise."""
        return self.lightness if self.target_lightness is None else self.target_lightness

    @property
    def settled_temperature(self) -> int:
        """Target temperature during a transition, current temperature otherwise."""
        return self.temperature if self.target_temperature is None else self.target_temperature

    @classmethod
    def parse(cls, params: bytes) -> CtlStatus:
        """Decode the status parameters."""
        _require(params, 4, "CTL Status")
        has_target = len(params) >= 9
        return cls(
            int.from_bytes(params[0:2], "little"),
            int.from_bytes(params[2:4], "little"),
            int.from_bytes(params[4:6], "little") if has_target else None,
            int.from_bytes(params[6:8], "little") if has_target else None,
        )


@dataclass(frozen=True, slots=True)
class HslStatus:
    """Light HSL Status (lightness, hue, saturation)."""

    lightness: int
    hue: int
    saturation: int

    @classmethod
    def parse(cls, params: bytes) -> HslStatus:
        """Decode the status parameters."""
        _require(params, 6, "HSL Status")
        lightness, hue, saturation = struct.unpack("<HHH", params[:6])
        return cls(lightness, hue, saturation)


def _require(params: bytes, minimum: int, name: str) -> None:
    if len(params) < minimum:
        raise MeshError(f"{name} too short: {params.hex()}")


# --- Configuration -------------------------------------------------------------------------


def config_appkey_add(net_index: int, app_index: int, app_key: bytes) -> bytes:
    """Config AppKey Add."""
    indexes = bytes(
        [net_index & 0xFF, ((app_index & 0x0F) << 4) | (net_index >> 8), app_index >> 4]
    )
    return _message(Opcode.CONFIG_APPKEY_ADD, indexes + app_key)


def config_model_app_bind(element_address: int, app_index: int, model_id: int) -> bytes:
    """Config Model App Bind for a SIG model."""
    return _message(
        Opcode.CONFIG_MODEL_APP_BIND, struct.pack("<HHH", element_address, app_index, model_id)
    )


def config_model_publication_set(
    element_address: int,
    model_id: int,
    *,
    publish_address: int,
    app_index: int,
    ttl: int,
    period: float,
) -> bytes:
    """Config Model Publication Set for a SIG model.

    The model publishes its state to `publish_address` on every change, and also every `period`
    seconds when it is not zero. No retransmission: the next state corrects a lost one.
    """
    period_field = encode_transition_time(period)  # same encoding: steps + resolution
    params = struct.pack(
        "<HHHBBBH", element_address, publish_address, app_index, ttl, period_field, 0, model_id
    )
    return _message(Opcode.CONFIG_MODEL_PUBLICATION_SET, params)


def config_composition_data_get() -> bytes:
    """Config Composition Data Get (page 0): elements and models of the node."""
    return _message(Opcode.CONFIG_COMPOSITION_DATA_GET, b"\x00")


@dataclass(frozen=True, slots=True)
class CompositionData:
    """Page 0 of a node's composition data: the SIG models of each element."""

    company_id: int
    product_id: int
    version_id: int
    elements: tuple[frozenset[int], ...]

    def has_model(self, element: int, model_id: int) -> bool:
        """True if element number `element` (0 = primary) implements `model_id`."""
        return element < len(self.elements) and model_id in self.elements[element]

    @classmethod
    def parse(cls, data: bytes) -> CompositionData:
        """Decode page 0 (without the page number)."""
        _require(data, 10, "Composition Data")
        company_id, product_id, version_id = struct.unpack("<HHH", data[:6])
        elements = []
        offset = 10
        while offset + 4 <= len(data):
            sig_count, vendor_count = data[offset + 2], data[offset + 3]
            offset += 4
            end = offset + 2 * sig_count
            _require(data[offset:end], 2 * sig_count, "Composition Data element")
            elements.append(frozenset(struct.unpack(f"<{sig_count}H", data[offset:end])))
            offset = end + 4 * vendor_count
        return cls(company_id, product_id, version_id, tuple(elements))


def config_node_reset() -> bytes:
    """Config Node Reset: the node forgets the network and becomes unprovisioned again."""
    return _message(Opcode.CONFIG_NODE_RESET)


def check_config_status(params: bytes, action: str) -> None:
    """Raise ConfigurationError if a configuration status is not a success."""
    if params and params[0] != 0:
        raise ConfigurationError(f"{action} refused by the node (status {params[0]:#04x})")
