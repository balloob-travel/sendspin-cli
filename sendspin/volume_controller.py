"""Protocol for external volume controller backends."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

VolumeChangeCallback = Callable[[int, bool], None]


class VolumeController(Protocol):
    """Contract implemented by external volume controller backends."""

    async def set_state(self, volume: int, *, muted: bool) -> None:
        """Apply a logical volume and mute state."""

    async def get_state(self) -> tuple[int, bool]:
        """Read the logical volume and mute state."""

    async def start_monitoring(self, callback: VolumeChangeCallback) -> None:
        """Start reporting externally observed state changes."""

    async def stop_monitoring(self) -> None:
        """Stop monitoring external state changes."""

