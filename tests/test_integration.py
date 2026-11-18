"""Integration in Home Assistant, with a fake lamp instead of Bluetooth."""

import struct
from types import SimpleNamespace
from unittest.mock import patch

from bluetooth_data_tools import monotonic_time_coarse
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    MockModule,
    mock_integration,
)

from custom_components.ledvance_mesh.const import DOMAIN, STATUS_GROUP_ADDRESS
from custom_components.ledvance_mesh.mesh import MeshClient, ProvisioningResult
from custom_components.ledvance_mesh.mesh.messages import Opcode
from homeassistant.config_entries import ConfigSubentryData
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from tests.fake_lamp import DEVICE_KEY, KEYS, FakeLamp

FLOW = "custom_components.ledvance_mesh.config_flow"
COORDINATOR = "custom_components.ledvance_mesh.coordinator"
MAC = "F0:D1:B8:00:00:01"
NEW = "F0:D1:B8:00:00:02"
ENTITY = "light.living_room"


class FakeGateway:
    """Replaces MeshGateway: a single lamp, linked through a fake bearer."""

    lamp: FakeLamp

    def __init__(self, hass, network, *, on_message, on_beacon, on_connected) -> None:
        self._network, self._on_message, self._on_connected = network, on_message, on_connected
        self.bearer = self.lamp.bearer()
        self._client: MeshClient | None = None
        self.connected = False
        self.proxy_address = MAC
        type(self).instance = self

    def register_node(self, unicast, address, device_key) -> None:
        self._network.device_keys[unicast] = device_key

    def unregister_node(self, unicast, element_count) -> None:
        self._network.forget_node(unicast, element_count)

    async def async_run(self) -> None:
        await self.async_get_client()

    async def async_get_client(self) -> MeshClient:
        if self._client is None:
            self._client = MeshClient(self.bearer, self._network, on_message=self._on_message)
            self.connected = True
            self._on_connected()
        return self._client

    async def async_stop(self) -> None:
        self.connected = False

    async def async_provision(self, address, allocate_address) -> ProvisioningResult:
        unicast = allocate_address(4)
        self.bearer.node_address = unicast  # the fake lamp takes the address it is given
        return ProvisioningResult(DEVICE_KEY, 4, unicast)


@pytest.fixture(autouse=True)
def _environment(hass: HomeAssistant, enable_custom_integrations: None):
    mock_integration(hass, MockModule("bluetooth"))
    FakeGateway.lamp = FakeLamp()
    with (
        patch(f"{COORDINATOR}.bluetooth.async_register_advertisement_callback"),
        patch(f"{COORDINATOR}.MeshGateway", FakeGateway),
        patch(f"{COORDINATOR}._RESTART_DELAY", 0),
    ):
        yield


def _entry(*, setup_version: int | None, legacy: bool = False) -> MockConfigEntry:
    lamp = {"address": MAC, "unicast": 0x0010, "element_count": 4, "device_key": DEVICE_KEY.hex()}
    if setup_version is not None:
        lamp["setup_version"] = setup_version
    data = {"net_key": KEYS.net_key.hex(), "app_key": KEYS.app_key.hex(), "local_address": 1}
    if legacy:
        data["iv_index"] = 0
    return MockConfigEntry(
        domain=DOMAIN,
        data=data,
        version=1,
        minor_version=1 if legacy else 2,
        subentries_data=[
            ConfigSubentryData(data=lamp, subentry_type="light", title="Living room", unique_id=MAC)
        ],
    )


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> FakeLamp:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    return FakeGateway.lamp


async def test_state_is_read_at_connection_then_pushed_by_the_lamp(hass: HomeAssistant) -> None:
    FakeGateway.lamp.on, FakeGateway.lamp.lightness, FakeGateway.lamp.temperature = True, 25, 4000
    lamp = await _setup(hass, _entry(setup_version=2))
    state = hass.states.get(ENTITY)
    assert state.state == "on" and state.attributes["brightness"] == 128
    assert state.attributes["color_mode"] == "color_temp"

    bearer = FakeGateway.instance.bearer
    bearer.transmit(Opcode.HSL_STATUS, struct.pack("<HHH", 10, 200, 80), dst=STATUS_GROUP_ADDRESS)
    await hass.async_block_till_done()
    state = hass.states.get(ENTITY)
    assert state.attributes["color_mode"] == "hs" and state.attributes["hs_color"] == (200, 80)
    assert state.attributes["brightness"] == 51

    await hass.services.async_call(
        "light", "turn_on", {"entity_id": ENTITY, "color_temp_kelvin": 3000}, blocking=True
    )
    assert lamp.temperature == 3000 and hass.states.get(ENTITY).attributes["color_mode"] == (
        "color_temp"
    )
    await hass.services.async_call("light", "turn_off", {"entity_id": ENTITY}, blocking=True)
    assert lamp.on is False and hass.states.get(ENTITY).state == "off"


async def test_silent_lamp_becomes_unavailable_and_comes_back(hass: HomeAssistant) -> None:
    FakeGateway.lamp.on = True
    await _setup(hass, _entry(setup_version=2))
    coordinator = hass.config_entries.async_entries(DOMAIN)[0].runtime_data
    subentry_id = next(iter(coordinator.lamps))
    FakeGateway.instance.proxy_address = None  # another lamp is the proxy
    coordinator.presence._last_message[subentry_id] -= 1000  # no news for a long time
    await coordinator._async_check_presence(None)
    assert hass.states.get(ENTITY).state == "unavailable"

    bearer = FakeGateway.instance.bearer
    bearer.transmit(Opcode.LIGHTNESS_STATUS, b"\x32\x00", dst=STATUS_GROUP_ADDRESS)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert hass.states.get(ENTITY).state == "on"


async def test_lamp_from_an_older_version_is_migrated_and_reconfigured(
    hass: HomeAssistant,
) -> None:
    entry = _entry(setup_version=None, legacy=True)
    lamp = await _setup(hass, entry)
    assert "iv_index" not in entry.data and entry.minor_version == 2
    await entry.runtime_data._async_maintain(None)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert Opcode.CONFIG_MODEL_PUBLICATION_SET in [opcode for opcode, _ in lamp.received]
    subentry = next(iter(entry.subentries.values()))
    assert subentry.data["setup_version"] == 2
    assert entry.state.value == "loaded" and hass.states.get(ENTITY).state != "unavailable"


def _advertising(address: str) -> list[SimpleNamespace]:
    return [
        SimpleNamespace(
            address=address,
            name="SYLVANIA A19",
            rssi=-60,
            time=monotonic_time_coarse(),
            service_uuids=["00001827-0000-1000-8000-00805f9b34fb"],
        )
    ]


async def _finish_progress(hass: HomeAssistant, result: dict) -> dict:
    assert result["type"] is FlowResultType.SHOW_PROGRESS
    await hass.async_block_till_done(wait_background_tasks=True)
    return await hass.config_entries.subentries.async_configure(result["flow_id"])


async def test_add_and_re_pair_flows(hass: HomeAssistant) -> None:
    entry = _entry(setup_version=2)
    await _setup(hass, entry)
    discovered = f"{FLOW}.bluetooth.async_discovered_service_info"
    new_mac = NEW
    flows = hass.config_entries.subentries

    # Adding: the known lamp is not offered, the new one is paired and configured.
    with patch(discovered, return_value=_advertising(MAC) + _advertising(new_mac)):
        result = await flows.async_init((entry.entry_id, "light"), context={"source": "user"})
        assert result["type"] is FlowResultType.FORM and result["step_id"] == "user"
        choices = result["data_schema"].schema["address"].container
        assert list(choices) == [new_mac]
        result = await flows.async_configure(
            result["flow_id"], {"address": new_mac, "name": "Office"}
        )
        result = await _finish_progress(hass, result)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done(wait_background_tasks=True)
    added = next(s for s in entry.subentries.values() if s.title == "Office")
    assert added.data["unicast"] == 0x0010 and added.data["setup_version"] == 2

    # Re-pairing: refused until the lamp is reset, then it keeps its entity.
    living_room = next(s for s in entry.subentries.values() if s.title == "Living room")
    context = {"source": "reconfigure", "subentry_id": living_room.subentry_id}
    with patch(discovered, return_value=[]):
        result = await flows.async_init((entry.entry_id, "light"), context=context)
        result = await flows.async_configure(result["flow_id"], {})
        assert result["errors"] == {"base": "not_reset"}
    with patch(discovered, return_value=_advertising(MAC)):
        result = await flows.async_configure(result["flow_id"], {})
        result = await _finish_progress(hass, result)
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "reconfigure_successful"
    await hass.async_block_till_done(wait_background_tasks=True)
    assert entry.subentries[living_room.subentry_id].data["unicast"] == 0x0014
    assert hass.states.get(ENTITY) is not None


async def test_unsupported_device_is_refused_and_reset(hass: HomeAssistant) -> None:
    entry = _entry(setup_version=2)
    lamp = await _setup(hass, entry)
    lamp.composition = lamp.composition[:10] + bytes([0, 0, 1, 0, 0x00, 0x10])  # OnOff only
    flows = hass.config_entries.subentries
    with patch(f"{FLOW}.bluetooth.async_discovered_service_info", return_value=_advertising(NEW)):
        result = await flows.async_init((entry.entry_id, "light"), context={"source": "user"})
        result = await flows.async_configure(result["flow_id"], {"address": NEW, "name": "Spot"})
        result = await _finish_progress(hass, result)
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "provisioning_failed"
    assert "unsupported device" in result["description_placeholders"]["error"]
    assert Opcode.CONFIG_NODE_RESET in [opcode for opcode, _ in lamp.received]
    assert len(entry.subentries) == 1
