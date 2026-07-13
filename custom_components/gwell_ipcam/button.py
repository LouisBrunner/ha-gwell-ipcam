"""Button platform for the Gwell IP Camera integration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription

from .coordinator import GwellIPCamCoordinator
from .entity import GwellIPCamEntity

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .api import CameraIdentity
    from .data import GwellIPCamConfigEntry

BUTTON_DESCRIPTIONS: tuple[ButtonEntityDescription, ...] = (
    ButtonEntityDescription(
        key="format_sd_card",
        translation_key="format_sd_card",
        entity_registry_enabled_default=False,
    ),
    ButtonEntityDescription(
        key="quick_record",
        translation_key="quick_record",
    ),
    ButtonEntityDescription(
        key="sync_time",
        translation_key="sync_time",
        entity_registry_enabled_default=False,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,  # noqa: ARG001
    entry: GwellIPCamConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the button platform."""
    async_add_entities(
        GwellIPCamButton(
            coordinator=entry.runtime_data.coordinator,
            identity=entry.runtime_data.identity,
            entity_description=description,
        )
        for description in BUTTON_DESCRIPTIONS
    )


class GwellIPCamButton(GwellIPCamEntity[GwellIPCamCoordinator], ButtonEntity):
    """Button triggering a one-off action on the camera."""

    def __init__(
        self,
        coordinator: GwellIPCamCoordinator,
        identity: CameraIdentity,
        entity_description: ButtonEntityDescription,
    ) -> None:
        """Initialize the button."""
        super().__init__(coordinator, identity)
        self.entity_description = entity_description
        self._attr_unique_id = f"{coordinator.config_entry.unique_id}_{entity_description.key}"

    async def async_press(self) -> None:
        """Handle the button press."""
        client = self.coordinator.config_entry.runtime_data.client
        match self.entity_description.key:
            case "format_sd_card":
                await client.async_format_sd_card()
            case "quick_record":
                await client.async_toggle_quick_record()
            case "sync_time":
                await client.async_sync_time()
            case _:
                raise NotImplementedError(self.entity_description.key)
        await self.coordinator.async_request_refresh()
