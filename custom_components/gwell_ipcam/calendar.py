"""Calendar platform: recordings as calendar events."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.util import dt as dt_util

from .coordinator import GwellIPCamRecordingsCoordinator
from .entity import GwellIPCamEntity
from .media_source import media_source_identifier

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


_TAG_SUMMARIES = {"A": "Motion recording", "M": "Manual recording", "S": "Scheduled recording"}


def _to_event(entry: GwellIPCamConfigEntry, source_entity_id: str, recording: Recording) -> CalendarEvent:
    media_content_id = media_source_identifier(entry, recording.recording_id)
    description = (
        f"Recording ID: {recording.recording_id}\n"
        f"Duration: {recording.duration}\n"
        f"Media: {media_content_id}\n"
        f"Source: {source_entity_id}"
    )
    return CalendarEvent(
        start=recording.started_at,
        end=recording.started_at + recording.duration,
        summary=_TAG_SUMMARIES.get(recording.tag, "Recording"),
        description=description,
        uid=recording.recording_id,
    )


class GwellIPCamCalendar(GwellIPCamEntity[GwellIPCamRecordingsCoordinator], CalendarEntity):
    """The single recordings-calendar entity for one camera device."""

    _attr_translation_key = "recordings_calendar"
    _attr_icon = "mdi:calendar-clock"

    def __init__(self, coordinator: GwellIPCamRecordingsCoordinator, identity: CameraIdentity) -> None:
        """Initialize the calendar."""
        super().__init__(coordinator, identity)
        self._attr_unique_id = f"{coordinator.config_entry.unique_id}_recordings_calendar"

    @property
    def event(self) -> CalendarEvent | None:
        """Return the currently in-progress recording, if any (all our events are in the past otherwise)."""
        now = dt_util.utcnow()
        ongoing = [r for r in self.coordinator.data or [] if r.started_at <= now <= r.started_at + r.duration]
        if not ongoing:
            return None
        latest = max(ongoing, key=lambda recording: recording.started_at)
        return _to_event(self.coordinator.config_entry, self.entity_id, latest)

    async def async_get_events(
        self,
        hass: HomeAssistant,  # noqa: ARG002
        start_date: datetime,
        end_date: datetime,
    ) -> list[CalendarEvent]:
        """Return recordings that fall within the requested range."""
        recordings = self.coordinator.data or []
        entry = self.coordinator.config_entry
        return [_to_event(entry, self.entity_id, r) for r in recordings if start_date <= r.started_at <= end_date]
