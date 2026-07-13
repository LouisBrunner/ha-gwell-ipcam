"""Update platform for the Gwell IP Camera integration."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, override

from homeassistant.components.update import UpdateEntity, UpdateEntityFeature

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
    """Reports and installs camera firmware updates."""

    _attr_translation_key = "firmware"
    _attr_supported_features = UpdateEntityFeature.INSTALL

    def __init__(self, coordinator: GwellIPCamCoordinator, identity: CameraIdentity) -> None:
        """Initialize the update entity."""
        super().__init__(coordinator, identity)
        self._attr_unique_id = f"{coordinator.config_entry.unique_id}_firmware"
        self._attr_installed_version = identity.firmware_version

    async def async_update(self) -> None:
        """Check for a new firmware version."""
        client = self.coordinator.config_entry.runtime_data.client
        info = await client.async_get_firmware_info()
        self._attr_latest_version = info.latest_version
        self._attr_release_summary = info.release_summary
        self._attr_release_url = info.release_url

    @override
    async def async_install(self, version: str | None, backup: bool, **kwargs: Any) -> None:
        """Install the available firmware update."""
        client = self.coordinator.config_entry.runtime_data.client
        await client.async_install_firmware_update()
        await self.coordinator.async_request_refresh()
