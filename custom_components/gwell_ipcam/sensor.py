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
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfInformation

from .api import SETTING_NET_TYPE
from .coordinator import GwellIPCamCoordinator, GwellIPCamRecordingsCoordinator
from .entity import GwellIPCamDescribedEntity, GwellIPCamEntity
from .media_source import media_source_identifier, stream_url

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
    extra_attributes_fn: Callable[[GwellIPCamState], dict[str, int]] | None = None


def _sd_card_percent_used(state: GwellIPCamState) -> float | None:
    total = state.storage.total_mb
    if not total:
        return None
    return round(state.storage.used_mb / total * 100, 1)


def _sd_card_mb_attributes(state: GwellIPCamState) -> dict[str, int]:
    return {"used_mb": state.storage.used_mb, "total_mb": state.storage.total_mb}


SENSOR_DESCRIPTIONS: tuple[GwellIPCamSensorDescription, ...] = (
    GwellIPCamSensorDescription(
        key="sd_card_usage",
        translation_key="sd_card_usage",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
        icon="mdi:harddisk",
        value_fn=_sd_card_percent_used,
        extra_attributes_fn=_sd_card_mb_attributes,
    ),
    GwellIPCamSensorDescription(
        key="sd_card_space_used",
        translation_key="sd_card_space_used",
        device_class=SensorDeviceClass.DATA_SIZE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfInformation.MEGABYTES,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda state: state.storage.used_mb,
    ),
    GwellIPCamSensorDescription(
        key="camera_time",
        translation_key="camera_time",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        icon="mdi:clock-outline",
        value_fn=lambda state: state.camera_time,
    ),
    GwellIPCamSensorDescription(
        key="sd_card_space_total",
        translation_key="sd_card_space_total",
        device_class=SensorDeviceClass.DATA_SIZE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfInformation.MEGABYTES,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda state: state.storage.total_mb,
    ),
    GwellIPCamSensorDescription(
        key="network_type",
        translation_key="network_type",
        device_class=SensorDeviceClass.ENUM,
        options=["wifi", "wired"],
        icon="mdi:network",
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


class GwellIPCamSensor(GwellIPCamDescribedEntity[GwellIPCamCoordinator, GwellIPCamSensorDescription], SensorEntity):
    """Generic sensor driven by a declarative description."""

    @property
    def native_value(self) -> str | int | float | datetime | None:
        """Delegate to the description's `value_fn`."""
        if self.coordinator.data is None:
            return None
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict[str, int] | None:
        """Delegate to the description's `extra_attributes_fn`, if it has one."""
        if self.entity_description.extra_attributes_fn is None or self.coordinator.data is None:
            return None
        return self.entity_description.extra_attributes_fn(self.coordinator.data)


class GwellIPCamRecordingsSensor(GwellIPCamEntity[GwellIPCamRecordingsCoordinator], SensorEntity):
    """Exposes the list of recorded files as JSON, without hitting the recorder."""

    _attr_translation_key = "recordings"
    _attr_icon = "mdi:filmstrip-box-multiple"
    # MEASUREMENT, not TOTAL_INCREASING: the count can drop as old recordings roll off the lookback window.
    _attr_state_class = SensorStateClass.MEASUREMENT
    _unrecorded_attributes = frozenset({"recordings"})

    def __init__(self, coordinator: GwellIPCamRecordingsCoordinator, identity: CameraIdentity) -> None:
        """Initialize, wiring to the recordings coordinator rather than the general state one."""
        super().__init__(coordinator, identity)
        self._attr_unique_id = f"{coordinator.config_entry.unique_id}_recordings"

    @property
    def native_value(self) -> int:
        """Return the number of known recordings."""
        return len(self.coordinator.data or [])

    @property
    def extra_state_attributes(self) -> dict[str, str | int | float]:
        """Return the latest recording's details (readable on the device page) plus the full list as JSON."""
        entry = self.coordinator.config_entry
        recordings = self.coordinator.data or []
        attributes: dict[str, str | int | float] = {
            "recordings": json.dumps(
                [
                    {
                        "recording_id": recording.recording_id,
                        "started_at": recording.started_at.isoformat(),
                        "duration_s": recording.duration.total_seconds(),
                        "tag": recording.tag,
                        "media_content_id": media_source_identifier(entry, recording.recording_id),
                        "stream_url": stream_url(entry, recording.recording_id),
                    }
                    for recording in recordings
                ]
            ),
        }
        if recordings:
            latest = max(recordings, key=lambda recording: recording.started_at)
            attributes |= {
                "latest_recording_id": latest.recording_id,
                "latest_started_at": latest.started_at.isoformat(),
                "latest_duration_s": latest.duration.total_seconds(),
                "latest_tag": latest.tag,
                "latest_media_content_id": media_source_identifier(entry, latest.recording_id),
                "latest_stream_url": stream_url(entry, latest.recording_id),
            }
        return attributes
