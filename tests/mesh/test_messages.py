"""Access messages: opcodes, transitions, encoding and decoding."""

import pytest

from custom_components.ledvance_mesh.mesh import messages as m
from custom_components.ledvance_mesh.mesh.errors import ConfigurationError, MeshError


@pytest.mark.parametrize(
    ("opcode", "encoded"),
    [(0x00, b"\x00"), (0x8203, b"\x82\x03"), (0xC0_05F1, b"\xc0\x05\xf1")],
)
def test_opcode_roundtrip(opcode: int, encoded: bytes) -> None:
    assert m.encode_opcode(opcode) == encoded
    assert m.split_opcode(encoded + b"xyz") == (opcode, b"xyz")


@pytest.mark.parametrize(
    ("seconds", "code"),
    [(0, 0x00), (0.5, 0x05), (5, 0x32), (10, 0x4A), (120, 0x8C), (10**6, 0xFE)],
)
def test_transition_time(seconds: float, code: int) -> None:
    assert m.encode_transition_time(seconds) == code


def test_commands() -> None:
    assert m.onoff_set(True, 7) == b"\x82\x02\x01\x07"
    assert m.onoff_set(False, 7, 5) == b"\x82\x02\x00\x07\x32\x00"
    assert m.lightness_set(25, 3) == b"\x82\x4c\x19\x00\x03"
    assert m.ctl_set(25, 2700, 9) == b"\x82\x5e\x19\x00\x8c\x0a\x00\x00\x09"
    assert m.hsl_set(25, 120, 100, 1, 2) == b"\x82\x76\x19\x00\x78\x00\x64\x00\x01\x14\x00"


def test_status_parsing_with_and_without_transition() -> None:
    assert m.OnOffStatus.parse(b"\x01").value is True
    assert m.OnOffStatus.parse(b"\x01\x00\x32").value is False
    assert m.LightnessStatus.parse(bytes.fromhex("0100")).value == 1
    status = m.LightnessStatus.parse(bytes.fromhex("0100320032"))
    assert (status.present, status.target, status.value) == (1, 50, 50)
    ctl = m.CtlStatus.parse(bytes.fromhex("3200940a"))
    assert (ctl.settled_lightness, ctl.settled_temperature) == (50, 2708)
    ctl_moving = m.CtlStatus.parse(bytes.fromhex("1900940a320064190a"))
    assert (ctl_moving.settled_lightness, ctl_moving.settled_temperature) == (50, 6500)
    assert m.HslStatus.parse(bytes.fromhex("320068016400")) == m.HslStatus(50, 360, 100)


def test_truncated_status_raises() -> None:
    with pytest.raises(MeshError):
        m.HslStatus.parse(b"\x01\x02")


def test_configuration_messages() -> None:
    key = bytes(range(16))
    assert m.config_appkey_add(0, 0, key) == b"\x00\x00\x00\x00" + key
    assert m.config_model_app_bind(0x0010, 0, 0x1307) == b"\x80\x3d\x10\x00\x00\x00\x07\x13"
    assert m.config_node_reset() == b"\x80\x49"


def test_config_status_check() -> None:
    m.check_config_status(b"\x00\x00\x00\x00", "test")
    with pytest.raises(ConfigurationError, match="0x06"):
        m.check_config_status(b"\x06\x00\x00\x00", "test")
