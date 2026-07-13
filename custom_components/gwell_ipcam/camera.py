"""Camera platform for the Gwell IP Camera integration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.camera import Camera, CameraEntityFeature

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
    """Set up the camera platform."""
    async_add_entities(
        [
            GwellIPCamCamera(
                coordinator=entry.runtime_data.coordinator,
                identity=entry.runtime_data.identity,
            )
        ]
    )


class GwellIPCamCamera(GwellIPCamEntity[GwellIPCamCoordinator], Camera):
    """Live feed for a Gwell IP camera."""

    _attr_supported_features = CameraEntityFeature.STREAM

    def __init__(self, coordinator: GwellIPCamCoordinator, identity: CameraIdentity) -> None:
        """Initialize the camera."""
        super().__init__(coordinator, identity)
        Camera.__init__(self)
        self._attr_unique_id = f"{coordinator.config_entry.unique_id}_live"

    async def stream_source(self) -> str | None:
        """Return the live stream URL, or None if unavailable (see api.py)."""
        return await self.coordinator.config_entry.runtime_data.client.async_get_live_stream_url()
