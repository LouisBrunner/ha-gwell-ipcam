"""Custom integration to integrate Gwell IP cameras with Home Assistant."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

from homeassistant.const import CONF_HOST, CONF_PORT, Platform
from homeassistant.core import callback
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryError, ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.event import async_track_time_interval

from .api import APIAuthError, APIConnectionError, APIError, GwellIPCamClient
from .const import CONF_PASSWORD_HASH, DISCOVERY_INTERVAL_S, DOMAIN
from .coordinator import GwellIPCamCoordinator, GwellIPCamRecordingsCoordinator
from .data import GwellIPCamData
from .discovery import async_discover_and_trigger_flows
from .motion_events import async_handle_recordings_update

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.typing import ConfigType

    from .data import GwellIPCamConfigEntry

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

PLATFORMS: list[Platform] = [
    Platform.ASSIST_SATELLITE,
    Platform.BUTTON,
    Platform.CALENDAR,
    Platform.CAMERA,
    Platform.EVENT,
    Platform.MEDIA_PLAYER,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
    Platform.TIME,
    Platform.UPDATE,
]


async def async_setup(hass: HomeAssistant, _config: ConfigType) -> bool:
    """Set up the integration and start background camera discovery."""

    @callback
    def _async_start_background_discovery(*_: object) -> None:
        hass.async_create_background_task(
            async_discover_and_trigger_flows(hass),
            "gwell_ipcam-discovery",
            eager_start=True,
        )

    _async_start_background_discovery()
    async_track_time_interval(
        hass,
        _async_start_background_discovery,
        timedelta(seconds=DISCOVERY_INTERVAL_S),
        cancel_on_shutdown=True,
    )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: GwellIPCamConfigEntry) -> bool:
    """Set up a Gwell IP camera from a config entry."""
    client = GwellIPCamClient(
        hass=hass,
        host=entry.data[CONF_HOST],
        port=entry.data[CONF_PORT],
        password_hash=entry.data[CONF_PASSWORD_HASH],
        entry_id=entry.entry_id,
    )
    await client.async_load_quick_record_state()

    try:
        identity = await client.async_get_identity()
    except APIAuthError as e:
        raise ConfigEntryAuthFailed(str(e)) from e
    except APIConnectionError as e:
        raise ConfigEntryNotReady(str(e)) from e
    except APIError as e:
        raise ConfigEntryError(str(e)) from e

    coordinator = GwellIPCamCoordinator(hass=hass, config_entry=entry)
    recordings_coordinator = GwellIPCamRecordingsCoordinator(hass=hass, config_entry=entry)

    entry.runtime_data = GwellIPCamData(
        client=client,
        identity=identity,
        coordinator=coordinator,
        recordings_coordinator=recordings_coordinator,
    )

    await coordinator.async_config_entry_first_refresh()
    await recordings_coordinator.async_config_entry_first_refresh()

    # Seed the baseline before listening, or every recording already on the SD card looks "new" on this boot.
    entry.runtime_data.known_recording_ids = {r.recording_id for r in recordings_coordinator.data or []}
    entry.async_on_unload(
        recordings_coordinator.async_add_listener(lambda: async_handle_recordings_update(hass, entry))
    )

    await client.async_start_streaming()
    entry.async_on_unload(client.async_stop_streaming)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))

    return True


async def async_unload_entry(hass: HomeAssistant, entry: GwellIPCamConfigEntry) -> bool:
    """Handle removal of an entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_reload_entry(hass: HomeAssistant, entry: GwellIPCamConfigEntry) -> None:
    """Reload config entry."""
    await hass.config_entries.async_reload(entry.entry_id)
