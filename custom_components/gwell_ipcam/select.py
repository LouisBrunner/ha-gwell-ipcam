"""Select platform for the Gwell IP Camera integration."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING

from homeassistant.components.select import SelectEntity, SelectEntityDescription

from .api import SETTING_RECORD_TIME, SETTING_RECORD_TYPE, SETTING_VIDEO_FORMAT
from .const import LOGGER
from .coordinator import GwellIPCamCoordinator
from .entity import GwellIPCamDescribedEntity

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

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
        icon="mdi:record-circle-outline",
    ),
    GwellIPCamSelectDescription(
        key="video_format",
        translation_key="video_format",
        setting_type=SETTING_VIDEO_FORMAT,
        options=["ntsc", "pal"],
        value_to_option={0: "ntsc", 1: "pal"},
        icon="mdi:television-classic",
    ),
    GwellIPCamSelectDescription(
        key="record_time",
        translation_key="record_time",
        setting_type=SETTING_RECORD_TIME,
        options=["1", "2", "3"],
        icon="mdi:timer-outline",
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


class GwellIPCamSelect(GwellIPCamDescribedEntity[GwellIPCamCoordinator, GwellIPCamSelectDescription], SelectEntity):
    """Select entity driven by a declarative description."""

    @property
    def current_option(self) -> str | None:
        """Map the raw settingType value through `value_to_option`."""
        if self.coordinator.data is None:
            return None
        desc = self.entity_description
        value = self.coordinator.data.settings.get(desc.setting_type)
        return desc.value_to_option.get(value) if value is not None else None

    async def async_select_option(self, option: str) -> None:
        """Reverse-map `option` through `value_to_option` and write the raw settingType value."""
        uid = uuid.uuid4().hex[:8]
        LOGGER.debug("[%s] User set %s to %s", uid, self.entity_id, option)
        desc = self.entity_description
        value = next((v for v, o in desc.value_to_option.items() if o == option), None)
        if value is None:
            msg = f"{option!r} is not a valid option for {self.entity_id}"
            raise ValueError(msg)
        client = self.coordinator.config_entry.runtime_data.client
        fresh = await client.async_set_setting(desc.setting_type, value, uid=uid)
        self.coordinator.apply_fresh_settings(fresh)
