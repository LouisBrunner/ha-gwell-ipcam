"""Media player platform for the Gwell IP Camera integration: pushes audio to the camera's speaker."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.media_player import (
    MediaPlayerDeviceClass,
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
)

from .audio import async_media_id_to_pcm16_8k
from .const import LOGGER
from .coordinator import GwellIPCamCoordinator
from .entity import GwellIPCamEntity

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .api import CameraIdentity
    from .data import GwellIPCamConfigEntry


async def async_setup_entry(
    hass: HomeAssistant,  # noqa: ARG001
    entry: GwellIPCamConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the media_player platform."""
    async_add_entities(
        [
            GwellIPCamMediaPlayer(
                coordinator=entry.runtime_data.coordinator,
                identity=entry.runtime_data.identity,
            )
        ]
    )


class GwellIPCamMediaPlayer(GwellIPCamEntity[GwellIPCamCoordinator], MediaPlayerEntity):
    """Pushes arbitrary media (TTS, announcements) to the camera's speaker as talk-back audio."""

    _attr_translation_key = "speaker"
    _attr_icon = "mdi:bullhorn"
    _attr_device_class = MediaPlayerDeviceClass.SPEAKER
    _attr_supported_features = MediaPlayerEntityFeature.PLAY_MEDIA
    _attr_state = MediaPlayerState.IDLE

    def __init__(self, coordinator: GwellIPCamCoordinator, identity: CameraIdentity) -> None:
        """Initialize the media player."""
        super().__init__(coordinator, identity)
        self._attr_unique_id = f"{coordinator.config_entry.unique_id}_speaker"

    async def async_play_media(self, media_type: str, media_id: str, **kwargs: object) -> None:  # noqa: ARG002
        """Decode the given media to 8kHz mono PCM16 and push it to the camera's speaker."""
        LOGGER.debug("User played media %s (%s) on %s", media_id, media_type, self.entity_id)
        client = self.coordinator.config_entry.runtime_data.client
        self._attr_state = MediaPlayerState.PLAYING
        self.async_write_ha_state()
        try:
            pcm = await async_media_id_to_pcm16_8k(self.hass, media_id)
            await client.async_talk(pcm)
        finally:
            self._attr_state = MediaPlayerState.IDLE
            self.async_write_ha_state()
