"""Diagnostics support for the Gwell IP Camera integration."""

from __future__ import annotations

from dataclasses import asdict
from typing import TYPE_CHECKING, Any

from homeassistant.components.diagnostics import async_redact_data

from .const import CONF_PASSWORD_HASH

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .data import GwellIPCamConfigEntry

TO_REDACT = {CONF_PASSWORD_HASH}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant,  # noqa: ARG001
    entry: GwellIPCamConfigEntry,
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    data = entry.runtime_data
    return {
        "entry_data": async_redact_data(dict(entry.data), TO_REDACT),
        "identity": asdict(data.identity),
        "state": asdict(data.coordinator.data) if data.coordinator.data else None,
        "recordings_count": len(data.recordings_coordinator.data or []),
    }
