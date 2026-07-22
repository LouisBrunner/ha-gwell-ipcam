"""Config flow tests using the HA test harness."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_NAME, CONF_PASSWORD, CONF_PORT
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.service_info.dhcp import DhcpServiceInfo

from custom_components.gwell_ipcam.api import APIAuthError, APIConnectionError, CameraIdentity, DiscoveredCamera
from custom_components.gwell_ipcam.const import CONF_CONTACT_ID, CONF_PASSWORD_HASH, DEFAULT_PORT, DOMAIN

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant


async def _resolve_progress(
    hass: HomeAssistant, result: config_entries.ConfigFlowResult
) -> config_entries.ConfigFlowResult:
    """Poll a SHOW_PROGRESS flow to its terminal result (the task may finish eagerly or not)."""
    while result["type"] is FlowResultType.SHOW_PROGRESS:
        result = await hass.config_entries.flow.async_configure(result["flow_id"])
    return result


async def test_manual_flow_creates_entry(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "manual"})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "manual"

    identity = CameraIdentity(
        contact_id="1283250", name="IPCam-1283250", model="Sricam/ieGeek IP Camera", firmware_version="21.0.0.30"
    )
    with patch(
        "custom_components.gwell_ipcam.api.GwellIPCamClient.async_check_connection",
        AsyncMock(return_value=identity),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_HOST: "192.168.0.66", CONF_PORT: 51880, CONF_PASSWORD: "camtest12"},
        )
        result = await _resolve_progress(hass, result)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "IPCam-1283250"
    assert result["data"] == {
        CONF_HOST: "192.168.0.66",
        CONF_PORT: 51880,
        CONF_PASSWORD_HASH: "636734832",
        CONF_CONTACT_ID: "1283250",
        CONF_NAME: "IPCam-1283250",
    }


async def test_manual_flow_auth_error_keeps_form_values(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "manual"})

    with patch(
        "custom_components.gwell_ipcam.api.GwellIPCamClient.async_check_connection",
        AsyncMock(side_effect=APIAuthError("rejected")),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_HOST: "192.168.0.66", CONF_PORT: 51880, CONF_PASSWORD: "wrong"},
        )
        result = await _resolve_progress(hass, result)

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "auth"}
    assert result["description_placeholders"] == {"error": "rejected"}
    data_schema = result["data_schema"]
    assert data_schema is not None
    defaults = {marker.schema: marker.default() for marker in data_schema.schema if marker.default is not vol.UNDEFINED}
    assert defaults[CONF_HOST] == "192.168.0.66"
    assert defaults[CONF_PORT] == 51880
    assert defaults[CONF_PASSWORD] == "wrong"


async def test_discover_flow_aborts_when_nothing_found(hass: HomeAssistant) -> None:
    with patch(
        "custom_components.gwell_ipcam.api.GwellIPCamClient.async_discover",
        AsyncMock(return_value=[]),
    ):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "discover"})
        result = await _resolve_progress(hass, result)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_devices_found"


async def test_discover_flow_shows_camera_list(hass: HomeAssistant) -> None:
    camera = DiscoveredCamera(host="192.168.0.66", port=DEFAULT_PORT, contact_id="1283250", name="IPCam-1283250")
    with patch(
        "custom_components.gwell_ipcam.api.GwellIPCamClient.async_discover",
        AsyncMock(return_value=[camera]),
    ):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "discover"})
        result = await _resolve_progress(hass, result)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "discover"


async def test_discover_flow_surfaces_broadcast_failure(hass: HomeAssistant) -> None:
    with patch(
        "custom_components.gwell_ipcam.api.GwellIPCamClient.async_discover",
        AsyncMock(side_effect=APIConnectionError("Name or service not known")),
    ):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "discover"})
        result = await _resolve_progress(hass, result)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "discovery_failed"
    assert result["description_placeholders"] == {"error": "Name or service not known"}


async def test_dhcp_flow_creates_entry(hass: HomeAssistant) -> None:
    camera = DiscoveredCamera(host="192.168.0.66", port=51880, contact_id="1283250", name="IPCam-1283250")
    discovery_info = DhcpServiceInfo(ip="192.168.0.66", hostname="ipcam", macaddress="4cb0088a361a")
    with patch(
        "custom_components.gwell_ipcam.api.GwellIPCamClient.async_discover_one",
        AsyncMock(return_value=camera),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_DHCP}, data=discovery_info
        )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "discover_password"

    identity = CameraIdentity(
        contact_id="1283250", name="IPCam-1283250", model="Sricam/ieGeek IP Camera", firmware_version="21.0.0.30"
    )
    with patch(
        "custom_components.gwell_ipcam.api.GwellIPCamClient.async_check_connection",
        AsyncMock(return_value=identity),
    ):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_PASSWORD: "camtest12"})
        result = await _resolve_progress(hass, result)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {
        CONF_HOST: "192.168.0.66",
        CONF_PORT: 51880,
        CONF_PASSWORD_HASH: "636734832",
        CONF_CONTACT_ID: "1283250",
        CONF_NAME: "IPCam-1283250",
    }


async def test_dhcp_flow_aborts_when_camera_not_reachable(hass: HomeAssistant) -> None:
    discovery_info = DhcpServiceInfo(ip="192.168.0.66", hostname="ipcam", macaddress="4cb0088a361a")
    with patch(
        "custom_components.gwell_ipcam.api.GwellIPCamClient.async_discover_one",
        AsyncMock(return_value=None),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_DHCP}, data=discovery_info
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_devices_found"


@pytest.fixture(autouse=True)
def _mock_setup_entry():
    with patch("custom_components.gwell_ipcam.async_setup_entry", AsyncMock(return_value=True)):
        yield


@pytest.fixture(autouse=True)
def _mock_background_discovery():
    """async_setup() fires a background UDP broadcast; keep it off the real network during tests."""
    with patch("custom_components.gwell_ipcam.api.GwellIPCamClient.async_discover", AsyncMock(return_value=[])):
        yield
