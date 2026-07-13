"""Config flow tests using the HA test harness."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.service_info.dhcp import DhcpServiceInfo

from custom_components.gwell_ipcam.api import APIAuthError, CameraIdentity, DiscoveredCamera
from custom_components.gwell_ipcam.const import CONF_CONTACT_ID, DOMAIN

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant


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

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "IPCam-1283250"
    assert result["data"][CONF_CONTACT_ID] == "1283250"
    assert result["data"][CONF_HOST] == "192.168.0.66"


async def test_manual_flow_auth_error_shows_form_again(hass: HomeAssistant) -> None:
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

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "auth"}


async def test_discover_flow_aborts_when_nothing_found(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    with patch(
        "custom_components.gwell_ipcam.api.GwellIPCamClient.async_discover",
        AsyncMock(return_value=[]),
    ):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "discover"})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_devices_found"


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

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_HOST] == "192.168.0.66"
    assert result["data"][CONF_CONTACT_ID] == "1283250"


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
