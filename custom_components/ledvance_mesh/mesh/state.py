"""Mesh network state as seen by our node: what outlives a proxy connection."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from .iv import IvState
from .keys import NetworkKeys


class SequenceSource(Protocol):
    """Provides sequence numbers.

    Each number must be used only once per IV index, even across restarts.
    """

    def next(self) -> int:
        """Return the next sequence number."""


@dataclass(slots=True)
class MeshNetwork:
    """Network keys, addresses and counters."""

    keys: NetworkKeys
    address: int  # our unicast address
    sequence: SequenceSource
    iv: IvState = field(default_factory=lambda: IvState(0))
    # Node address -> device key (configuration messages).
    device_keys: dict[int, bytes] = field(default_factory=dict)
    # Group addresses we listen to.
    groups: set[int] = field(default_factory=set)
    # Replay protection: source -> (IV index, sequence number) of the last accepted PDU.
    replay: dict[int, tuple[int, int]] = field(default_factory=dict)

    def accept(self, src: int, iv_index: int, seq: int) -> bool:
        """True if the PDU is newer than the last one accepted from `src`; then remember it."""
        if (iv_index, seq) <= self.replay.get(src, (-1, -1)):
            return False
        self.replay[src] = (iv_index, seq)
        return True

    def forget_node(self, first: int, element_count: int) -> None:
        """Forget a node removed from the network (device key and replay protection)."""
        self.device_keys.pop(first, None)
        for address in range(first, first + element_count):
            self.replay.pop(address, None)
