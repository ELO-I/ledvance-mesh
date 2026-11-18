"""Network IV index and the IV Update procedure (Mesh Profile §3.10.5).

The IV index is part of every nonce: together with the 24-bit sequence number, it ensures a
nonce is never used twice. When sequence numbers run out, the network moves to the next IV index
in two steps:

1. "update in progress": everyone already accepts the new index but still transmits with the old;
2. back to normal: everyone transmits with the new index and sequence numbers restart from 0.

Each phase lasts at least 96 hours, so that every node has time to follow.
"""

from __future__ import annotations

from dataclasses import dataclass

# Largest gap a node may catch up with at once (IV Index Recovery).
_MAX_RECOVERY = 42

# Minimum duration of each phase of the procedure, in seconds.
MIN_PHASE_DURATION = 96 * 3600

# Past this sequence number it is time to start an update: half of the numbers remain to last
# through the 96 hours of the "in progress" phase.
SEQUENCE_UPDATE_THRESHOLD = 0x800000


@dataclass(frozen=True, slots=True)
class IvState:
    """Current IV index, and update-in-progress flag."""

    index: int
    update_in_progress: bool = False

    @property
    def tx_index(self) -> int:
        """IV index to transmit with."""
        return self.index - 1 if self.update_in_progress else self.index

    def rx_index(self, ivi: int) -> int | None:
        """IV index of a received PDU, from its IVI bit: the current index or the previous one."""
        if self.index & 1 == ivi:
            return self.index
        return self.index - 1 if self.index > 0 else None

    def begin_update(self) -> IvState:
        """Start an update: accept the new index, keep transmitting with the old one."""
        return self if self.update_in_progress else IvState(self.index + 1, True)

    def complete_update(self) -> IvState:
        """Complete the update: transmit with the new index from now on."""
        return IvState(self.index, False)

    def after_beacon(self, index: int, update_in_progress: bool) -> IvState:
        """State to adopt after an authenticated beacon announcing this index and update flag."""
        if index < self.index or index > self.index + _MAX_RECOVERY:
            return self
        if index == self.index:
            # At the same index, only the end of the update is news; a node still "in progress"
            # while we are back to normal is simply late.
            return self.complete_update() if not update_in_progress else self
        return IvState(index, update_in_progress)
