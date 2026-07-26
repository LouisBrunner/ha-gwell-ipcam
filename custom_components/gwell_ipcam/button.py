"""Button platform for the Gwell IP Camera integration."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import async_get_platforms

from .api import map_ptz_direction
from .const import DOMAIN, LOGGER
from .coordinator import GwellIPCamCoordinator
from .entity import GwellIPCamDescribedEntity

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .data import GwellIPCamConfigEntry

_PTZ_KEYS = {"ptz_up", "ptz_down", "ptz_left", "ptz_right"}
_QUICK_RECORD_KEY = "quick_record"

BUTTON_DESCRIPTIONS: tuple[ButtonEntityDescription, ...] = (
    ButtonEntityDescription(
        key="format_sd_card",
        translation_key="format_sd_card",
        entity_registry_enabled_default=False,
        icon="mdi:sd",
    ),
    ButtonEntityDescription(
        key=_QUICK_RECORD_KEY,
        translation_key="quick_record",
        icon="mdi:record-circle",
    ),
    ButtonEntityDescription(
        key="sync_time",
        translation_key="sync_time",
        entity_registry_enabled_default=False,
        icon="mdi:clock-check-outline",
    ),
    ButtonEntityDescription(key="ptz_up", translation_key="ptz_up", icon="mdi:arrow-up-bold"),
    ButtonEntityDescription(key="ptz_down", translation_key="ptz_down", icon="mdi:arrow-down-bold"),
    ButtonEntityDescription(key="ptz_left", translation_key="ptz_left", icon="mdi:arrow-left-bold"),
    ButtonEntityDescription(key="ptz_right", translation_key="ptz_right", icon="mdi:arrow-right-bold"),
    ButtonEntityDescription(
        key="start_conversation", translation_key="start_conversation", icon="mdi:microphone-message"
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


class GwellIPCamButton(GwellIPCamDescribedEntity[GwellIPCamCoordinator, ButtonEntityDescription], ButtonEntity):
    """Button triggering a one-off action on the camera."""

    @property
    def icon(self) -> str | None:
        """Quick record swaps icon to show whether a session is currently active."""
        if self.entity_description.key != _QUICK_RECORD_KEY:
            return super().icon
        client = self.coordinator.config_entry.runtime_data.client
        return "mdi:stop-circle" if client.quick_record_active else "mdi:record-circle"

    @property
    def extra_state_attributes(self) -> dict[str, bool] | None:
        """Quick record exposes whether a session is currently active, since the button has no on/off state."""
        if self.entity_description.key != _QUICK_RECORD_KEY:
            return None
        client = self.coordinator.config_entry.runtime_data.client
        return {"active": client.quick_record_active}

    def _tag_quick_record_side_effects(self) -> None:
        """Attribute switch.record/select.record_mode's next state write to this button press in the logbook."""
        if self._context is None:
            return
        record_unique_id = f"{self.coordinator.config_entry.unique_id}_record"
        record_type_unique_id = f"{self.coordinator.config_entry.unique_id}_record_type"
        for platform in async_get_platforms(self.hass, DOMAIN):
            if platform.domain not in ("switch", "select"):
                continue
            for entity in platform.entities.values():
                if entity.unique_id in (record_unique_id, record_type_unique_id):
                    entity.async_set_context(self._context)

    async def async_press(self) -> None:
        """Handle the button press."""
        client = self.coordinator.config_entry.runtime_data.client
        key = self.entity_description.key
        uid = uuid.uuid4().hex[:8]
        LOGGER.debug("[%s] User pressed %s (%s)", uid, self.entity_id, key)

        if key in _PTZ_KEYS:
            direction = map_ptz_direction(key.removeprefix("ptz_"), self.coordinator.data.settings)
            await client.async_ptz(direction)
            return

        if key == "start_conversation":
            registry = er.async_get(self.hass)
            unique_id = f"{self.coordinator.config_entry.unique_id}_assist_satellite"
            entity_id = registry.async_get_entity_id("assist_satellite", DOMAIN, unique_id)
            if entity_id is not None:
                await self.hass.services.async_call(
                    "assist_satellite",
                    "start_conversation",
                    # An empty start_message still goes through TTS synthesis upstream and can fail on empty text.
                    {"entity_id": entity_id, "start_message": "Listening", "preannounce": False},
                )
            return

        match key:
            case "format_sd_card":
                await client.async_format_sd_card(uid=uid)
            case "quick_record":
                _active, fresh = await client.async_toggle_quick_record(
                    current_settings=self.coordinator.data.settings, uid=uid
                )
                self._tag_quick_record_side_effects()
                self.coordinator.apply_fresh_settings(fresh)
                self.async_write_ha_state()
                return
            case "sync_time":
                camera_time = await client.async_sync_time(uid=uid)
                self.coordinator.apply_fresh_camera_time(camera_time)
                return
            case _:
                raise NotImplementedError(key)
        await self.coordinator.async_request_refresh()
