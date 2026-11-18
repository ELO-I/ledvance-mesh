"""A lamp of the network: configuration, state reading, commands, published states.

This module does not depend on Home Assistant.

The bulb keeps two sets of values, colour (hue, saturation) and white (temperature), and does not
say which one is shown. The active mode is inferred from what changes: a colour command only
changes hue and saturation, a white command only the temperature. When off, the bulb reports a
zero lightness.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum

from .mesh import (
    MeshClient,
    NetworkKeys,
    add_app_key,
    bind_model,
    messages as msg,
    set_publication,
)
from .mesh.client import DEFAULT_TTL, REQUEST_ATTEMPTS, REQUEST_TIMEOUT
from .mesh.messages import Opcode
from .profile import LampProfile

# Version of the configuration applied by `Lamp.configure`: increment it when it changes, so that
# lamps already added are configured again.
#   1: AppKey and model binding; 2: state publication.
SETUP_VERSION = 2


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


class LampColorMode(StrEnum):
    """Active lighting mode."""

    COLOR_TEMP = "color_temp"
    HS = "hs"


@dataclass(frozen=True, slots=True)
class LampState:
    """State of a lamp, in Home Assistant units (brightness 1-255, hue °, saturation %)."""

    on: bool
    brightness: int  # when off: the brightness it will come back to
    color_mode: LampColorMode
    hs_color: tuple[float, float]
    color_temp_kelvin: int


@dataclass(frozen=True, slots=True)
class LampCommand:
    """What is asked of a lamp. Missing fields stay unchanged."""

    on: bool = True
    brightness: int | None = None
    hs_color: tuple[float, float] | None = None
    color_temp_kelvin: int | None = None
    transition: float | None = None


class Lamp:
    """A bulb identified by its mesh address."""

    def __init__(self, unicast: int, element_count: int, profile: LampProfile) -> None:
        self.unicast = unicast
        self.element_count = element_count
        self.profile = profile

    @property
    def addresses(self) -> range:
        """Addresses of the lamp's elements."""
        return range(self.unicast, self.unicast + self.element_count)

    async def configure(
        self, client: MeshClient, keys: NetworkKeys, *, publish_address: int, heartbeat: float
    ) -> None:
        """Apply the whole configuration (version `SETUP_VERSION`); can be replayed.

        The lamp gets the AppKey, binds it to its light models, then publishes its state to
        `publish_address` on every change, and every `heartbeat` seconds as a sign of life.
        """
        await add_app_key(client, self.unicast, keys)
        for offset, model in self.profile.bindings:
            if offset < self.element_count:
                await bind_model(client, self.unicast, self.unicast + offset, model)
        for offset, model in self.profile.publications:
            await set_publication(
                client,
                self.unicast,
                self.unicast + offset,
                model,
                publish_address=publish_address,
                ttl=DEFAULT_TTL,
                period=heartbeat if (offset, model) == self.profile.heartbeat else 0,
            )

    async def read(
        self,
        client: MeshClient,
        color_mode: LampColorMode,
        *,
        attempts: int = REQUEST_ATTEMPTS,
        timeout: float = REQUEST_TIMEOUT,
    ) -> LampState:
        """Read the full state; `color_mode` is the last known mode, which the lamp does not tell.

        `attempts` and `timeout` set how patient to be with a lamp that does not answer.
        """

        async def ask(payload: bytes, response: Opcode) -> bytes:
            return await client.request(
                self.unicast, payload, response, attempts=attempts, timeout=timeout
            )

        hsl = msg.HslStatus.parse(await ask(msg.hsl_get(), Opcode.HSL_STATUS))
        ctl = msg.CtlStatus.parse(await ask(msg.ctl_get(), Opcode.CTL_STATUS))
        lightness = hsl.lightness
        if lightness == 0:
            lightness = msg.parse_lightness_last(
                await ask(msg.lightness_last_get(), Opcode.LIGHTNESS_LAST_STATUS)
            )
        return LampState(
            on=hsl.lightness > 0,
            brightness=self.profile.brightness_from_wire(lightness),
            color_mode=color_mode,
            hs_color=(float(hsl.hue), float(hsl.saturation)),
            color_temp_kelvin=ctl.settled_temperature,
        )

    async def apply(
        self, client: MeshClient, command: LampCommand, current: LampState
    ) -> LampState:
        """Run `command` and return the resulting state, once any transition is over."""
        tid = client.next_tid()
        transition = command.transition

        if not command.on:
            params = await self._ask(
                client, msg.onoff_set(False, tid, transition), Opcode.ONOFF_STATUS
            )
            return replace(current, on=msg.OnOffStatus.parse(params).value)

        brightness = command.brightness if command.brightness is not None else current.brightness
        lightness = self.profile.brightness_to_wire(brightness)

        if command.hs_color is not None:
            hue = _clamp(round(command.hs_color[0]), 0, 360)
            saturation = _clamp(round(command.hs_color[1]), 0, 100)
            await self._ask(
                client, msg.hsl_set(lightness, hue, saturation, tid, transition), Opcode.HSL_STATUS
            )
            # The HSL status only gives the values of the moment, not the target of a
            # transition: keep the requested values, which the lamp has just acknowledged.
            return LampState(
                on=True,
                brightness=self.profile.brightness_from_wire(lightness),
                color_mode=LampColorMode.HS,
                hs_color=(float(hue), float(saturation)),
                color_temp_kelvin=current.color_temp_kelvin,
            )

        if command.color_temp_kelvin is not None:
            kelvin = self.profile.clamp_kelvin(command.color_temp_kelvin)
            params = await self._ask(
                client, msg.ctl_set(lightness, kelvin, tid, transition), Opcode.CTL_STATUS
            )
            ctl = msg.CtlStatus.parse(params)
            return replace(
                self._with_lightness(current, ctl.settled_lightness),
                color_mode=LampColorMode.COLOR_TEMP,
                color_temp_kelvin=ctl.settled_temperature,
            )

        if command.brightness is not None:
            params = await self._ask(
                client, msg.lightness_set(lightness, tid, transition), Opcode.LIGHTNESS_STATUS
            )
            return self._with_lightness(current, msg.LightnessStatus.parse(params).value)

        params = await self._ask(client, msg.onoff_set(True, tid, transition), Opcode.ONOFF_STATUS)
        return replace(current, on=msg.OnOffStatus.parse(params).value)

    def after_status(self, state: LampState, opcode: int, params: bytes) -> LampState | None:
        """State after a status published by the lamp; None if the message is not a status."""
        if opcode == Opcode.LIGHTNESS_STATUS:
            return self._with_lightness(state, msg.LightnessStatus.parse(params).value)
        if opcode == Opcode.CTL_STATUS:
            ctl = msg.CtlStatus.parse(params)
            state = self._with_lightness(state, ctl.settled_lightness)
            if ctl.settled_temperature == state.color_temp_kelvin:
                return state
            return replace(
                state,
                color_mode=LampColorMode.COLOR_TEMP,
                color_temp_kelvin=ctl.settled_temperature,
            )
        if opcode == Opcode.HSL_STATUS:
            hsl = msg.HslStatus.parse(params)
            state = self._with_lightness(state, hsl.lightness)
            hs_color = (float(hsl.hue), float(hsl.saturation))
            if hs_color == state.hs_color:
                return state
            return replace(state, color_mode=LampColorMode.HS, hs_color=hs_color)
        if opcode == Opcode.ONOFF_STATUS:
            return replace(state, on=msg.OnOffStatus.parse(params).value)
        return None

    def _with_lightness(self, state: LampState, lightness: int) -> LampState:
        """A zero lightness means "off": the last brightness is kept."""
        if lightness == 0:
            return replace(state, on=False)
        return replace(state, on=True, brightness=self.profile.brightness_from_wire(lightness))

    async def _ask(self, client: MeshClient, payload: bytes, response: Opcode) -> bytes:
        return await client.request(self.unicast, payload, response)
