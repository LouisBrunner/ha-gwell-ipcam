"""Select platform for the Gwell IP Camera integration."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from homeassistant.components.select import SelectEntity, SelectEntityDescription

from .api import SETTING_RECORD_TIME, SETTING_RECORD_TYPE, SETTING_VIDEO_FORMAT
from .const import LOGGER
from .coordinator import GwellIPCamCoordinator
from .entity import GwellIPCamEntity

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .api import CameraIdentity
    from .data import GwellIPCamConfigEntry


@dataclass(frozen=True, kw_only=True)
class GwellIPCamSelectDescription(SelectEntityDescription):
    """Describes a select entity backed by a raw settingType value."""

    setting_type: int
    value_to_option: dict[int, str]


SELECT_DESCRIPTIONS: tuple[GwellIPCamSelectDescription, ...] = (
    GwellIPCamSelectDescription(
        key="record_type",
        translation_key="record_type",
        setting_type=SETTING_RECORD_TYPE,
        options=["manual", "alarm", "timing"],
        value_to_option={0: "manual", 1: "alarm", 2: "timing"},
    ),
    GwellIPCamSelectDescription(
        key="video_format",
        translation_key="video_format",
        setting_type=SETTING_VIDEO_FORMAT,
        options=["ntsc", "pal"],
        value_to_option={0: "ntsc", 1: "pal"},
    ),
    GwellIPCamSelectDescription(
        key="record_time",
        translation_key="record_time",
        setting_type=SETTING_RECORD_TIME,
        options=["1", "2", "3"],
        value_to_option={0: "1", 1: "2", 2: "3"},  # wire is 0-indexed, UI shows minutes
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,  # noqa: ARG001
    entry: GwellIPCamConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the select platform."""
    data = entry.runtime_data
    async_add_entities(
        GwellIPCamSelect(coordinator=data.coordinator, identity=data.identity, entity_description=description)
        for description in SELECT_DESCRIPTIONS
    )


class GwellIPCamSelect(GwellIPCamEntity[GwellIPCamCoordinator], SelectEntity):
    """Select entity driven by a declarative description."""

    entity_description: GwellIPCamSelectDescription

    def __init__(
        self,
        coordinator: GwellIPCamCoordinator,
        identity: CameraIdentity,
        entity_description: GwellIPCamSelectDescription,
    ) -> None:
        """Initialize the select entity."""
        super().__init__(coordinator, identity)
        self.entity_description = entity_description
        self._attr_unique_id = f"{coordinator.config_entry.unique_id}_{entity_description.key}"

    @property
    def current_option(self) -> str | None:
        """Return the current option."""
        value = self.coordinator.data.settings.get(self.entity_description.setting_type)
        return self.entity_description.value_to_option.get(value) if value is not None else None

    async def async_select_option(self, option: str) -> None:
        """Write the selected option back to the camera."""
        LOGGER.debug("User set %s to %s", self.entity_id, option)
        value_to_option = self.entity_description.value_to_option
        value = next(v for v, o in value_to_option.items() if o == option)
        client = self.coordinator.config_entry.runtime_data.client
        await client.async_set_setting(self.entity_description.setting_type, value)
        await self.coordinator.async_request_refresh()
