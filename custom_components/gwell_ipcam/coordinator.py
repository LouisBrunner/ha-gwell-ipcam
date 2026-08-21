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
_MAX_FALLBACK_STREAK = 3
_MIN_AUTH_FAILURE_STREAK = 3

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


async def _confirm_auth_failure(uid: str, label: str, probe: _Probe, auth_streaks: dict[str, int] | None) -> bool:
    """Whether `label` has now looked like a bad password for `_MIN_AUTH_FAILURE_STREAK` consecutive polls."""
    if not await _looks_like_auth_failure(probe):
        if auth_streaks is not None:
            auth_streaks.pop(label, None)
        return False
    streak = (auth_streaks.get(label, 0) + 1) if auth_streaks is not None else _MIN_AUTH_FAILURE_STREAK
    if auth_streaks is not None:
        auth_streaks[label] = streak
    if streak < _MIN_AUTH_FAILURE_STREAK:
        LOGGER.debug(
            "[%s] %s looks like a bad password (%d/%d), could still be a slow boot",
            uid,
            label,
            streak,
            _MIN_AUTH_FAILURE_STREAK,
        )
        return False
    return True


async def _call_with_retry[T](
    probe: _Probe, uid: str, label: str, call: Callable[[], Awaitable[T]], auth_streaks: dict[str, int] | None = None
) -> T:
    """Retry `call` on a transient connection drop; each call gets its own retries, not the whole batch."""
    for attempt in range(_UPDATE_RETRIES + 1):
        if probe.hass.is_stopping:
            # Don't burn through full-timeout retries while HA is trying to shut down.
            msg = f"{label}: Home Assistant is shutting down"
            raise UpdateFailed(msg)
        try:
            result = await call()
        except APIAuthError as exception:
            raise ConfigEntryAuthFailed(exception) from exception
        except APIConnectionError as exception:
            if attempt == _UPDATE_RETRIES:
                if await _confirm_auth_failure(uid, label, probe, auth_streaks):
                    raise ConfigEntryAuthFailed(exception) from exception
                raise UpdateFailed(exception) from exception
            LOGGER.debug("[%s] %s attempt %s failed (%s), retrying", uid, label, attempt + 1, exception)
        except APIError as exception:
            raise UpdateFailed(exception) from exception
        else:
            if auth_streaks is not None:
                auth_streaks.pop(label, None)
            return result
    raise AssertionError


@dataclass
class _Fallback[T]:
    """A field's last known value, and whether one actually exists yet (it may legitimately be `None`)."""

    has_previous: bool
    value: T


@dataclass
class _FetchContext:
    probe: _Probe
    uid: str
    streaks: dict[str, int]
    auth_streaks: dict[str, int]


async def _fetch_or_keep_previous[T](
    ctx: _FetchContext, label: str, call: Callable[[], Awaitable[T]], fallback: _Fallback[T]
) -> T:
    """Like `_call_with_retry`, but falls back to `fallback.value` up to `_MAX_FALLBACK_STREAK` times, not failing."""
    try:
        result = await _call_with_retry(ctx.probe, ctx.uid, label, call, ctx.auth_streaks)
    except UpdateFailed:
        if not fallback.has_previous:
            raise
        streak = ctx.streaks.get(label, 0) + 1
        ctx.streaks[label] = streak
        if streak == 1:
            LOGGER.warning("[%s] %s failed, keeping the last known value while retrying", ctx.uid, label)
        elif streak == _MAX_FALLBACK_STREAK + 1:
            LOGGER.warning(
                "[%s] %s failed %d times in a row, no longer serving the stale value", ctx.uid, label, streak
            )
        else:
            LOGGER.debug(
                "[%s] %s still failing after retries (%d), keeping the last known value", ctx.uid, label, streak
            )
        return fallback.value
    else:
        if ctx.streaks.pop(label, None):
            LOGGER.info("[%s] %s recovered", ctx.uid, label)
        return result


@dataclass
class GwellIPCamState:
    """Snapshot of a camera's general state; individual fields may be a stale fallback if a poll failed."""

    camera_time: datetime
    storage: StorageState
    settings: dict[int, int]
    record_quality: int | None

    @property
    def recording(self) -> bool:
        """Derive from `SETTING_REMOTE_RECORD` in `settings`, rather than a dedicated wire field."""
        return bool(self.settings.get(SETTING_REMOTE_RECORD, 0))


class GwellIPCamCoordinator(DataUpdateCoordinator[GwellIPCamState]):
    """Coordinator polling the camera's general state at a relaxed interval."""

    config_entry: GwellIPCamConfigEntry

    def __init__(self, hass: HomeAssistant, config_entry: GwellIPCamConfigEntry) -> None:
        """Initialize, polling at `STATE_UPDATE_INTERVAL_S` (the relaxed, general-state cadence)."""
        super().__init__(
            hass=hass,
            logger=LOGGER,
            config_entry=config_entry,
            name=f"{config_entry.title} state",
            update_interval=timedelta(seconds=STATE_UPDATE_INTERVAL_S),
            always_update=False,
        )
        self.__fallback_streaks: dict[str, int] = {}
        self.__auth_streaks: dict[str, int] = {}

    def apply_fresh_settings(self, settings: dict[int, int]) -> None:
        """Push a write's already-confirmed settings, bypassing a fresh poll that could race a stale value."""
        if self.data is None:
            return
        self.async_set_updated_data(
            GwellIPCamState(
                camera_time=self.data.camera_time,
                storage=self.data.storage,
                settings=settings,
                record_quality=self.data.record_quality,
            )
        )

    def apply_fresh_record_quality(self, record_quality: int) -> None:
        """Apply the same reasoning as `apply_fresh_settings`, for the one field outside the settings dump."""
        if self.data is None:
            return
        self.async_set_updated_data(
            GwellIPCamState(
                camera_time=self.data.camera_time,
                storage=self.data.storage,
                settings=self.data.settings,
                record_quality=record_quality,
            )
        )

    def apply_fresh_camera_time(self, camera_time: datetime) -> None:
        """Apply the same reasoning as `apply_fresh_settings`, for the camera clock."""
        if self.data is None:
            return
        self.async_set_updated_data(
            GwellIPCamState(
                camera_time=camera_time,
                storage=self.data.storage,
                settings=self.data.settings,
                record_quality=self.data.record_quality,
            )
        )

    async def _async_update_data(self) -> GwellIPCamState:
        """Fetch the camera's general state; a field still failing after retries keeps its last known value."""
        client = self.config_entry.runtime_data.client
        probe = _Probe(self.hass, self.config_entry.data[CONF_HOST])
        previous = self.data
        uid = uuid.uuid4().hex[:8]
        ctx = _FetchContext(probe, uid, self.__fallback_streaks, self.__auth_streaks)
        started = time.monotonic()
        LOGGER.debug("[%s] Starting state check", uid)
        has_previous = previous is not None
        settings = await _fetch_or_keep_previous(
            ctx,
            "get_settings",
            lambda: client.async_get_settings(uid=uid),
            _Fallback(has_previous, previous.settings if previous else {}),
        )
        camera_time = await _fetch_or_keep_previous(
            ctx,
            "get_camera_time",
            lambda: client.async_get_camera_time(uid=uid),
            _Fallback(has_previous, previous.camera_time if previous else dt_util.utcnow()),
        )
        storage = await _fetch_or_keep_previous(
            ctx,
            "get_storage_state",
            lambda: client.async_get_storage_state(uid=uid),
            _Fallback(has_previous, previous.storage if previous else StorageState(used_mb=0, total_mb=0)),
        )
        record_quality = await _fetch_or_keep_previous(
            ctx,
            "get_record_quality",
            lambda: client.async_get_record_quality(uid=uid),
            _Fallback(has_previous, previous.record_quality if previous else None),
        )
        if abs((dt_util.utcnow() - camera_time).total_seconds()) > CLOCK_DRIFT_THRESHOLD_S:
            LOGGER.info("[%s] Camera clock drifted from %s, syncing", uid, camera_time)
            try:
                camera_time = await _call_with_retry(
                    probe, uid, "sync_time", lambda: client.async_sync_time(uid=uid), ctx.auth_streaks
                )
            except UpdateFailed as exception:
                LOGGER.warning("[%s] Camera clock sync failed, keeping the drifted value: %s", uid, exception)
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
        """Initialize, polling at `RECORDINGS_POLL_INTERVAL_S` (tighter, since it drives motion events)."""
        super().__init__(
            hass=hass,
            logger=LOGGER,
            config_entry=config_entry,
            name=f"{config_entry.title} recordings",
            update_interval=timedelta(seconds=RECORDINGS_POLL_INTERVAL_S),
            always_update=False,
        )
        self.__fallback_streaks: dict[str, int] = {}
        self.__auth_streaks: dict[str, int] = {}

    async def _async_update_data(self) -> list[Recording]:
        """Fetch the current recordings list; keeps the last known list if still failing after retries."""
        client = self.config_entry.runtime_data.client
        probe = _Probe(self.hass, self.config_entry.data[CONF_HOST])
        previous = self.data
        uid = uuid.uuid4().hex[:8]
        ctx = _FetchContext(probe, uid, self.__fallback_streaks, self.__auth_streaks)
        started = time.monotonic()
        LOGGER.debug("[%s] Starting recordings check", uid)
        recordings = await _fetch_or_keep_previous(
            ctx,
            "get_recordings",
            lambda: client.async_get_recordings(uid=uid),
            _Fallback(previous is not None, previous or []),
        )
        LOGGER.debug(
            "[%s] Finished recordings check in %.3fs (%d recordings)", uid, time.monotonic() - started, len(recordings)
        )
        return recordings
