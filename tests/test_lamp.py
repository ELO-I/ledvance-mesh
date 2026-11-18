"""Lamp logic, against a stateful fake node (mesh encryption included)."""

import struct

import pytest

from custom_components.ledvance_mesh.lamp import Lamp, LampColorMode, LampCommand, LampState
from custom_components.ledvance_mesh.mesh import CompositionData, messages as m
from custom_components.ledvance_mesh.mesh.messages import Opcode
from custom_components.ledvance_mesh.profile import SYLVANIA_SMART_PLUS_COLOR as PROFILE
from tests.fake_lamp import KEYS, SYLVANIA_COMPOSITION, FakeLamp

WHITE = LampState(
    on=True, brightness=255, color_mode=LampColorMode.COLOR_TEMP, hs_color=(0.0, 0.0),
    color_temp_kelvin=2700,
)  # fmt: skip


@pytest.fixture
def node() -> FakeLamp:
    return FakeLamp()


@pytest.fixture
def lamp(node: FakeLamp) -> Lamp:
    return Lamp(node.unicast, 4, PROFILE)


async def test_read_translates_native_units(node: FakeLamp, lamp: Lamp) -> None:
    node.on, node.lightness, node.hue, node.saturation, node.temperature = True, 25, 120, 80, 4000
    state = await lamp.read(node.client(), LampColorMode.HS)
    assert state == LampState(True, 128, LampColorMode.HS, (120.0, 80.0), 4000)


async def test_read_of_a_lamp_that_is_off_keeps_the_brightness_it_will_come_back_to(
    node: FakeLamp, lamp: Lamp
) -> None:
    node.on, node.lightness = False, 25
    state = await lamp.read(node.client(), LampColorMode.COLOR_TEMP)
    assert state.on is False and state.brightness == 128
    # Picking a colour while off turns it back on at this brightness, not at the minimum.
    await lamp.apply(node.client(), LampCommand(hs_color=(30.0, 90.0)), state)
    assert node.on and node.lightness == 25


async def test_turn_off_and_on(node: FakeLamp, lamp: Lamp) -> None:
    client = node.client()
    node.on = True
    off = await lamp.apply(client, LampCommand(on=False), WHITE)
    assert off.on is False and node.on is False and off.brightness == WHITE.brightness
    on = await lamp.apply(client, LampCommand(), off)
    assert on.on is True and node.on is True


async def test_brightness_is_converted_and_clamped_to_the_bulb_range(
    node: FakeLamp, lamp: Lamp
) -> None:
    client = node.client()
    state = await lamp.apply(client, LampCommand(brightness=255), WHITE)
    assert node.lightness == 50 and state.brightness == 255
    state = await lamp.apply(client, LampCommand(brightness=1), state)
    assert node.lightness == 1 and state.brightness == 5  # 1/50 of the scale, rounded


async def test_colour_and_white_use_native_units_and_switch_mode(
    node: FakeLamp, lamp: Lamp
) -> None:
    client = node.client()
    state = await lamp.apply(client, LampCommand(hs_color=(400.0, 140.0), brightness=128), WHITE)
    assert (node.hue, node.saturation, node.lightness) == (360, 100, 25)  # capped, not wrapped
    assert state.color_mode is LampColorMode.HS and state.hs_color == (360.0, 100.0)
    dimmed = await lamp.apply(client, LampCommand(brightness=51), state)
    assert dimmed.color_mode is LampColorMode.HS and node.lightness == 10
    white = await lamp.apply(client, LampCommand(color_temp_kelvin=12000), dimmed)
    assert node.temperature == 6500 and white.color_temp_kelvin == 6500
    assert white.color_mode is LampColorMode.COLOR_TEMP and white.hs_color == (360.0, 100.0)


async def test_transition_is_sent_as_mesh_transition_time(node: FakeLamp, lamp: Lamp) -> None:
    await lamp.apply(node.client(), LampCommand(brightness=100, transition=5), WHITE)
    opcode, params = node.received[-1]
    assert opcode == Opcode.LIGHTNESS_SET
    assert params[3:5] == bytes([m.encode_transition_time(5), 0])
    await lamp.apply(node.client(), LampCommand(brightness=100), WHITE)
    assert len(node.received[-1][1]) == 3  # no transition: lightness (2) + tid (1)


async def test_configure_binds_every_model_and_sets_up_publication(
    node: FakeLamp, lamp: Lamp
) -> None:
    await lamp.configure(node.client(), KEYS, publish_address=0xC000, heartbeat=60)
    binds = [params for opcode, params in node.received if opcode == Opcode.CONFIG_MODEL_APP_BIND]
    assert node.received[0][0] == Opcode.CONFIG_APPKEY_ADD
    assert {int.from_bytes(p[:2], "little") for p in binds} == {node.unicast + i for i in range(4)}
    publications = {
        struct.unpack("<H", p[9:11])[0]: (struct.unpack("<H", p[2:4])[0], p[7])
        for opcode, p in node.received
        if opcode == Opcode.CONFIG_MODEL_PUBLICATION_SET
    }
    # All publish to the group; only the lightness model also publishes every 60 s.
    assert publications == {
        m.Model.LIGHTNESS_SERVER: (0xC000, m.encode_transition_time(60)),
        m.Model.CTL_SERVER: (0xC000, 0),
        m.Model.HSL_SERVER: (0xC000, 0),
    }


def test_published_status_updates_the_state_and_reveals_the_mode(lamp: Lamp) -> None:
    def hsl(lightness: int, hue: int, saturation: int) -> bytes:
        return struct.pack("<HHH", lightness, hue, saturation)

    def ctl(lightness: int, temperature: int) -> bytes:
        return struct.pack("<HH", lightness, temperature)

    # Turning off: the lamp publishes everything with zero lightness; nothing else changes.
    off = lamp.after_status(WHITE, Opcode.HSL_STATUS, hsl(0, 0, 0))
    assert off == LampState(False, 255, LampColorMode.COLOR_TEMP, (0.0, 0.0), 2700)
    # Brightness change: both value sets are published, the mode does not move.
    dimmed = lamp.after_status(WHITE, Opcode.CTL_STATUS, ctl(10, 2700))
    assert dimmed is not None and dimmed.brightness == 51
    assert dimmed.color_mode is LampColorMode.COLOR_TEMP
    # A changing hue: the lamp switched to colour. A temperature: to white.
    coloured = lamp.after_status(dimmed, Opcode.HSL_STATUS, hsl(10, 200, 80))
    assert coloured is not None and coloured.color_mode is LampColorMode.HS
    assert coloured.hs_color == (200.0, 80.0)
    white = lamp.after_status(coloured, Opcode.CTL_STATUS, ctl(10, 4000))
    assert white is not None and white.color_mode is LampColorMode.COLOR_TEMP
    assert white.color_temp_kelvin == 4000 and white.hs_color == (200.0, 80.0)
    # During a transition, the target is what counts.
    fading = lamp.after_status(WHITE, Opcode.LIGHTNESS_STATUS, b"\x32\x00\x00\x00\x0a")
    assert fading is not None and fading.on is False
    assert lamp.after_status(WHITE, Opcode.CONFIG_APPKEY_STATUS, b"\x00") is None


def test_supported_models_are_checked_against_the_composition_data() -> None:
    composition = CompositionData.parse(SYLVANIA_COMPOSITION)
    assert PROFILE.missing_models(composition) == []
    white_only = CompositionData(0x052E, 2, 0, (composition.elements[0] - {0x1307}, frozenset()))
    assert PROFILE.missing_models(white_only) == [
        "HSL_SERVER (element 0)",
        "CTL_TEMPERATURE_SERVER (element 1)",
        "HSL_HUE_SERVER (element 2)",
        "HSL_SATURATION_SERVER (element 3)",
    ]
