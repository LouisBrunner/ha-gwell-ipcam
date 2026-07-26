"""Motion-event firing tests for custom_components/gwell_ipcam/motion_events.py."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from custom_components.gwell_ipcam.api import Recording
from custom_components.gwell_ipcam.motion_events import EVENT_MOTION_DETECTED, async_handle_recordings_update


@dataclass
class _FakeRuntimeData:
    recordings_coordinator: object
    known_recording_ids: set[str] = field(default_factory=set)
    recordings_since: datetime | None = None


def _recording(recording_id: str, *, tag: str, started_at: datetime) -> Recording:
    return Recording(recording_id=recording_id, started_at=started_at, duration=timedelta(seconds=30), tag=tag)


def _entry(recordings: list[Recording], **runtime_kwargs: object) -> object:
    runtime_data = _FakeRuntimeData(recordings_coordinator=SimpleNamespace(data=recordings), **runtime_kwargs)
    return SimpleNamespace(entry_id="e", runtime_data=runtime_data)


async def test_alarm_triggered_recording_fires_the_event(hass):
    started_at = datetime(2026, 7, 1, 12, 0, 0)
    entry = _entry([_recording("r1", tag="A", started_at=started_at)])
    events = []
    hass.bus.async_listen(EVENT_MOTION_DETECTED, lambda event: events.append(event.data))

    async_handle_recordings_update(hass, entry)
    await hass.async_block_till_done()

    assert len(events) == 1
    assert events[0]["recording_id"] == "r1"


@pytest.mark.parametrize("tag", ["M", "S"])
async def test_non_alarm_recording_does_not_fire_the_event(hass, tag):
    """Covers both manual (button-press, tag M) and scheduled (Timing, tag S) recordings -- neither is an alarm."""
    started_at = datetime(2026, 7, 1, 12, 0, 0)
    entry = _entry([_recording("r1", tag=tag, started_at=started_at)])
    events = []
    hass.bus.async_listen(EVENT_MOTION_DETECTED, lambda event: events.append(event.data))

    async_handle_recordings_update(hass, entry)
    await hass.async_block_till_done()

    assert events == []


async def test_non_alarm_recording_is_still_tracked_as_known(hass):
    """A non-alarm recording must not fire an event, but must still count toward the known-IDs watermark."""
    started_at = datetime(2026, 7, 1, 12, 0, 0)
    entry = _entry([_recording("r1", tag="M", started_at=started_at)])

    async_handle_recordings_update(hass, entry)

    assert "r1" in entry.runtime_data.known_recording_ids
    assert entry.runtime_data.recordings_since == started_at
