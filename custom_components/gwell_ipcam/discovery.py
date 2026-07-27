"""Background UDP broadcast that turns newly-seen cameras into discovery flows."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.config_entries import SOURCE_INTEGRATION_DISCOVERY, ConfigEntryState
from homeassistant.const import CONF_HOST, CONF_NAME, CONF_PORT
from homeassistant.helpers import discovery_flow

from .api import GwellIPCamClient
from .const import CONF_CONTACT_ID, DISCOVERY_TIMEOUT_S, DOMAIN, LOGGER

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant


async def async_discover_and_trigger_flows(hass: HomeAssistant) -> None:
    """Broadcast for cameras; start a discovery flow for new ones, reload any known one that isn't loaded."""
    known_entries = {
        entry.data[CONF_CONTACT_ID]: entry
        for entry in hass.config_entries.async_entries(DOMAIN)
        if CONF_CONTACT_ID in entry.data
    }
    found = await GwellIPCamClient.async_discover(hass, timeout_s=DISCOVERY_TIMEOUT_S)
    for camera in found:
        entry = known_entries.get(camera.contact_id)
        if entry is None:
            discovery_flow.async_create_flow(
                hass,
                DOMAIN,
                context={"source": SOURCE_INTEGRATION_DISCOVERY},
                data={
                    CONF_HOST: camera.host,
                    CONF_PORT: camera.port,
                    CONF_CONTACT_ID: camera.contact_id,
                    CONF_NAME: camera.name,
                },
            )
        elif entry.state is not ConfigEntryState.LOADED:
            # Reappeared while stuck in setup-retry (or otherwise not loaded) -- don't wait out the backoff.
            LOGGER.debug("Camera %s reappeared, reloading %s", camera.contact_id, entry.entry_id)
            hass.config_entries.async_schedule_reload(entry.entry_id)
