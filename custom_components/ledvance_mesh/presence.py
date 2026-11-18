"""Lamp presence: knowing whether lamps are powered and reachable.

A lamp switched off at the wall gives no warning: it just goes silent. Three clues say a lamp
is there, from fastest to slowest:

- the Bluetooth connection open with it, when it is the proxy;
- its Bluetooth advertisements (about one per second), if the Home Assistant adapter hears them;
- the mesh messages it sends, including the state it publishes at a regular interval.

This module only keeps these clues up to date and tells what to conclude; it sends nothing.
"""

from __future__ import annotations

from enum import Enum, auto
import time

from .const import ADVERTISEMENT_TIMEOUT, HEARTBEAT_TIMEOUT


class Presence(Enum):
    """What the clues say about a lamp."""

    PRESENT = auto()
    DOUBTFUL = auto()  # its advertisements stopped: it must be asked
    GONE = auto()


class PresenceTracker:
    """Presence clues of each lamp, timed with the monotonic clock."""

    def __init__(self) -> None:
        self._last_message: dict[str, float] = {}
        self._last_advertisement: dict[str, float] = {}

    def message_received(self, lamp: str) -> None:
        """A mesh message from the lamp just arrived (answer or published state)."""
        self._last_message[lamp] = time.monotonic()

    def advertisement_received(self, lamp: str) -> None:
        """A Bluetooth advertisement of the lamp was just heard."""
        self._last_advertisement[lamp] = time.monotonic()

    def advertisements_stopped(self, lamp: str) -> None:
        """Stop relying on the lamp's advertisements until the next one is heard.

        To call when they stopped without meaning anything: the lamp answered despite their
        absence, or it is the proxy (a connected lamp no longer advertises).
        """
        self._last_advertisement.pop(lamp, None)

    def message_age(self, lamp: str) -> float | None:
        """Seconds since the lamp's last mesh message (None: never received)."""
        last = self._last_message.get(lamp)
        return None if last is None else time.monotonic() - last

    def advertisement_age(self, lamp: str) -> float | None:
        """Seconds since the last tracked advertisement (None: advertisements not tracked)."""
        last = self._last_advertisement.get(lamp)
        return None if last is None else time.monotonic() - last

    def assess(self, lamp: str, *, is_proxy: bool) -> Presence:
        """Conclude from the available clues."""
        if is_proxy:
            return Presence.PRESENT
        advertisement_age = self.advertisement_age(lamp)
        if advertisement_age is not None:
            if advertisement_age <= ADVERTISEMENT_TIMEOUT:
                return Presence.PRESENT
            return Presence.DOUBTFUL
        message_age = self.message_age(lamp)
        if message_age is not None and message_age <= HEARTBEAT_TIMEOUT:
            return Presence.PRESENT
        return Presence.GONE
