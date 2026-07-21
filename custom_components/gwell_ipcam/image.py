"""Image platform: static thumbnail of the most recent recording."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.image import ImageEntity
from homeassistant.util import dt as dt_util

from .coordinator import GwellIPCamRecordingsCoordinator
from .entity import GwellIPCamEntity

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .api import CameraIdentity
    from .data import GwellIPCamConfigEntry


async def async_setup_entry(
    hass: HomeAssistant,
    entry: GwellIPCamConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the image platform."""
    async_add_entities(
        [
            GwellIPCamLatestRecordingImage(
                hass=hass,
                coordinator=entry.runtime_data.recordings_coordinator,
                identity=entry.runtime_data.identity,
            )
        ]
    )


class GwellIPCamLatestRecordingImage(GwellIPCamEntity[GwellIPCamRecordingsCoordinator], ImageEntity):
    """Thumbnail of the most recently recorded clip."""

    _attr_translation_key = "latest_recording"

    def __init__(
        self, hass: HomeAssistant, coordinator: GwellIPCamRecordingsCoordinator, identity: CameraIdentity
    ) -> None:
        """Initialize the image entity."""
        GwellIPCamEntity.__init__(self, coordinator, identity)
        ImageEntity.__init__(self, hass)
        self._attr_unique_id = f"{coordinator.config_entry.unique_id}_latest_recording"
        self.__latest_recording_id: str | None = None

    def _handle_coordinator_update(self) -> None:
        recordings = self.coordinator.data or []
        latest_id = max(recordings, key=lambda recording: recording.started_at).recording_id if recordings else None
        if latest_id != self.__latest_recording_id:
            self.__latest_recording_id = latest_id
            self._attr_image_last_updated = dt_util.utcnow()
        super()._handle_coordinator_update()

    async def async_image(self) -> bytes | None:
        """Fetch the latest recording's thumbnail."""
        return await self.coordinator.config_entry.runtime_data.client.async_get_latest_recording_thumbnail()
