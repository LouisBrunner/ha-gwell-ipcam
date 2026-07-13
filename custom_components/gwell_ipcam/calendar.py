"""
Calendar platform for the Gwell IP Camera integration.

Surfaces the camera's recordings (each one implies motion was detected) as
calendar events, so they can be reviewed in HA's calendar views.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.calendar import CalendarEntity, CalendarEvent

from .coordinator import GwellIPCamRecordingsCoordinator
from .entity import GwellIPCamEntity

if TYPE_CHECKING:
    from datetime import datetime

    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .api import CameraIdentity, Recording
    from .data import GwellIPCamConfigEntry


async def async_setup_entry(
    hass: HomeAssistant,  # noqa: ARG001
    entry: GwellIPCamConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the calendar platform."""
    async_add_entities(
        [
            GwellIPCamCalendar(
                coordinator=entry.runtime_data.recordings_coordinator,
                identity=entry.runtime_data.identity,
            )
        ]
    )


def _to_event(recording: Recording) -> CalendarEvent:
    return CalendarEvent(
        start=recording.started_at,
        end=recording.started_at + recording.duration,
        summary="Motion recording",
        uid=recording.recording_id,
    )


class GwellIPCamCalendar(GwellIPCamEntity[GwellIPCamRecordingsCoordinator], CalendarEntity):
    """Calendar of recordings for a camera."""

    def __init__(self, coordinator: GwellIPCamRecordingsCoordinator, identity: CameraIdentity) -> None:
        """Initialize the calendar."""
        super().__init__(coordinator, identity)
        self._attr_unique_id = f"{coordinator.config_entry.unique_id}_recordings_calendar"

    @property
    def event(self) -> CalendarEvent | None:
        """Return the most recent recording, if any."""
        recordings = self.coordinator.data or []
        if not recordings:
            return None
        return _to_event(max(recordings, key=lambda recording: recording.started_at))

    async def async_get_events(
        self,
        hass: HomeAssistant,  # noqa: ARG002
        start_date: datetime,
        end_date: datetime,
    ) -> list[CalendarEvent]:
        """Return recordings that fall within the requested range."""
        recordings = self.coordinator.data or []
        return [_to_event(recording) for recording in recordings if start_date <= recording.started_at <= end_date]
