"""Time platform for the Gwell IP Camera integration."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import time as dtime
from typing import TYPE_CHECKING, Literal

from homeassistant.components.time import TimeEntity, TimeEntityDescription

from .api import SETTING_RECORD_PLAN_TIME, decode_record_plan_time
from .const import LOGGER
from .coordinator import GwellIPCamCoordinator
from .entity import GwellIPCamDescribedEntity

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .data import GwellIPCamConfigEntry


@dataclass(frozen=True, kw_only=True)
class GwellIPCamTimeDescription(TimeEntityDescription):
    """Describes one endpoint of the Timing record schedule (`SETTING_RECORD_PLAN_TIME`)."""

    endpoint: Literal["start", "end"]


TIME_DESCRIPTIONS: tuple[GwellIPCamTimeDescription, ...] = (
    GwellIPCamTimeDescription(
        key="record_schedule_start",
        translation_key="record_schedule_start",
        endpoint="start",
        icon="mdi:clock-start",
    ),
    GwellIPCamTimeDescription(
        key="record_schedule_end",
        translation_key="record_schedule_end",
        endpoint="end",
        icon="mdi:clock-end",
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,  # noqa: ARG001
    entry: GwellIPCamConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the time platform."""
    data = entry.runtime_data
    async_add_entities(
        GwellIPCamTime(coordinator=data.coordinator, identity=data.identity, entity_description=description)
        for description in TIME_DESCRIPTIONS
    )


class GwellIPCamTime(GwellIPCamDescribedEntity[GwellIPCamCoordinator, GwellIPCamTimeDescription], TimeEntity):
    """Time entity for one `GwellIPCamTimeDescription`."""

    @property
    def native_value(self) -> dtime | None:
        """Return the current value, or None if unset or never configured on the camera."""
        value = self.coordinator.data.settings.get(SETTING_RECORD_PLAN_TIME)
        decoded = decode_record_plan_time(value) if value is not None else None
        if decoded is None:
            return None
        start, end = decoded
        return start if self.entity_description.endpoint == "start" else end

    async def async_set_value(self, value: dtime) -> None:
        """Write the value back to the camera; the other endpoint is sent unchanged."""
        uid = uuid.uuid4().hex[:8]
        LOGGER.debug("[%s] User set %s to %s", uid, self.entity_id, value)
        current = self.coordinator.data.settings.get(SETTING_RECORD_PLAN_TIME, 0)
        start, end = decode_record_plan_time(current) or (dtime(0, 0), dtime(0, 0))
        client = self.coordinator.config_entry.runtime_data.client
        if self.entity_description.endpoint == "start":
            fresh = await client.async_set_record_plan(value, end, uid=uid)
        else:
            fresh = await client.async_set_record_plan(start, value, uid=uid)
        self.coordinator.apply_fresh_settings(fresh)
