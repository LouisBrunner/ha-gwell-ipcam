"""Event platform: fires on new recordings, the integration's only motion signal."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.event import EventDeviceClass, EventEntity
from homeassistant.core import callback

from .coordinator import GwellIPCamCoordinator
from .entity import GwellIPCamEntity
from .motion_events import EVENT_MOTION_DETECTED

if TYPE_CHECKING:
    from homeassistant.core import Event, HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .api import CameraIdentity
    from .data import GwellIPCamConfigEntry

EVENT_TYPE_MOTION = "motion"


async def async_setup_entry(
    hass: HomeAssistant,  # noqa: ARG001
    entry: GwellIPCamConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the event platform."""
    async_add_entities(
        [
            GwellIPCamMotionEvent(
                coordinator=entry.runtime_data.coordinator,
                identity=entry.runtime_data.identity,
            )
        ]
    )


class GwellIPCamMotionEvent(GwellIPCamEntity[GwellIPCamCoordinator], EventEntity):
    """Fires when the camera's recordings list gains a new (motion) entry."""

    _attr_translation_key = "motion"
    _attr_icon = "mdi:motion-play"
    _attr_device_class = EventDeviceClass.MOTION

    def __init__(self, coordinator: GwellIPCamCoordinator, identity: CameraIdentity) -> None:
        """Initialize the event entity."""
        super().__init__(coordinator, identity)
        self._attr_event_types = [EVENT_TYPE_MOTION]
        self._attr_unique_id = f"{coordinator.config_entry.unique_id}_motion"

    async def async_added_to_hass(self) -> None:
        """Subscribe to this camera's motion-detected bus events."""
        await super().async_added_to_hass()
        device_id = self.device_entry.id if self.device_entry else None
        self.async_on_remove(
            self.hass.bus.async_listen(
                EVENT_MOTION_DETECTED,
                self.__async_handle_bus_event,
                event_filter=callback(lambda event_data: event_data.get("device_id") == device_id),
            )
        )

    @callback
    def __async_handle_bus_event(self, event: Event) -> None:
        self._trigger_event(
            EVENT_TYPE_MOTION,
            {
                "recording_id": event.data["recording_id"],
                "started_at": event.data["started_at"],
                "media_content_id": event.data["media_content_id"],
            },
        )
        self.async_write_ha_state()
