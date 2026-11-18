"""Bluetooth Mesh stack exceptions."""

from __future__ import annotations


class MeshError(Exception):
    """Generic mesh stack error."""


class MeshTimeoutError(MeshError):
    """The node did not answer in time."""


class ProvisioningError(MeshError):
    """Provisioning of a device failed."""


class ConfigurationError(MeshError):
    """A node refused a configuration message (non-zero status)."""


class SequenceExhaustedError(MeshError):
    """The sequence counter is exhausted: the IV index should have been incremented."""
