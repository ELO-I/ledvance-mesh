"""Stateful fake bulb node: behaves like a Sylvania SMART+ (native units)."""

from __future__ import annotations

import struct

from custom_components.ledvance_mesh.mesh import MeshClient, MeshNetwork, NetworkKeys, messages as m
from custom_components.ledvance_mesh.mesh.messages import Opcode
from tests.mesh.fakes import Counter, FakeNodeBearer

KEYS = NetworkKeys(bytes(range(16)), bytes(range(16, 32)))


def _composition() -> bytes:
    """Page 0 of the composition data, as read from a real Sylvania 60A19CBLE bulb."""
    elements = (
        [0x0000, 0x0002, 0x0003, 0x1000, 0x1002, 0x1004, 0x1006, 0x1007, 0x1300, 0x1301, 0x1303],
        [0x1002, 0x1306],
        [0x1002, 0x130A],
        [0x1002, 0x130B],
    )
    elements[0].extend([0x1304, 0x1307, 0x1308])
    data = struct.pack("<HHHHH", 0x052E, 0x0001, 0x3233, 105, 0x0007)
    for index, models in enumerate(elements):
        vendor = [bytes.fromhex("2e050000")] if index == 0 else []
        data += struct.pack("<HBB", 0, len(models), len(vendor))
        data += struct.pack(f"<{len(models)}H", *models) + b"".join(vendor)
    return data


SYLVANIA_COMPOSITION = _composition()
DEVICE_KEY = bytes(range(100, 116))
CLIENT_ADDRESS = 0x0001


class FakeLamp:
    """State of a bulb and handling of the messages it receives."""

    def __init__(self, unicast: int = 0x0010) -> None:
        self.unicast = unicast
        self.on = False
        self.lightness = 50  # 1-50
        self.hue, self.saturation, self.temperature = 0, 0, 2700
        self.online = True
        self.composition = SYLVANIA_COMPOSITION
        self.received: list[tuple[int, bytes]] = []

    def bearer(self) -> FakeNodeBearer:
        return FakeNodeBearer(
            KEYS,
            node_address=self.unicast,
            client_address=CLIENT_ADDRESS,
            device_key=DEVICE_KEY,
            handler=self.handle,
        )

    def client(self) -> MeshClient:
        network = MeshNetwork(
            KEYS, CLIENT_ADDRESS, Counter(1), device_keys={self.unicast: DEVICE_KEY}
        )
        return MeshClient(self.bearer(), network)

    def handle(self, opcode: int, params: bytes) -> tuple[int, bytes] | None:
        if not self.online:
            return None
        self.received.append((opcode, params))
        lightness = self.lightness if self.on else 0  # when off, the bulb reports 0
        if opcode == Opcode.LIGHTNESS_LAST_GET:
            return Opcode.LIGHTNESS_LAST_STATUS, struct.pack("<H", self.lightness)
        if opcode == Opcode.ONOFF_SET:
            self.on = bool(params[0])
            return Opcode.ONOFF_STATUS, bytes([int(self.on)])
        if opcode == Opcode.LIGHTNESS_SET:
            (lightness,) = struct.unpack("<H", params[:2])
            self.lightness, self.on = max(1, min(50, lightness)), True
            return Opcode.LIGHTNESS_STATUS, struct.pack("<H", self.lightness)
        if opcode == Opcode.CTL_GET:
            return Opcode.CTL_STATUS, struct.pack("<HH", lightness, self.temperature)
        if opcode == Opcode.CTL_SET:
            lightness, temperature = struct.unpack("<HH", params[:4])
            self.lightness, self.temperature, self.on = (
                max(1, min(50, lightness)),
                temperature,
                True,
            )
            return Opcode.CTL_STATUS, struct.pack("<HH", self.lightness, self.temperature)
        if opcode == Opcode.HSL_GET:
            return Opcode.HSL_STATUS, struct.pack("<HHH", lightness, self.hue, self.saturation)
        if opcode == Opcode.HSL_SET:
            lightness, hue, saturation = struct.unpack("<HHH", params[:6])
            # Like the real bulb: out-of-range values are capped.
            self.lightness, self.on = max(1, min(50, lightness)), True
            self.hue, self.saturation = min(hue, 360), min(saturation, 100)
            return Opcode.HSL_STATUS, struct.pack("<HHH", self.lightness, self.hue, self.saturation)
        if opcode == Opcode.CONFIG_APPKEY_ADD:
            return Opcode.CONFIG_APPKEY_STATUS, b"\x00\x00\x00\x00"
        if opcode == Opcode.CONFIG_MODEL_APP_BIND:
            return Opcode.CONFIG_MODEL_APP_STATUS, b"\x00" + params
        if opcode == Opcode.CONFIG_MODEL_PUBLICATION_SET:
            return Opcode.CONFIG_MODEL_PUBLICATION_STATUS, b"\x00" + params
        if opcode == Opcode.CONFIG_COMPOSITION_DATA_GET:
            return Opcode.CONFIG_COMPOSITION_DATA_STATUS, b"\x00" + self.composition
        if opcode == Opcode.CONFIG_NODE_RESET:
            return Opcode.CONFIG_NODE_RESET_STATUS, b""
        raise AssertionError(f"opcode not handled by the fake node: {opcode:#x}")


__all__ = ["DEVICE_KEY", "KEYS", "SYLVANIA_COMPOSITION", "FakeLamp", "m"]
