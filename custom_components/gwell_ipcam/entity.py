"""Base entity for the Gwell IP Camera integration."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityDescription
from homeassistant.helpers.update_coordinator import CoordinatorEntity, DataUpdateCoordinator

from .const import DOMAIN

if TYPE_CHECKING:
    from .api import CameraIdentity


class GwellIPCamEntity[T: DataUpdateCoordinator[Any]](CoordinatorEntity[T]):
    """Base entity tying a camera's entities to its device entry."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: T, identity: CameraIdentity) -> None:
        """Initialize the entity."""
        super().__init__(coordinator)
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, identity.contact_id)},
            name=identity.name,
            manufacturer="Gwell",
            model=identity.model,
            sw_version=identity.firmware_version,
        )


class GwellIPCamDescribedEntity[T: DataUpdateCoordinator[Any], DescT: EntityDescription](GwellIPCamEntity[T]):
    """Base for entities driven by a declarative `EntityDescription`; derives the unique ID from its key."""

    entity_description: DescT

    def __init__(self, coordinator: T, identity: CameraIdentity, entity_description: DescT) -> None:
        """Initialize the entity, deriving its unique ID from `entity_description.key`."""
        super().__init__(coordinator, identity)
        self.entity_description = entity_description
        assert coordinator.config_entry is not None  # noqa: S101 -- always constructed with a real config entry
        self._attr_unique_id = f"{coordinator.config_entry.unique_id}_{entity_description.key}"
