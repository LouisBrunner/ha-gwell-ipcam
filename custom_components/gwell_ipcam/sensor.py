"""Sensor platform for the Gwell IP Camera integration."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import EntityCategory, UnitOfInformation

from .api import SETTING_NET_TYPE
from .coordinator import GwellIPCamCoordinator, GwellIPCamRecordingsCoordinator
from .entity import GwellIPCamEntity

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime

    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .api import CameraIdentity
    from .coordinator import GwellIPCamState
    from .data import GwellIPCamConfigEntry


@dataclass(frozen=True, kw_only=True)
class GwellIPCamSensorDescription(SensorEntityDescription):
    """Describes a sensor sourced from the general state coordinator."""

    value_fn: Callable[[GwellIPCamState], str | int | float | datetime | None]


SENSOR_DESCRIPTIONS: tuple[GwellIPCamSensorDescription, ...] = (
    GwellIPCamSensorDescription(
        key="sd_card_space_used",
        translation_key="sd_card_space_used",
        device_class=SensorDeviceClass.DATA_SIZE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfInformation.MEGABYTES,
        value_fn=lambda state: state.storage.used_mb,
    ),
    GwellIPCamSensorDescription(
        key="camera_time",
        translation_key="camera_time",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda state: state.camera_time,
    ),
    GwellIPCamSensorDescription(
        key="sd_card_space_total",
        translation_key="sd_card_space_total",
        device_class=SensorDeviceClass.DATA_SIZE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfInformation.MEGABYTES,
        value_fn=lambda state: state.storage.total_mb,
    ),
    GwellIPCamSensorDescription(
        key="network_type",
        translation_key="network_type",
        device_class=SensorDeviceClass.ENUM,
        options=["wifi", "wired"],
        value_fn=lambda state: "wifi" if state.settings.get(SETTING_NET_TYPE) else "wired",
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,  # noqa: ARG001
    entry: GwellIPCamConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the sensor platform."""
    data = entry.runtime_data
    async_add_entities(
        [
            *(
                GwellIPCamSensor(coordinator=data.coordinator, identity=data.identity, entity_description=description)
                for description in SENSOR_DESCRIPTIONS
            ),
            GwellIPCamRecordingsSensor(coordinator=data.recordings_coordinator, identity=data.identity),
        ]
    )


class GwellIPCamSensor(GwellIPCamEntity[GwellIPCamCoordinator], SensorEntity):
    """Generic sensor driven by a declarative description."""

    entity_description: GwellIPCamSensorDescription

    def __init__(
        self,
        coordinator: GwellIPCamCoordinator,
        identity: CameraIdentity,
        entity_description: GwellIPCamSensorDescription,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, identity)
        self.entity_description = entity_description
        self._attr_unique_id = f"{coordinator.config_entry.unique_id}_{entity_description.key}"

    @property
    def native_value(self) -> str | int | float | datetime | None:
        """Return the sensor's value."""
        return self.entity_description.value_fn(self.coordinator.data)


class GwellIPCamRecordingsSensor(GwellIPCamEntity[GwellIPCamRecordingsCoordinator], SensorEntity):
    """Exposes the list of recorded files as JSON, without hitting the recorder."""

    _attr_translation_key = "recordings"
    _unrecorded_attributes = frozenset({"recordings"})

    def __init__(self, coordinator: GwellIPCamRecordingsCoordinator, identity: CameraIdentity) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, identity)
        self._attr_unique_id = f"{coordinator.config_entry.unique_id}_recordings"

    @property
    def native_value(self) -> int:
        """Return the number of known recordings."""
        return len(self.coordinator.data or [])

    @property
    def extra_state_attributes(self) -> dict[str, str]:
        """Return the recordings list as JSON, excluded from recorder history."""
        recordings = self.coordinator.data or []
        return {
            "recordings": json.dumps(
                [
                    {
                        "recording_id": recording.recording_id,
                        "started_at": recording.started_at.isoformat(),
                        "duration_s": recording.duration.total_seconds(),
                        "motion_triggered": recording.motion_triggered,
                    }
                    for recording in recordings
                ]
            ),
        }
