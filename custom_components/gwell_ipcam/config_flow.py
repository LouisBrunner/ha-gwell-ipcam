"""Config flow for the Gwell IP Camera integration."""

from __future__ import annotations

from typing import TYPE_CHECKING

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_NAME, CONF_PASSWORD, CONF_PORT
from homeassistant.helpers import selector

from .api import APIAuthError, APIConnectionError, APIError, CameraIdentity, DiscoveredCamera, GwellIPCamClient
from .const import CONF_CONTACT_ID, CONF_PASSWORD_HASH, DISCOVERY_TIMEOUT_S, DOMAIN, LOGGER

if TYPE_CHECKING:
    from homeassistant.helpers.service_info.dhcp import DhcpServiceInfo
    from homeassistant.helpers.typing import DiscoveryInfoType

_CONF_DEVICE = "device"


def _password_schema() -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_PASSWORD): selector.TextSelector(
                selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD),
            ),
        },
    )


def _manual_schema(*, host: str = "", port: int = 80, password_required: bool = True) -> vol.Schema:
    schema = {
        vol.Required(CONF_HOST, default=host): selector.TextSelector(),
        vol.Required(CONF_PORT, default=port): selector.NumberSelector(
            selector.NumberSelectorConfig(min=1, max=65535, mode=selector.NumberSelectorMode.BOX),
        ),
    }
    password_key = vol.Required(CONF_PASSWORD) if password_required else vol.Optional(CONF_PASSWORD)
    schema[password_key] = selector.TextSelector(
        selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD),
    )
    return vol.Schema(schema)


class GwellIPCamFlowHandler(config_entries.ConfigFlow, domain=DOMAIN):
    """Config flow for Gwell IP cameras."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the flow."""
        self.__discovered: dict[str, DiscoveredCamera] = {}
        self.__chosen: DiscoveredCamera | None = None

    async def async_step_user(self, user_input: dict | None = None) -> config_entries.ConfigFlowResult:  # noqa: ARG002
        """Let the user pick between auto-discovery and manual entry."""
        return self.async_show_menu(step_id="user", menu_options=["discover", "manual"])

    async def async_step_discover(self, user_input: dict | None = None) -> config_entries.ConfigFlowResult:
        """Broadcast a discovery request and let the user pick a camera."""
        errors: dict[str, str] = {}

        if user_input is not None:
            self.__chosen = self.__discovered[user_input[_CONF_DEVICE]]
            return await self.async_step_discover_password()

        already_configured = {
            entry.data[CONF_CONTACT_ID] for entry in self._async_current_entries() if CONF_CONTACT_ID in entry.data
        }
        found = await GwellIPCamClient.async_discover(self.hass, timeout_s=DISCOVERY_TIMEOUT_S)
        self.__discovered = {
            camera.contact_id: camera for camera in found if camera.contact_id not in already_configured
        }

        if not self.__discovered:
            return self.async_abort(reason="no_devices_found")

        return self.async_show_form(
            step_id="discover",
            data_schema=vol.Schema(
                {
                    vol.Required(_CONF_DEVICE): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=[
                                selector.SelectOptionDict(
                                    value=camera.contact_id,
                                    label=f"{camera.name} ({camera.host}:{camera.port})",
                                )
                                for camera in self.__discovered.values()
                            ],
                            mode=selector.SelectSelectorMode.LIST,
                        ),
                    ),
                },
            ),
            errors=errors,
        )

    async def async_step_integration_discovery(
        self, discovery_info: DiscoveryInfoType
    ) -> config_entries.ConfigFlowResult:
        """
        Handle a camera found by our own periodic background broadcast (see discovery.py).

        Unlike async_step_discover, host/port/contact_id/name are already
        known here, so we skip straight to asking for the password.
        """
        camera = DiscoveredCamera(
            host=discovery_info[CONF_HOST],
            port=discovery_info[CONF_PORT],
            contact_id=discovery_info[CONF_CONTACT_ID],
            name=discovery_info[CONF_NAME],
        )
        await self.async_set_unique_id(camera.contact_id)
        self._abort_if_unique_id_configured()
        self.context["title_placeholders"] = {"name": camera.name}
        self.__chosen = camera
        return await self.async_step_discover_password()

    async def async_step_dhcp(self, discovery_info: DhcpServiceInfo) -> config_entries.ConfigFlowResult:
        """
        Handle a camera found via its DHCP lease (manifest.json's OUI matcher).

        DHCP only gives us an IP -- contact_id has to be pulled from the
        camera itself via our own discovery request, unicast at that IP.
        """
        camera = await GwellIPCamClient.async_discover_one(self.hass, discovery_info.ip)
        if camera is None:
            return self.async_abort(reason="no_devices_found")
        await self.async_set_unique_id(camera.contact_id)
        self._abort_if_unique_id_configured(updates={CONF_HOST: camera.host, CONF_PORT: camera.port})
        self.context["title_placeholders"] = {"name": camera.name}
        self.__chosen = camera
        return await self.async_step_discover_password()

    async def async_step_discover_password(self, user_input: dict | None = None) -> config_entries.ConfigFlowResult:
        """Ask for the camera's password once a discovered device is chosen."""
        assert self.__chosen is not None  # noqa: S101
        errors: dict[str, str] = {}

        if user_input is not None:
            camera = self.__chosen
            password_hash = GwellIPCamClient.hash_password(user_input[CONF_PASSWORD])
            try:
                identity = await self.__check_connection(camera.host, camera.port, password_hash)
            except _FlowError as exception:
                errors["base"] = exception.reason
            else:
                return await self.__finish(
                    host=camera.host,
                    port=camera.port,
                    password_hash=password_hash,
                    identity=identity,
                )

        return self.async_show_form(
            step_id="discover_password",
            data_schema=_password_schema(),
            description_placeholders={"name": self.__chosen.name},
            errors=errors,
        )

    async def async_step_manual(self, user_input: dict | None = None) -> config_entries.ConfigFlowResult:
        """Handle manual host/port/password entry."""
        errors: dict[str, str] = {}

        if user_input is not None:
            password_hash = GwellIPCamClient.hash_password(user_input[CONF_PASSWORD])
            try:
                identity = await self.__check_connection(user_input[CONF_HOST], user_input[CONF_PORT], password_hash)
            except _FlowError as exception:
                errors["base"] = exception.reason
            else:
                return await self.__finish(
                    host=user_input[CONF_HOST],
                    port=user_input[CONF_PORT],
                    password_hash=password_hash,
                    identity=identity,
                )

        return self.async_show_form(step_id="manual", data_schema=_manual_schema(), errors=errors)

    async def async_step_reconfigure(self, user_input: dict | None = None) -> config_entries.ConfigFlowResult:
        """Let the user edit host/port/password for an existing camera."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}

        if user_input is not None:
            password = user_input.get(CONF_PASSWORD)
            password_hash = GwellIPCamClient.hash_password(password) if password else entry.data[CONF_PASSWORD_HASH]
            try:
                identity = await self.__check_connection(user_input[CONF_HOST], user_input[CONF_PORT], password_hash)
            except _FlowError as exception:
                errors["base"] = exception.reason
            else:
                self._abort_if_unique_id_mismatch()
                # keep the existing title: identity.name is a synthesized
                # placeholder (the protocol has no camera-name field), not
                # something that should overwrite a user's chosen title
                return self.async_update_reload_and_abort(
                    entry,
                    title=entry.title,
                    data_updates={
                        CONF_HOST: user_input[CONF_HOST],
                        CONF_PORT: user_input[CONF_PORT],
                        CONF_PASSWORD_HASH: password_hash,
                        CONF_CONTACT_ID: identity.contact_id,
                    },
                )

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_manual_schema(
                host=entry.data[CONF_HOST],
                port=entry.data[CONF_PORT],
                password_required=False,
            ),
            errors=errors,
        )

    async def __check_connection(self, host: str, port: int, password_hash: str) -> CameraIdentity:
        try:
            return await GwellIPCamClient.async_check_connection(self.hass, host, port, password_hash)
        except APIAuthError as exception:
            LOGGER.warning(exception)
            reason = "auth"
            raise _FlowError(reason) from exception
        except APIConnectionError as exception:
            LOGGER.warning(exception)
            reason = "connection"
            raise _FlowError(reason) from exception
        except APIError as exception:
            LOGGER.exception("Unexpected error while checking camera connection")
            reason = "unknown"
            raise _FlowError(reason) from exception

    async def __finish(
        self, *, host: str, port: int, password_hash: str, identity: CameraIdentity
    ) -> config_entries.ConfigFlowResult:
        await self.async_set_unique_id(identity.contact_id)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title=identity.name,
            data={
                CONF_HOST: host,
                CONF_PORT: port,
                CONF_PASSWORD_HASH: password_hash,
                CONF_CONTACT_ID: identity.contact_id,
                CONF_NAME: identity.name,
            },
        )


class _FlowError(Exception):
    """Internal signal carrying the translation key of a failed step."""

    def __init__(self, reason: str) -> None:
        """Initialize with the translation key to show as an error."""
        super().__init__(reason)
        self.reason = reason
