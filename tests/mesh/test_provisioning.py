"""Provisioning: the provisioner against a fake device."""

import pytest

from custom_components.ledvance_mesh.mesh import IvState, Provisioner, ProvisioningError
from tests.mesh.fakes import FakeProvisioneeBearer

NET_KEY = bytes(range(16))
IV = IvState(0)


async def test_device_is_provisioned_with_our_network_key_and_address() -> None:
    device = FakeProvisioneeBearer()
    allocations = []

    def allocate(element_count: int) -> int:
        allocations.append(element_count)
        return 0x0010

    result = await Provisioner(device).provision(NET_KEY, IV, allocate)
    assert allocations == [4]  # allocation happens once the element count is known
    assert device.provisioned == (NET_KEY, 0, 0x0010)
    assert (result.element_count, result.unicast_address) == (4, 0x0010)
    assert result.device_key == device.device_key  # both sides derive the same device key


async def test_device_joining_during_an_iv_update_is_told_so() -> None:
    device = FakeProvisioneeBearer()
    await Provisioner(device).provision(NET_KEY, IvState(5, True), lambda _count: 0x0010)
    assert device.provisioned == (NET_KEY, 5, 0x0010) and device.flags == 0x02


async def test_refusal_from_the_device_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    device = FakeProvisioneeBearer()

    async def refuse(pdu_type, payload):  # type: ignore[no-untyped-def]
        device._reply(0x09, b"\x01")

    monkeypatch.setattr(device, "send", refuse)
    with pytest.raises(ProvisioningError, match="refused"):
        await Provisioner(device).provision(NET_KEY, IV, lambda _count: 0x0010)


async def test_silent_device_times_out(monkeypatch: pytest.MonkeyPatch) -> None:
    device = FakeProvisioneeBearer()

    async def silent(pdu_type, payload):  # type: ignore[no-untyped-def]
        return None

    monkeypatch.setattr(device, "send", silent)
    with pytest.raises(ProvisioningError, match="no answer"):
        await Provisioner(device, step_timeout=0.05).provision(NET_KEY, IV, lambda _count: 0x0010)
