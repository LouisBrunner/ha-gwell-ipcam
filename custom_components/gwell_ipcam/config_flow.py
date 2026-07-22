"""Config flow for the Gwell IP Camera integration."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_NAME, CONF_PASSWORD, CONF_PORT
from homeassistant.helpers import selector

from .api import APIAuthError, APIConnectionError, APIError, CameraIdentity, DiscoveredCamera, GwellIPCamClient
from .const import CONF_CONTACT_ID, CONF_PASSWORD_HASH, DEFAULT_PORT, DISCOVERY_TIMEOUT_S, DOMAIN, LOGGER

if TYPE_CHECKING:
    import asyncio
    from collections.abc import Awaitable, Callable, Mapping
    from typing import Any

    from homeassistant.helpers.service_info.dhcp import DhcpServiceInfo
    from homeassistant.helpers.typing import DiscoveryInfoType

_CONF_DEVICE = "device"


@dataclass
class _ConnectStepSpec:
    """Per-step pieces of the shared discover_password/manual/reconfigure flow."""

    schema: vol.Schema
    placeholders: dict[str, str]
    start: Callable[[dict], tuple[str, int, str]]
    finish: Callable[[CameraIdentity], Awaitable[config_entries.ConfigFlowResult]]


def _password_schema(*, password: str = "") -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_PASSWORD, default=password): selector.TextSelector(
                selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD),
            ),
        },
    )


def _manual_schema(
    *, host: str = "", port: int = DEFAULT_PORT, password: str = "", password_required: bool = True
) -> vol.Schema:
    schema = {
        vol.Required(CONF_HOST, default=host): selector.TextSelector(),
        vol.Required(CONF_PORT, default=port): vol.All(
            selector.NumberSelector(
                selector.NumberSelectorConfig(min=1, max=65535, mode=selector.NumberSelectorMode.BOX),
            ),
            vol.Coerce(int),
        ),
    }
    password_key = (
        vol.Required(CONF_PASSWORD, default=password)
        if password_required
        else vol.Optional(CONF_PASSWORD, default=password)
    )
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
        self.__discover_task: asyncio.Task[list[DiscoveredCamera]] | None = None
        self.__discover_error: str = ""
        # Shared across discover_password/manual/reconfigure, only one of which is ever in-flight per flow instance.
        self.__connect_task: asyncio.Task[CameraIdentity] | None = None
        self.__connect_identity: CameraIdentity | None = None
        self.__connect_error: _FlowError | None = None
        self.__connect_host: str | None = None
        self.__connect_port: int | None = None
        self.__connect_password: str = ""
        self.__connect_password_hash: str = ""

    async def __await_connect_task(self, step_id: str) -> config_entries.ConfigFlowResult:
        # Must hand off to a different step_id, or HA never tells the frontend to re-poll and the spinner stalls.
        assert self.__connect_task is not None  # noqa: S101
        if not self.__connect_task.done():
            return self.async_show_progress(
                step_id=step_id, progress_action="connecting", progress_task=self.__connect_task
            )
        task = self.__connect_task
        self.__connect_task = None
        try:
            self.__connect_identity = await task
        except _FlowError as exception:
            self.__connect_error = exception
        return self.async_show_progress_done(next_step_id=f"{step_id}_result")

    async def __step_connect(
        self, step_id: str, user_input: dict | None, spec: _ConnectStepSpec
    ) -> config_entries.ConfigFlowResult:
        """Shared error/progress/identity handling for discover_password/manual/reconfigure."""
        if self.__connect_task is not None:
            return await self.__await_connect_task(step_id=step_id)

        errors: dict[str, str] = {}

        if self.__connect_error is not None:
            exception = self.__connect_error
            self.__connect_error = None
            errors["base"] = exception.reason
            spec.placeholders["error"] = exception.message
        elif self.__connect_identity is not None:
            identity = self.__connect_identity
            self.__connect_identity = None
            return await spec.finish(identity)
        elif user_input is not None:
            host, port, password_hash = spec.start(user_input)
            self.__connect_password_hash = password_hash
            self.__connect_task = self.hass.async_create_task(self.__check_connection(host, port, password_hash))
            return self.async_show_progress(
                step_id=step_id, progress_action="connecting", progress_task=self.__connect_task
            )

        return self.async_show_form(
            step_id=step_id, data_schema=spec.schema, description_placeholders=spec.placeholders, errors=errors
        )

    async def async_step_user(self, user_input: dict | None = None) -> config_entries.ConfigFlowResult:  # noqa: ARG002
        """Let the user pick between auto-discovery and manual entry."""
        return self.async_show_menu(step_id="user", menu_options=["discover", "manual"])

    async def async_step_discover(self, user_input: dict | None = None) -> config_entries.ConfigFlowResult:
        """Broadcast a discovery request and let the user pick a camera."""
        # HA redelivers stale menu user_input on progress-done re-entry
        if user_input is not None and _CONF_DEVICE in user_input:
            self.__chosen = self.__discovered[user_input[_CONF_DEVICE]]
            return await self.async_step_discover_password()

        if self.__discovered:
            return self.__show_discover_form()

        if self.__discover_task is None:
            self.__discover_task = self.hass.async_create_task(
                GwellIPCamClient.async_discover(self.hass, timeout_s=DISCOVERY_TIMEOUT_S)
            )
        if not self.__discover_task.done():
            return self.async_show_progress(
                step_id="discover",
                progress_action="discovering",
                progress_task=self.__discover_task,
            )

        try:
            found = await self.__discover_task
        except APIError as exception:
            LOGGER.warning(exception)
            self.__discover_error = str(exception)
            return self.async_show_progress_done(next_step_id="discover_error")
        finally:
            self.__discover_task = None

        already_configured = {
            entry.data[CONF_CONTACT_ID] for entry in self._async_current_entries() if CONF_CONTACT_ID in entry.data
        }
        self.__discovered = {c.contact_id: c for c in found if c.contact_id not in already_configured}
        if not self.__discovered:
            return self.async_show_progress_done(next_step_id="discover_no_devices")
        return self.async_show_progress_done(next_step_id="discover_result")

    async def async_step_discover_result(self, user_input: dict | None = None) -> config_entries.ConfigFlowResult:
        """Redispatch into `discover`, now that `self.__discover_task` has finished (see `__await_connect_task`)."""
        return await self.async_step_discover(user_input)

    async def async_step_discover_no_devices(self, _user_input: dict | None = None) -> config_entries.ConfigFlowResult:
        """Dead-end step: discovery ran but found nothing new."""
        return self.async_abort(reason="no_devices_found")

    async def async_step_discover_error(self, _user_input: dict | None = None) -> config_entries.ConfigFlowResult:
        """Dead-end step: the discovery broadcast itself failed."""
        return self.async_abort(reason="discovery_failed", description_placeholders={"error": self.__discover_error})

    def __show_discover_form(self) -> config_entries.ConfigFlowResult:
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
        )

    async def async_step_integration_discovery(
        self, discovery_info: DiscoveryInfoType
    ) -> config_entries.ConfigFlowResult:
        """Handle a camera found by our own periodic background broadcast (see discovery.py)."""
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
        """Handle a camera found via its DHCP lease (manifest.json's OUI matcher)."""
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
        camera = self.__chosen

        def start(user_input: dict) -> tuple[str, int, str]:
            self.__connect_password = user_input[CONF_PASSWORD]
            return camera.host, camera.port, GwellIPCamClient.hash_password(self.__connect_password)

        async def finish(identity: CameraIdentity) -> config_entries.ConfigFlowResult:
            return await self.__finish(
                host=camera.host, port=camera.port, password_hash=self.__connect_password_hash, identity=identity
            )

        return await self.__step_connect(
            "discover_password",
            user_input,
            _ConnectStepSpec(
                schema=_password_schema(password=self.__connect_password),
                placeholders={"name": camera.name},
                start=start,
                finish=finish,
            ),
        )

    async def async_step_discover_password_result(
        self, user_input: dict | None = None
    ) -> config_entries.ConfigFlowResult:
        """Redispatch into `discover_password`, now that `self.__connect_task` has finished."""
        return await self.async_step_discover_password(user_input)

    async def async_step_manual(self, user_input: dict | None = None) -> config_entries.ConfigFlowResult:
        """Handle manual host/port/password entry."""

        def start(user_input: dict) -> tuple[str, int, str]:
            self.__connect_host = user_input[CONF_HOST]
            self.__connect_port = user_input[CONF_PORT]
            self.__connect_password = user_input[CONF_PASSWORD]
            return self.__connect_host, self.__connect_port, GwellIPCamClient.hash_password(self.__connect_password)

        async def finish(identity: CameraIdentity) -> config_entries.ConfigFlowResult:
            assert self.__connect_host is not None  # noqa: S101
            assert self.__connect_port is not None  # noqa: S101
            return await self.__finish(
                host=self.__connect_host,
                port=self.__connect_port,
                password_hash=self.__connect_password_hash,
                identity=identity,
            )

        return await self.__step_connect(
            "manual",
            user_input,
            _ConnectStepSpec(
                schema=_manual_schema(
                    host=self.__connect_host or "",
                    port=self.__connect_port or DEFAULT_PORT,
                    password=self.__connect_password,
                ),
                placeholders={},
                start=start,
                finish=finish,
            ),
        )

    async def async_step_manual_result(self, user_input: dict | None = None) -> config_entries.ConfigFlowResult:
        """Redispatch into `manual`, now that `self.__connect_task` has finished."""
        return await self.async_step_manual(user_input)

    async def async_step_reconfigure(self, user_input: dict | None = None) -> config_entries.ConfigFlowResult:
        """Let the user edit host/port/password for an existing camera."""
        entry = self._get_reconfigure_entry()

        def start(user_input: dict) -> tuple[str, int, str]:
            self.__connect_host = user_input[CONF_HOST]
            self.__connect_port = user_input[CONF_PORT]
            password = user_input.get(CONF_PASSWORD)
            password_hash = GwellIPCamClient.hash_password(password) if password else entry.data[CONF_PASSWORD_HASH]
            return self.__connect_host, self.__connect_port, password_hash

        async def finish(identity: CameraIdentity) -> config_entries.ConfigFlowResult:
            assert self.__connect_host is not None  # noqa: S101
            assert self.__connect_port is not None  # noqa: S101
            self._abort_if_unique_id_mismatch()
            # identity.name is a synthesized placeholder; keep the user's existing title
            return self.async_update_reload_and_abort(
                entry,
                title=entry.title,
                data_updates={
                    CONF_HOST: self.__connect_host,
                    CONF_PORT: self.__connect_port,
                    CONF_PASSWORD_HASH: self.__connect_password_hash,
                    CONF_CONTACT_ID: identity.contact_id,
                },
            )

        return await self.__step_connect(
            "reconfigure",
            user_input,
            _ConnectStepSpec(
                schema=_manual_schema(
                    host=self.__connect_host or entry.data[CONF_HOST],
                    port=self.__connect_port or entry.data[CONF_PORT],
                    password="",
                    password_required=False,
                ),
                placeholders={},
                start=start,
                finish=finish,
            ),
        )

    async def async_step_reconfigure_result(self, user_input: dict | None = None) -> config_entries.ConfigFlowResult:
        """Redispatch into `reconfigure`, now that `self.__connect_task` has finished."""
        return await self.async_step_reconfigure(user_input)

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> config_entries.ConfigFlowResult:  # noqa: ARG002
        """Handle a reauth triggered by ConfigEntryAuthFailed (e.g. the camera's password changed)."""
        self.context["title_placeholders"] = {"name": self._get_reauth_entry().title}
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict | None = None) -> config_entries.ConfigFlowResult:
        """Ask for the camera's new password; host/port are assumed unchanged (use reconfigure for those)."""
        entry = self._get_reauth_entry()

        def start(user_input: dict) -> tuple[str, int, str]:
            self.__connect_password = user_input[CONF_PASSWORD]
            password_hash = GwellIPCamClient.hash_password(self.__connect_password)
            return entry.data[CONF_HOST], entry.data[CONF_PORT], password_hash

        async def finish(identity: CameraIdentity) -> config_entries.ConfigFlowResult:
            self._abort_if_unique_id_mismatch()
            return self.async_update_reload_and_abort(
                entry,
                data_updates={
                    CONF_PASSWORD_HASH: self.__connect_password_hash,
                    CONF_CONTACT_ID: identity.contact_id,
                },
            )

        return await self.__step_connect(
            "reauth_confirm",
            user_input,
            _ConnectStepSpec(
                schema=_password_schema(password=self.__connect_password),
                placeholders={"name": entry.title},
                start=start,
                finish=finish,
            ),
        )

    async def async_step_reauth_confirm_result(self, user_input: dict | None = None) -> config_entries.ConfigFlowResult:
        """Redispatch into `reauth_confirm`, now that `self.__connect_task` has finished."""
        return await self.async_step_reauth_confirm(user_input)

    async def __check_connection(self, host: str, port: int, password_hash: str) -> CameraIdentity:
        try:
            return await GwellIPCamClient.async_check_connection(self.hass, host, port, password_hash)
        except APIAuthError as exception:
            LOGGER.warning(exception)
            reason = "auth"
            raise _FlowError(reason, str(exception)) from exception
        except APIConnectionError as exception:
            LOGGER.warning(exception)
            reason = "connection"
            raise _FlowError(reason, str(exception)) from exception
        except APIError as exception:
            LOGGER.exception("Unexpected error while checking camera connection")
            reason = "unknown"
            raise _FlowError(reason, str(exception)) from exception

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
    """Internal signal carrying the translation key and detail message of a failed step."""

    def __init__(self, reason: str, message: str) -> None:
        """Initialize with the translation key and human-readable detail."""
        super().__init__(reason)
        self.reason = reason
        self.message = message
