"""PB-GATT provisioning of a mesh device, with "No OOB" authentication (Mesh Profile §5.4).

Sylvania / Ledvance bulbs advertise static OOB support but accept authentication without OOB,
as the official app does.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
import logging
import os
import struct

from cryptography.hazmat.primitives.asymmetric import ec

from .bearer import Bearer
from .crypto import aes_cmac, ccm_encrypt, k1, s1
from .errors import ProvisioningError
from .iv import IvState
from .proxy import ProxyPduType

_LOGGER = logging.getLogger(__name__)

# Provisioning PDU opcodes.
_INVITE, _CAPABILITIES, _START, _PUBLIC_KEY = 0x00, 0x01, 0x02, 0x03
_CONFIRMATION, _RANDOM, _DATA, _COMPLETE, _FAILED = 0x05, 0x06, 0x07, 0x08, 0x09

_ATTENTION_DURATION = 0
# Start: FIPS P-256 algorithm, no OOB public key, "No OOB" authentication.
_START_PARAMS = bytes([0, 0, 0, 0, 0])
_NO_OOB_AUTH_VALUE = bytes(16)
_NET_KEY_INDEX = 0
_FLAG_IV_UPDATE = 0x02

DEFAULT_STEP_TIMEOUT = 15.0
DEFAULT_COMPLETE_TIMEOUT = 20.0


@dataclass(frozen=True, slots=True)
class ProvisioningResult:
    """What we get from a provisioned device."""

    device_key: bytes
    element_count: int
    unicast_address: int


class Provisioner:
    """Provision a device connected over PB-GATT."""

    def __init__(
        self,
        bearer: Bearer,
        *,
        step_timeout: float = DEFAULT_STEP_TIMEOUT,
        complete_timeout: float = DEFAULT_COMPLETE_TIMEOUT,
    ) -> None:
        self._bearer = bearer
        self._step_timeout = step_timeout
        self._complete_timeout = complete_timeout
        self._received: asyncio.Queue[tuple[ProxyPduType, bytes]] = asyncio.Queue()
        bearer.set_receiver(lambda pdu_type, data: self._received.put_nowait((pdu_type, data)))

    async def provision(
        self, net_key: bytes, iv: IvState, allocate_address: Callable[[int], int]
    ) -> ProvisioningResult:
        """Add the device to the `net_key` network, whose IV index is in state `iv`.

        `allocate_address(element_count)` reserves and returns the address of the first element;
        it is called once the device's element count is known.
        """
        await self._send(_INVITE, bytes([_ATTENTION_DURATION]))
        capabilities = await self._expect(_CAPABILITIES)
        element_count = capabilities[0]
        unicast_address = allocate_address(element_count)
        _LOGGER.debug("device capabilities: %s", capabilities.hex())

        await self._send(_START, _START_PARAMS)
        private_key = ec.generate_private_key(ec.SECP256R1())
        numbers = private_key.public_key().public_numbers()
        public_key = numbers.x.to_bytes(32, "big") + numbers.y.to_bytes(32, "big")
        await self._send(_PUBLIC_KEY, public_key)
        device_public_key = await self._expect(_PUBLIC_KEY)
        peer = ec.EllipticCurvePublicNumbers(
            int.from_bytes(device_public_key[:32], "big"),
            int.from_bytes(device_public_key[32:64], "big"),
            ec.SECP256R1(),
        ).public_key()
        secret = private_key.exchange(ec.ECDH(), peer)

        confirmation_inputs = (
            bytes([_ATTENTION_DURATION])
            + capabilities
            + _START_PARAMS
            + public_key
            + device_public_key
        )
        confirmation_salt = s1(confirmation_inputs)
        confirmation_key = k1(secret, confirmation_salt, b"prck")
        random = os.urandom(16)
        await self._send(_CONFIRMATION, aes_cmac(confirmation_key, random + _NO_OOB_AUTH_VALUE))
        device_confirmation = await self._expect(_CONFIRMATION)
        await self._send(_RANDOM, random)
        device_random = await self._expect(_RANDOM)
        if aes_cmac(confirmation_key, device_random + _NO_OOB_AUTH_VALUE) != device_confirmation:
            raise ProvisioningError("invalid device confirmation")

        provisioning_salt = s1(confirmation_salt + random + device_random)
        session_key = k1(secret, provisioning_salt, b"prsk")
        session_nonce = k1(secret, provisioning_salt, b"prsn")[3:]
        device_key = k1(secret, provisioning_salt, b"prdk")
        flags = _FLAG_IV_UPDATE if iv.update_in_progress else 0
        data = net_key + struct.pack(">HBIH", _NET_KEY_INDEX, flags, iv.index, unicast_address)
        await self._send(_DATA, ccm_encrypt(session_key, session_nonce, data, 8))
        await self._expect(_COMPLETE, timeout=self._complete_timeout)
        return ProvisioningResult(device_key, element_count, unicast_address)

    async def _send(self, opcode: int, params: bytes) -> None:
        await self._bearer.send(ProxyPduType.PROVISIONING, bytes([opcode]) + params)

    async def _expect(self, opcode: int, *, timeout: float | None = None) -> bytes:
        """Wait for PDU `opcode` from the device and return its parameters."""
        try:
            async with asyncio.timeout(timeout or self._step_timeout):
                while True:
                    pdu_type, data = await self._received.get()
                    if pdu_type is ProxyPduType.PROVISIONING:
                        break
        except TimeoutError as err:
            raise ProvisioningError(f"no answer from the device (expected {opcode:#04x})") from err
        if data[0] == _FAILED:
            raise ProvisioningError(f"the device refused provisioning (code {data[1]:#04x})")
        if data[0] != opcode:
            raise ProvisioningError(f"unexpected PDU {data[0]:#04x} (expected {opcode:#04x})")
        return data[1:]
