"""DataUpdateCoordinators for the Gwell IP Camera integration."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING

from homeassistant.const import CONF_HOST
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import (
    SETTING_REMOTE_RECORD,
    APIAuthError,
    APIConnectionError,
    APIError,
    GwellIPCamClient,
    Recording,
    StorageState,
)
from .const import CLOCK_DRIFT_THRESHOLD_S, LOGGER, RECORDINGS_POLL_INTERVAL_S, STATE_UPDATE_INTERVAL_S

_UPDATE_RETRIES = 2
_DISCOVERY_PROBE_TIMEOUT_S = 2.0

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from datetime import datetime

    from homeassistant.core import HomeAssistant

    from .data import GwellIPCamConfigEntry


@dataclass
class _Probe:
    """Where to reach the camera, for the discovery probe that disambiguates auth failures from outages."""

    hass: HomeAssistant
    host: str


async def _looks_like_auth_failure(probe: _Probe) -> bool:
    """Check whether the camera answers unauthenticated discovery but ignores authenticated reads (bad password)."""
    try:
        found = await GwellIPCamClient.async_discover_one(probe.hass, probe.host, timeout_s=_DISCOVERY_PROBE_TIMEOUT_S)
    except APIError:
        return False
    return found is not None


async def _call_with_retry[T](probe: _Probe, uid: str, label: str, call: Callable[[], Awaitable[T]]) -> T:
    """Retry `call` on a transient connection drop; each call gets its own retries, not the whole batch."""
    for attempt in range(_UPDATE_RETRIES + 1):
        if probe.hass.is_stopping:
            # Don't burn through full-timeout retries while HA is trying to shut down.
            msg = f"{label}: Home Assistant is shutting down"
            raise UpdateFailed(msg)
        try:
            return await call()
        except APIAuthError as exception:
            raise ConfigEntryAuthFailed(exception) from exception
        except APIConnectionError as exception:
            if attempt == _UPDATE_RETRIES:
                if await _looks_like_auth_failure(probe):
                    raise ConfigEntryAuthFailed(exception) from exception
                raise UpdateFailed(exception) from exception
            LOGGER.debug("[%s] %s attempt %s failed (%s), retrying", uid, label, attempt + 1, exception)
        except APIError as exception:
            raise UpdateFailed(exception) from exception
    raise AssertionError


@dataclass
class _Fallback[T]:
    """A field's last known value, and whether one actually exists yet (it may legitimately be `None`)."""

    has_previous: bool
    value: T


async def _fetch_or_keep_previous[T](
    probe: _Probe, uid: str, label: str, call: Callable[[], Awaitable[T]], fallback: _Fallback[T]
) -> T:
    """
    Like `_call_with_retry`, but fall back to `fallback.value` instead of failing the whole coordinator update.

    Without this, one stuck field (e.g. record quality) would mark every entity on this coordinator
    unavailable even though the other three fields fetched fine this cycle.
    """
    try:
        return await _call_with_retry(probe, uid, label, call)
    except UpdateFailed:
        if not fallback.has_previous:
            raise
        LOGGER.warning("[%s] %s still failing after retries, keeping the last known value", uid, label)
        return fallback.value


@dataclass
class GwellIPCamState:
    """Snapshot of a camera's general state."""

    camera_time: datetime
    storage: StorageState
    settings: dict[int, int]
    record_quality: int | None

    @property
    def recording(self) -> bool:
        """Whether the camera is currently recording."""
        return bool(self.settings.get(SETTING_REMOTE_RECORD, 0))


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
        """Fetch the camera's general state; a field still failing after retries keeps its last known value."""
        client = self.config_entry.runtime_data.client
        probe = _Probe(self.hass, self.config_entry.data[CONF_HOST])
        previous = self.data
        uid = uuid.uuid4().hex[:8]
        started = time.monotonic()
        LOGGER.debug("[%s] Starting state check", uid)
        has_previous = previous is not None
        settings = await _fetch_or_keep_previous(
            probe,
            uid,
            "get_settings",
            lambda: client.async_get_settings(uid=uid),
            _Fallback(has_previous, previous.settings if previous else {}),
        )
        camera_time = await _fetch_or_keep_previous(
            probe,
            uid,
            "get_camera_time",
            lambda: client.async_get_camera_time(uid=uid),
            _Fallback(has_previous, previous.camera_time if previous else dt_util.utcnow()),
        )
        storage = await _fetch_or_keep_previous(
            probe,
            uid,
            "get_storage_state",
            lambda: client.async_get_storage_state(uid=uid),
            _Fallback(has_previous, previous.storage if previous else StorageState(used_mb=0, total_mb=0)),
        )
        record_quality = await _fetch_or_keep_previous(
            probe,
            uid,
            "get_record_quality",
            lambda: client.async_get_record_quality(uid=uid),
            _Fallback(has_previous, previous.record_quality if previous else None),
        )
        if abs((dt_util.utcnow() - camera_time).total_seconds()) > CLOCK_DRIFT_THRESHOLD_S:
            LOGGER.info("[%s] Camera clock drifted from %s, syncing", uid, camera_time)
            await _call_with_retry(probe, uid, "sync_time", lambda: client.async_sync_time(uid=uid))
            camera_time = await _call_with_retry(
                probe, uid, "get_camera_time", lambda: client.async_get_camera_time(uid=uid)
            )
        LOGGER.debug("[%s] Finished state check in %.3fs", uid, time.monotonic() - started)
        return GwellIPCamState(
            camera_time=camera_time,
            storage=storage,
            settings=settings,
            record_quality=record_quality,
        )


class GwellIPCamRecordingsCoordinator(DataUpdateCoordinator[list[Recording]]):
    """Polls the recordings list; new IDs are the only motion signal we have."""

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
        """Fetch the current recordings list; keeps the last known list if still failing after retries."""
        client = self.config_entry.runtime_data.client
        probe = _Probe(self.hass, self.config_entry.data[CONF_HOST])
        previous = self.data
        uid = uuid.uuid4().hex[:8]
        started = time.monotonic()
        LOGGER.debug("[%s] Starting recordings check", uid)
        recordings = await _fetch_or_keep_previous(
            probe,
            uid,
            "get_recordings",
            lambda: client.async_get_recordings(uid=uid),
            _Fallback(previous is not None, previous or []),
        )
        LOGGER.debug(
            "[%s] Finished recordings check in %.3fs (%d recordings)", uid, time.monotonic() - started, len(recordings)
        )
        return recordings
