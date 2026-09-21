"""Custom types for the Gwell IP Camera integration."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .const import DOMAIN

if TYPE_CHECKING:
    import asyncio
    from datetime import datetime

    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .api import CameraIdentity, GwellIPCamClient
    from .coordinator import GwellIPCamCoordinator, GwellIPCamRecordingsCoordinator


type GwellIPCamConfigEntry = ConfigEntry[GwellIPCamData]


def _cancel_task(task: asyncio.Task[None]) -> None:
    task.cancel()


@dataclass
class GwellIPCamData:
    """Stored on `ConfigEntry.runtime_data`; general state and recordings are polled by separate coordinators."""

    client: GwellIPCamClient
    identity: CameraIdentity
    coordinator: GwellIPCamCoordinator
    recordings_coordinator: GwellIPCamRecordingsCoordinator
    known_recording_ids: set[str] = field(default_factory=set)
    # Only recordings started after this are eligible to fire a motion event, however they were first noticed.
    recordings_since: datetime | None = None

    async def async_start_coordinators(
        self, hass: HomeAssistant, entry: GwellIPCamConfigEntry, *, degraded: bool
    ) -> None:
        """Run each coordinator's first refresh; backgrounded when already known-degraded, to not block entities."""
        if degraded:
            self.coordinator.last_update_success = False
            self.recordings_coordinator.last_update_success = False
            task = hass.async_create_background_task(self.coordinator.async_refresh(), f"{DOMAIN}-first-refresh")
            entry.async_on_unload(lambda: _cancel_task(task))
            recordings_task = hass.async_create_background_task(
                self.recordings_coordinator.async_refresh(), f"{DOMAIN}-recordings-first-refresh"
            )
            entry.async_on_unload(lambda: _cancel_task(recordings_task))
        else:
            await self.coordinator.async_config_entry_first_refresh()
            await self.recordings_coordinator.async_config_entry_first_refresh()
