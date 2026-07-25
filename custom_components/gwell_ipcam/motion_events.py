"""Fires a motion event for each newly-seen recording ID."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.helpers import entity_registry as er

from .const import DOMAIN, LOGGER
from .media_source import media_source_identifier

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .api import Recording
    from .data import GwellIPCamConfigEntry

EVENT_MOTION_DETECTED = f"{DOMAIN}_motion_detected"


def async_handle_recordings_update(hass: HomeAssistant, entry: GwellIPCamConfigEntry) -> None:
    """
    Diff the latest recordings list against known IDs and fire events for new ones.

    A recording also has to be newer than `recordings_since`, not just unseen by ID: the camera's recordings
    listing is fetched over an unreliable UDP exchange, so an early poll can genuinely undercount what's on the
    SD card. Without this recency gate, a later poll that returns the full list would treat every older,
    previously-missed recording as "new" and fire a motion event for all of them at once.
    """
    data = entry.runtime_data
    recordings: list[Recording] = data.recordings_coordinator.data or []

    watermark = data.recordings_since
    new_ids = {recording.recording_id for recording in recordings}
    unseen = [
        recording
        for recording in recordings
        if recording.recording_id not in data.known_recording_ids
        and (watermark is None or recording.started_at > watermark)
    ]
    data.known_recording_ids = new_ids
    if recordings:
        newest = max(recording.started_at for recording in recordings)
        if watermark is None or newest > watermark:
            data.recordings_since = newest

    if not unseen:
        return

    for recording in unseen:
        event_data = {
            "device_id": _device_id(hass, entry),
            "recording_id": recording.recording_id,
            "started_at": recording.started_at.isoformat(),
            "duration_s": recording.duration.total_seconds(),
            "media_content_id": media_source_identifier(entry, recording.recording_id),
        }
        LOGGER.debug("Firing %s: %s", EVENT_MOTION_DETECTED, event_data)
        hass.bus.async_fire(EVENT_MOTION_DETECTED, event_data)


def _device_id(hass: HomeAssistant, entry: GwellIPCamConfigEntry) -> str | None:
    """Look up the device registry ID for this camera's device entry."""
    registry = er.async_get(hass)
    entries = er.async_entries_for_config_entry(registry, entry.entry_id)
    return entries[0].device_id if entries else None
