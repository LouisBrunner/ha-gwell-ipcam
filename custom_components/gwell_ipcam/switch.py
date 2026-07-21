"""Switch platform for the Gwell IP Camera integration."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription

from .api import SETTING_DEFENCE_SWITCH, SETTING_IMAGE_FLIP, SETTING_MOTION_DETECT, SETTING_REMOTE_DEFENCE
from .const import LOGGER
from .coordinator import GwellIPCamCoordinator
from .entity import GwellIPCamEntity

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .api import CameraIdentity
    from .data import GwellIPCamConfigEntry

_RECORD_KEY = "record"  # special-cased: driven by async_set_recording_state, not a raw settingType write


@dataclass(frozen=True, kw_only=True)
class GwellIPCamSwitchDescription(SwitchEntityDescription):
    """Describes a switch toggling a raw settingType value."""

    setting_type: int


SWITCH_DESCRIPTIONS: tuple[SwitchEntityDescription | GwellIPCamSwitchDescription, ...] = (
    SwitchEntityDescription(key=_RECORD_KEY, translation_key="record"),
    GwellIPCamSwitchDescription(key="alarm", translation_key="alarm", setting_type=SETTING_REMOTE_DEFENCE),
    GwellIPCamSwitchDescription(
        key="motion_detect", translation_key="motion_detect", setting_type=SETTING_MOTION_DETECT
    ),
    GwellIPCamSwitchDescription(key="image_flip", translation_key="image_flip", setting_type=SETTING_IMAGE_FLIP),
    GwellIPCamSwitchDescription(
        key="defence_switch", translation_key="defence_switch", setting_type=SETTING_DEFENCE_SWITCH
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


class GwellIPCamSwitch(GwellIPCamEntity[GwellIPCamCoordinator], SwitchEntity):
    """Switch toggling a boolean setting on the camera."""

    entity_description: SwitchEntityDescription | GwellIPCamSwitchDescription

    def __init__(
        self,
        coordinator: GwellIPCamCoordinator,
        identity: CameraIdentity,
        entity_description: SwitchEntityDescription | GwellIPCamSwitchDescription,
    ) -> None:
        """Initialize the switch."""
        super().__init__(coordinator, identity)
        self.entity_description = entity_description
        self._attr_unique_id = f"{coordinator.config_entry.unique_id}_{entity_description.key}"

    @property
    def is_on(self) -> bool:
        """Return true if the setting is on."""
        if self.entity_description.key == _RECORD_KEY:
            return self.coordinator.data.recording
        desc = self.entity_description
        assert isinstance(desc, GwellIPCamSwitchDescription)  # noqa: S101
        return bool(self.coordinator.data.settings.get(desc.setting_type, 0))

    async def async_turn_on(self, **_: Any) -> None:
        """Turn the setting on."""
        await self.__set(value=True)

    async def async_turn_off(self, **_: Any) -> None:
        """Turn the setting off."""
        await self.__set(value=False)

    async def __set(self, *, value: bool) -> None:
        LOGGER.debug("User set %s to %s", self.entity_id, value)
        client = self.coordinator.config_entry.runtime_data.client
        if self.entity_description.key == _RECORD_KEY:
            await client.async_set_recording_state(enabled=value)
        else:
            desc = self.entity_description
            assert isinstance(desc, GwellIPCamSwitchDescription)  # noqa: S101
            await client.async_set_setting(desc.setting_type, int(value))
        await self.coordinator.async_request_refresh()
