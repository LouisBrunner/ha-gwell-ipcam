"""Assist Satellite platform: announcements and push-to-talk conversation via the camera."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.assist_satellite import (
    AssistSatelliteConfiguration,
    AssistSatelliteEntity,
    AssistSatelliteEntityFeature,
)
from homeassistant.core import callback

from .audio import async_listen_stream_16k, async_media_id_to_pcm16_8k
from .const import LOGGER
from .coordinator import GwellIPCamCoordinator
from .entity import GwellIPCamEntity

if TYPE_CHECKING:
    from homeassistant.components.assist_pipeline import PipelineEvent
    from homeassistant.components.assist_satellite import AssistSatelliteAnnouncement
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .api import CameraIdentity
    from .data import GwellIPCamConfigEntry


async def async_setup_entry(
    hass: HomeAssistant,  # noqa: ARG001
    entry: GwellIPCamConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the assist_satellite platform."""
    async_add_entities(
        [
            GwellIPCamAssistSatellite(
                coordinator=entry.runtime_data.coordinator,
                identity=entry.runtime_data.identity,
            )
        ]
    )


class GwellIPCamAssistSatellite(GwellIPCamEntity[GwellIPCamCoordinator], AssistSatelliteEntity):
    """
    Lets a camera announce TTS and take one push-to-talk conversation turn.

    Deliberately push-to-talk only (via `assist_satellite.start_conversation`), not
    always-listening wake-word: that would need a permanently-open mic connection with
    unverified long-term stability on this camera's RTSP server, plus adds continuous load
    to HA's own wake-word processing for very marginal benefit on a camera.
    """

    _attr_supported_features = (
        AssistSatelliteEntityFeature.ANNOUNCE | AssistSatelliteEntityFeature.START_CONVERSATION
    )

    def __init__(self, coordinator: GwellIPCamCoordinator, identity: CameraIdentity) -> None:
        """Initialize the satellite."""
        super().__init__(coordinator, identity)
        self._attr_unique_id = f"{coordinator.config_entry.unique_id}_assist_satellite"

    @callback
    def async_get_configuration(self) -> AssistSatelliteConfiguration:
        """Report no wake-word support -- this satellite is push-to-talk only."""
        return AssistSatelliteConfiguration(available_wake_words=[], active_wake_words=[], max_active_wake_words=0)

    async def async_set_configuration(self, config: AssistSatelliteConfiguration) -> None:
        """No configurable options -- nothing to set."""

    def on_pipeline_event(self, event: PipelineEvent) -> None:
        """No custom handling needed -- base class already drives the entity's state."""

    async def async_announce(self, announcement: AssistSatelliteAnnouncement) -> None:
        """Push the given media to the camera's speaker."""
        LOGGER.debug("User announced %s on %s", announcement.media_id, self.entity_id)
        client = self.coordinator.config_entry.runtime_data.client
        pcm = await async_media_id_to_pcm16_8k(self.hass, announcement.media_id)
        await client.async_talk(pcm)

    async def async_start_conversation(self, start_announcement: AssistSatelliteAnnouncement) -> None:
        """Announce (if given), then run one pipeline turn against the camera's mic."""
        LOGGER.debug("User started a conversation on %s", self.entity_id)
        if start_announcement is not None and start_announcement.media_id:
            await self.async_announce(start_announcement)
        session = self.coordinator.config_entry.runtime_data.client.rtsp_session
        await self.async_accept_pipeline_from_satellite(audio_stream=async_listen_stream_16k(session))
