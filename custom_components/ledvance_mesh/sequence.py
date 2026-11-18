"""Mesh sequence counter that survives restarts.

A sequence number must never be reused with the same IV index: bulbs reject any message whose
number is not greater than the last one received from the same source (replay protection).
To avoid writing to disk on every message, numbers are reserved in blocks: the bound of the
reserve is persisted before it is reached, and after a restart we resume at that bound,
skipping whatever may have been used.
"""

from __future__ import annotations

from collections.abc import Callable

from .mesh.errors import SequenceExhaustedError

BLOCK_SIZE = 1024
MAX_SEQUENCE = 0xFFFFFF


class PersistentSequence:
    """Source of strictly increasing sequence numbers, persisted in blocks."""

    def __init__(self, reserved: int, on_reserve: Callable[[int], None]) -> None:
        """`reserved`: bound persisted by the previous session (0 if none).

        `on_reserve(bound)` must persist the new bound; it is called when the reserve is half
        used, well before it runs out.
        """
        self._next = reserved
        self._reserved = reserved + BLOCK_SIZE
        self._on_reserve = on_reserve

    @property
    def value(self) -> int:
        """Next number to be handed out."""
        return self._next

    def reset(self) -> None:
        """Restart from zero: only call this when the transmit IV index has just changed."""
        self._next = 0
        self._reserved = BLOCK_SIZE

    @property
    def reserved(self) -> int:
        """Bound (excluded) up to which numbers are reserved."""
        return self._reserved

    def next(self) -> int:
        """Return the next sequence number."""
        if self._next >= min(self._reserved, MAX_SEQUENCE + 1):
            raise SequenceExhaustedError(
                "sequence numbers exhausted: the network did not follow the IV index update"
            )
        value = self._next
        self._next += 1
        if self._reserved - self._next <= BLOCK_SIZE // 2:
            self._reserved += BLOCK_SIZE
            self._on_reserve(self._reserved)
        return value
