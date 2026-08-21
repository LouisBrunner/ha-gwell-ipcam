"""Update platform for the Gwell IP Camera integration."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, Any, override

from homeassistant.components.update import UpdateEntity
from homeassistant.helpers.event import async_track_time_interval

from .api import APIError
from .const import FIRMWARE_CHECK_INTERVAL_S, LOGGER
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
    """Set up the update platform."""
    async_add_entities(
        [
            GwellIPCamFirmwareUpdate(
                coordinator=entry.runtime_data.coordinator,
                identity=entry.runtime_data.identity,
            )
        ]
    )


class GwellIPCamFirmwareUpdate(GwellIPCamEntity[GwellIPCamCoordinator], UpdateEntity):
    """Reports available camera firmware updates; installing is not supported (no known wire command exists)."""

    _attr_translation_key = "firmware"

    def __init__(self, coordinator: GwellIPCamCoordinator, identity: CameraIdentity) -> None:
        """Initialize the update entity."""
        super().__init__(coordinator, identity)
        self._attr_unique_id = f"{coordinator.config_entry.unique_id}_firmware"
        self._attr_installed_version = identity.firmware_version
        self.__last_check_ok = identity.model is not None

    async def async_added_to_hass(self) -> None:
        """Check for updates once now and on a daily timer (CoordinatorEntity hard-codes should_poll=False)."""
        await super().async_added_to_hass()
        initial_check_task = self.hass.async_create_background_task(
            self.__async_check_for_update(None), f"{self.unique_id}-initial-firmware-check"
        )
        self.async_on_remove(initial_check_task.cancel)
        self.async_on_remove(
            async_track_time_interval(
                self.hass, self.__async_check_for_update, timedelta(seconds=FIRMWARE_CHECK_INTERVAL_S)
            )
        )

    async def __async_check_for_update(self, _now: object) -> None:
        await self.async_update()
        self.async_write_ha_state()

    async def async_update(self) -> None:
        """Check for a new firmware version; called both on startup and by the periodic timer."""
        client = self.coordinator.config_entry.runtime_data.client
        try:
            info = await client.async_get_firmware_info()
        except APIError as e:
            if self.__last_check_ok:
                LOGGER.warning("%s: firmware check failed, keeping the last known value: %s", self.entity_id, e)
            else:
                LOGGER.debug("%s: firmware check failed, keeping the last known value: %s", self.entity_id, e)
            self.__last_check_ok = False
            return
        if not self.__last_check_ok:
            LOGGER.info("%s: firmware check recovered", self.entity_id)
        self.__last_check_ok = True
        self._attr_latest_version = info.latest_version
        self._attr_release_summary = info.release_summary
        self._attr_release_url = info.release_url

    @override
    async def async_install(self, version: str | None, backup: bool, **kwargs: Any) -> None:
        """Not reachable via the UI/service (no INSTALL feature declared); always raises `APIError` if called."""
        client = self.coordinator.config_entry.runtime_data.client
        await client.async_install_firmware_update()
        await self.coordinator.async_request_refresh()
