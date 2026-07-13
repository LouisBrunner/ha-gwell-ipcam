"""
Background UDP broadcast discovery, chaining into integration-discovery flows.

Mirrors the pattern used by other local-UDP-broadcast integrations (flux_led,
wiz, lifx): once any camera is configured and this component's async_setup
has run, we keep periodically re-broadcasting in the background and hand any
newly-seen, not-yet-configured camera straight to a SOURCE_INTEGRATION_DISCOVERY
flow via the discovery_flow helper, which produces a "Discovered" card in
Settings > Devices without the user needing to click "Add Integration" again.

This only covers *subsequent* cameras: the very first camera on a fresh
install still has to go through the manual "Add Integration" flow, since
nothing about this component runs before it has at least one config entry
(or an OUI/hostname DHCP matcher in manifest.json, which we don't have data
for yet -- see README).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.config_entries import SOURCE_INTEGRATION_DISCOVERY
from homeassistant.const import CONF_HOST, CONF_NAME, CONF_PORT
from homeassistant.helpers import discovery_flow

from .api import GwellIPCamClient
from .const import CONF_CONTACT_ID, DISCOVERY_TIMEOUT_S, DOMAIN

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant


async def async_discover_and_trigger_flows(hass: HomeAssistant) -> None:
    """Broadcast for cameras and start a discovery flow for any unconfigured one."""
    already_configured = {
        entry.data[CONF_CONTACT_ID]
        for entry in hass.config_entries.async_entries(DOMAIN)
        if CONF_CONTACT_ID in entry.data
    }
    found = await GwellIPCamClient.async_discover(hass, timeout_s=DISCOVERY_TIMEOUT_S)
    for camera in found:
        if camera.contact_id in already_configured:
            continue
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
