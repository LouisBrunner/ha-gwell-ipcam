"""System health support for the Gwell IP Camera integration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import callback

from .const import DOMAIN

if TYPE_CHECKING:
    from homeassistant.components import system_health
    from homeassistant.core import HomeAssistant


@callback
def async_register(
    hass: HomeAssistant,  # noqa: ARG001
    register: system_health.SystemHealthRegistration,
) -> None:
    """Register system health callbacks."""
    register.async_register_info(system_health_info)


async def system_health_info(hass: HomeAssistant) -> dict[str, int | str]:
    """Get info for the system health card."""
    entries = hass.config_entries.async_entries(DOMAIN)
    return {
        "cameras_configured": len(entries),
        "cameras_online": sum(1 for entry in entries if entry.state is ConfigEntryState.LOADED),
    }
