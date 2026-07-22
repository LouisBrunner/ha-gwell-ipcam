"""Switch platform for the Gwell IP Camera integration."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription

from .api import SETTING_DEFENCE_SWITCH, SETTING_IMAGE_FLIP, SETTING_MOTION_DETECT, SETTING_REMOTE_DEFENCE
from .const import LOGGER
from .coordinator import GwellIPCamCoordinator
from .entity import GwellIPCamDescribedEntity

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .data import GwellIPCamConfigEntry

_RECORD_KEY = "record"  # special-cased: driven by async_set_recording_state, not a raw settingType write


@dataclass(frozen=True, kw_only=True)
class GwellIPCamSwitchDescription(SwitchEntityDescription):
    """Describes a switch; `setting_type=None` marks the special-cased recording switch."""

    setting_type: int | None
    invert: bool = False


SWITCH_DESCRIPTIONS: tuple[GwellIPCamSwitchDescription, ...] = (
    GwellIPCamSwitchDescription(key=_RECORD_KEY, translation_key="record", icon="mdi:record-rec", setting_type=None),
    GwellIPCamSwitchDescription(
        key="alarm", translation_key="alarm", setting_type=SETTING_REMOTE_DEFENCE, icon="mdi:alarm-light"
    ),
    GwellIPCamSwitchDescription(
        key="motion_detect",
        translation_key="motion_detect",
        setting_type=SETTING_MOTION_DETECT,
        icon="mdi:motion-sensor",
    ),
    # ON means physically upside-down, which is wire value 0, hence invert=True.
    GwellIPCamSwitchDescription(
        key="upside_down",
        translation_key="upside_down",
        setting_type=SETTING_IMAGE_FLIP,
        icon="mdi:flip-vertical",
        invert=True,
    ),
    GwellIPCamSwitchDescription(
        key="defence_switch",
        translation_key="defence_switch",
        setting_type=SETTING_DEFENCE_SWITCH,
        icon="mdi:shield-home",
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,  # noqa: ARG001
    entry: GwellIPCamConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the switch platform."""
    async_add_entities(
        GwellIPCamSwitch(
            coordinator=entry.runtime_data.coordinator,
            identity=entry.runtime_data.identity,
            entity_description=description,
        )
        for description in SWITCH_DESCRIPTIONS
    )


class GwellIPCamSwitch(
    GwellIPCamDescribedEntity[GwellIPCamCoordinator, GwellIPCamSwitchDescription], SwitchEntity
):
    """Switch toggling a boolean setting on the camera."""

    @property
    def is_on(self) -> bool:
        """Return true if the setting is on."""
        desc = self.entity_description
        if desc.setting_type is None:
            return self.coordinator.data.recording
        raw_on = bool(self.coordinator.data.settings.get(desc.setting_type, 0))
        return not raw_on if desc.invert else raw_on

    async def async_turn_on(self, **_: Any) -> None:
        """Turn the setting on."""
        await self.__set(value=True)

    async def async_turn_off(self, **_: Any) -> None:
        """Turn the setting off."""
        await self.__set(value=False)

    async def __set(self, *, value: bool) -> None:
        uid = uuid.uuid4().hex[:8]
        LOGGER.debug("[%s] User set %s to %s", uid, self.entity_id, value)
        client = self.coordinator.config_entry.runtime_data.client
        desc = self.entity_description
        if desc.setting_type is None:
            await client.async_set_recording_state(enabled=value, uid=uid)
        else:
            raw_value = not value if desc.invert else value
            await client.async_set_setting(desc.setting_type, int(raw_value), uid=uid)
        await self.coordinator.async_request_refresh()
