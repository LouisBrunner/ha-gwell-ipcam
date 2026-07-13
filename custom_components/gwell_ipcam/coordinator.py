"""DataUpdateCoordinators for the Gwell IP Camera integration."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING

from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import SETTING_REMOTE_RECORD, APIAuthError, APIError, Recording
from .const import LOGGER, RECORDINGS_POLL_INTERVAL_S, STATE_UPDATE_INTERVAL_S

if TYPE_CHECKING:
    from datetime import datetime

    from homeassistant.core import HomeAssistant

    from .api import StorageState
    from .data import GwellIPCamConfigEntry


@dataclass
class GwellIPCamState:
    """Snapshot of a camera's general state."""

    camera_time: datetime
    storage: StorageState
    settings: dict[int, int]
    record_quality: int | None
    fetched_at: datetime

    @property
    def recording(self) -> bool:
        """Whether the camera is currently recording."""
        return bool(self.settings.get(SETTING_REMOTE_RECORD, 0))

    @property
    def live_state(self) -> str:
        """Full raw settings dump as JSON, for diagnostics/history."""
        return json.dumps(self.settings)


class GwellIPCamCoordinator(DataUpdateCoordinator[GwellIPCamState]):
    """Coordinator polling the camera's general state at a relaxed interval."""

    config_entry: GwellIPCamConfigEntry

    def __init__(self, hass: HomeAssistant, config_entry: GwellIPCamConfigEntry) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass=hass,
            logger=LOGGER,
            config_entry=config_entry,
            name=f"{config_entry.title} state",
            update_interval=timedelta(seconds=STATE_UPDATE_INTERVAL_S),
        )

    async def _async_update_data(self) -> GwellIPCamState:
        """Fetch the camera's general state."""
        client = self.config_entry.runtime_data.client
        try:
            settings = await client.async_get_settings()
            camera_time = await client.async_get_camera_time()
            storage = await client.async_get_storage_state()
            record_quality = await client.async_get_record_quality()
        except APIAuthError as exception:
            raise ConfigEntryAuthFailed(exception) from exception
        except APIError as exception:
            raise UpdateFailed(exception) from exception
        return GwellIPCamState(
            camera_time=camera_time,
            storage=storage,
            settings=settings,
            record_quality=record_quality,
            fetched_at=dt_util.utcnow(),
        )


class GwellIPCamRecordingsCoordinator(DataUpdateCoordinator[list[Recording]]):
    """
    Coordinator cheaply polling the recordings list to drive motion detection.

    New entries appearing in the recordings list are the integration's only
    motion signal: no video processing happens locally, we just notice the
    camera wrote a new file and fire an event pointing at it.
    """

    config_entry: GwellIPCamConfigEntry

    def __init__(self, hass: HomeAssistant, config_entry: GwellIPCamConfigEntry) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass=hass,
            logger=LOGGER,
            config_entry=config_entry,
            name=f"{config_entry.title} recordings",
            update_interval=timedelta(seconds=RECORDINGS_POLL_INTERVAL_S),
        )

    async def _async_update_data(self) -> list[Recording]:
        """Fetch the current recordings list."""
        client = self.config_entry.runtime_data.client
        try:
            return await client.async_get_recordings()
        except APIAuthError as exception:
            raise ConfigEntryAuthFailed(exception) from exception
        except APIError as exception:
            raise UpdateFailed(exception) from exception
