"""Number platform for the Gwell IP Camera integration."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from homeassistant.components.number import NumberEntity, NumberEntityDescription, NumberMode
from homeassistant.const import UnitOfTime

from .api import SETTING_BUZZER, SETTING_MOTION_SENSITIVITY, SETTING_VIDEO_VOLUME
from .coordinator import GwellIPCamCoordinator
from .entity import GwellIPCamEntity

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .api import CameraIdentity
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
        entity_registry_enabled_default=False,
    ),
    GwellIPCamNumberDescription(
        key="motion_sensitivity",
        translation_key="motion_sensitivity",
        setting_type=SETTING_MOTION_SENSITIVITY,
        native_min_value=0,
        native_max_value=10,  # exact upper bound unconfirmed; lower = more sensitive
        native_step=1,
        mode=NumberMode.SLIDER,
    ),
    GwellIPCamNumberDescription(
        key=_RECORD_QUALITY_KEY,
        translation_key="record_quality",
        setting_type=None,
        native_min_value=0,
        native_max_value=4,
        native_step=1,
        mode=NumberMode.SLIDER,
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


class GwellIPCamNumber(GwellIPCamEntity[GwellIPCamCoordinator], NumberEntity):
    """Number entity driven by a declarative description."""

    entity_description: GwellIPCamNumberDescription

    def __init__(
        self,
        coordinator: GwellIPCamCoordinator,
        identity: CameraIdentity,
        entity_description: GwellIPCamNumberDescription,
    ) -> None:
        """Initialize the number entity."""
        super().__init__(coordinator, identity)
        self.entity_description = entity_description
        self._attr_unique_id = f"{coordinator.config_entry.unique_id}_{entity_description.key}"

    @property
    def native_value(self) -> float | None:
        """Return the current value."""
        if self.entity_description.setting_type is None:
            return self.coordinator.data.record_quality
        return self.coordinator.data.settings.get(self.entity_description.setting_type)

    async def async_set_native_value(self, value: float) -> None:
        """Write the value back to the camera."""
        client = self.coordinator.config_entry.runtime_data.client
        if self.entity_description.setting_type is None:
            await client.async_set_record_quality(int(value))
        else:
            await client.async_set_setting(self.entity_description.setting_type, int(value))
        await self.coordinator.async_request_refresh()
