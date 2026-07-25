"""Number platform for the Gwell IP Camera integration."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING

from homeassistant.components.number import NumberEntity, NumberEntityDescription, NumberMode
from homeassistant.const import UnitOfTime

from .api import SETTING_BUZZER, SETTING_MOTION_SENSITIVITY, SETTING_VIDEO_VOLUME
from .const import LOGGER
from .coordinator import GwellIPCamCoordinator
from .entity import GwellIPCamDescribedEntity

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .data import GwellIPCamConfigEntry

_RECORD_QUALITY_KEY = "record_quality"  # special-cased: separate iExtendedCmd wire path, not a settingType


@dataclass(frozen=True, kw_only=True)
class GwellIPCamNumberDescription(NumberEntityDescription):
    """Describes a number entity backed by a raw settingType value (or record quality, if setting_type is None)."""

    setting_type: int | None


NUMBER_DESCRIPTIONS: tuple[GwellIPCamNumberDescription, ...] = (
    GwellIPCamNumberDescription(
        key="video_volume",
        translation_key="video_volume",
        setting_type=SETTING_VIDEO_VOLUME,
        native_min_value=0,
        native_max_value=9,
        native_step=1,
        mode=NumberMode.SLIDER,
        icon="mdi:volume-high",
    ),
    GwellIPCamNumberDescription(
        key="buzzer_duration",
        translation_key="buzzer_duration",
        setting_type=SETTING_BUZZER,
        native_min_value=0,
        native_max_value=3,
        native_step=1,
        native_unit_of_measurement=UnitOfTime.MINUTES,
        mode=NumberMode.SLIDER,
        icon="mdi:bell-ring",
    ),
    GwellIPCamNumberDescription(
        key="motion_sensitivity",
        translation_key="motion_sensitivity",
        setting_type=SETTING_MOTION_SENSITIVITY,
        native_min_value=0,
        native_max_value=6,  # confirmed from the official app's AlarmSetActivity wheel bounds; lower = more sensitive
        native_step=1,
        mode=NumberMode.SLIDER,
        icon="mdi:tune",
    ),
    GwellIPCamNumberDescription(
        key=_RECORD_QUALITY_KEY,
        translation_key="record_quality",
        setting_type=None,
        native_min_value=0,
        native_max_value=4,
        native_step=1,
        mode=NumberMode.SLIDER,
        icon="mdi:quality-high",
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,  # noqa: ARG001
    entry: GwellIPCamConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the number platform."""
    data = entry.runtime_data
    async_add_entities(
        GwellIPCamNumber(coordinator=data.coordinator, identity=data.identity, entity_description=description)
        for description in NUMBER_DESCRIPTIONS
    )


class GwellIPCamNumber(
    GwellIPCamDescribedEntity[GwellIPCamCoordinator, GwellIPCamNumberDescription], NumberEntity
):
    """Number entity driven by a declarative description."""

    @property
    def native_value(self) -> float | None:
        """Return the current value."""
        if self.entity_description.setting_type is None:
            return self.coordinator.data.record_quality
        return self.coordinator.data.settings.get(self.entity_description.setting_type)

    async def async_set_native_value(self, value: float) -> None:
        """Write the value back to the camera."""
        uid = uuid.uuid4().hex[:8]
        LOGGER.debug("[%s] User set %s to %s", uid, self.entity_id, value)
        client = self.coordinator.config_entry.runtime_data.client
        if self.entity_description.setting_type is None:
            fresh = await client.async_set_record_quality(int(value), uid=uid)
            self.coordinator.apply_fresh_record_quality(fresh)
        else:
            fresh = await client.async_set_setting(self.entity_description.setting_type, int(value), uid=uid)
            self.coordinator.apply_fresh_settings(fresh)
