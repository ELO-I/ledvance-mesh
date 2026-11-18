"""Mesh client: requests/answers, segments, received publications, IV index."""

import asyncio

import pytest

from custom_components.ledvance_mesh.mesh import (
    AccessMessage,
    ConfigurationError,
    IvState,
    MeshClient,
    MeshNetwork,
    MeshTimeoutError,
    NetworkKeys,
    SecureNetworkBeacon,
    add_app_key,
    get_composition,
    messages as m,
    set_publication,
)
from custom_components.ledvance_mesh.mesh.proxy import ProxyPduType
from tests.fake_lamp import SYLVANIA_COMPOSITION
from tests.mesh.fakes import Counter, FakeNodeBearer

KEYS = NetworkKeys(bytes(range(16)), bytes(range(16, 32)))
DEVICE_KEY = bytes(range(100, 116))
CLIENT, NODE, GROUP = 0x0001, 0x0010, 0xC000


class Setup:
    def __init__(self, handler, iv: IvState | None = None) -> None:  # type: ignore[no-untyped-def]
        iv = iv or IvState(0)
        self.node = FakeNodeBearer(
            KEYS,
            node_address=NODE,
            client_address=CLIENT,
            device_key=DEVICE_KEY,
            handler=handler,
            iv=iv,
        )
        self.network = MeshNetwork(KEYS, CLIENT, Counter(1), iv, {NODE: DEVICE_KEY}, {GROUP})
        self.messages: list[AccessMessage] = []
        self.beacons: list[SecureNetworkBeacon] = []
        self.client = MeshClient(
            self.node, self.network, on_message=self.messages.append, on_beacon=self.beacons.append
        )


async def test_request_returns_status_parameters_and_start_configures_the_proxy() -> None:
    setup = Setup(lambda op, params: (m.Opcode.ONOFF_STATUS, b"\x01"))
    await setup.client.start()
    assert [t for t, _ in setup.node.sent] == [ProxyPduType.CONFIGURATION]
    params = await setup.client.request(NODE, m.onoff_set(True, 0), m.Opcode.ONOFF_STATUS)
    assert m.OnOffStatus.parse(params).value is True


async def test_request_is_retransmitted_then_times_out() -> None:
    calls = []

    def handler(op: int, params: bytes):  # type: ignore[no-untyped-def]
        calls.append(op)
        return (m.Opcode.ONOFF_STATUS, b"\x00") if len(calls) == 3 else None

    setup = Setup(handler)
    request = setup.client.request(NODE, m.onoff_set(True, 0), m.Opcode.ONOFF_STATUS, timeout=0.05)
    assert await request == b"\x00" and len(calls) == 3
    assert setup.network.sequence.value == 4  # a fresh sequence number per transmission

    silent = Setup(lambda *_: None)
    with pytest.raises(MeshTimeoutError):
        await silent.client.request(
            NODE, m.onoff_set(True, 0), m.Opcode.ONOFF_STATUS, timeout=0.02, attempts=2
        )


async def test_concurrent_requests_are_matched_by_opcode() -> None:
    answers = {
        m.Opcode.ONOFF_SET: (m.Opcode.ONOFF_STATUS, b"\x01"),
        m.Opcode.LIGHTNESS_SET: (m.Opcode.LIGHTNESS_STATUS, b"\x19\x00"),
    }
    setup = Setup(lambda op, params: answers[op])
    on, lightness = await asyncio.gather(
        setup.client.request(NODE, m.onoff_set(True, 0), m.Opcode.ONOFF_STATUS),
        setup.client.request(NODE, m.lightness_set(25, 0), m.Opcode.LIGHTNESS_STATUS),
    )
    assert m.OnOffStatus.parse(on).value is True
    assert m.LightnessStatus.parse(lightness).value == 25


async def test_segmented_exchange_is_acknowledged_in_both_directions() -> None:
    reply = (m.Opcode.CONFIG_COMPOSITION_DATA_STATUS, b"\x00" + SYLVANIA_COMPOSITION)
    setup = Setup(lambda op, params: reply)  # long answer: 7 segments
    composition = await get_composition(setup.client, NODE)
    assert (composition.company_id, len(composition.elements)) == (0x052E, 4)
    await asyncio.sleep(0)  # let the acknowledgement go out
    assert setup.node.received_acks[-1].block_ack == (1 << 7) - 1


async def test_lost_segment_of_a_request_is_sent_again() -> None:
    setup = Setup(lambda op, params: (m.Opcode.CONFIG_APPKEY_STATUS, b"\x00\x00\x00\x00"))
    setup.node.drop_segments = {0}  # AppKey Add takes 2 segments; the first one is lost
    await asyncio.wait_for(add_app_key(setup.client, NODE, KEYS), timeout=1.5)


async def test_refused_configuration_raises() -> None:
    setup = Setup(lambda op, params: (m.Opcode.CONFIG_MODEL_PUBLICATION_STATUS, b"\x07" + params))
    with pytest.raises(ConfigurationError):
        await set_publication(
            setup.client, NODE, NODE, m.Model.HSL_SERVER, publish_address=GROUP, ttl=7
        )


async def test_published_status_reaches_the_listener_once() -> None:
    setup = Setup(lambda *_: None)
    setup.node.transmit(m.Opcode.LIGHTNESS_STATUS, b"\x0a\x00", dst=GROUP)
    setup.node.transmit(m.Opcode.LIGHTNESS_STATUS, b"\x14\x00", dst=0xC001)  # group not listened to
    await asyncio.sleep(0)
    assert setup.messages == [AccessMessage(NODE, GROUP, m.Opcode.LIGHTNESS_STATUS, b"\x0a\x00")]

    # A PDU already received (repeating relay, or replay) is ignored.
    setup.node.counter.value -= 2
    setup.node.transmit(m.Opcode.LIGHTNESS_STATUS, b"\x32\x00", dst=GROUP)
    await asyncio.sleep(0)
    assert len(setup.messages) == 1


async def test_beacon_is_reported_and_ours_can_be_sent() -> None:
    setup = Setup(lambda *_: None, IvState(3))
    setup.node.beacon()
    await asyncio.sleep(0)
    assert setup.beacons == [SecureNetworkBeacon(iv_index=3, iv_update=False, key_refresh=False)]
    await setup.client.send_beacon()
    assert setup.node.sent[-1][0] is ProxyPduType.BEACON


async def test_messages_flow_across_an_iv_update() -> None:
    # The node is already back to normal with index 5; we are still "in progress".
    setup = Setup(lambda op, params: (m.Opcode.ONOFF_STATUS, b"\x01"), IvState(5))
    setup.network.iv = IvState(5, True)  # we transmit with 4, the node answers with 5
    assert await setup.client.request(NODE, m.onoff_set(True, 0), m.Opcode.ONOFF_STATUS) == b"\x01"
