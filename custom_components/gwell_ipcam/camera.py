"""Camera platform for the Gwell IP Camera integration."""

from __future__ import annotations

from typing import TYPE_CHECKING

import av
import voluptuous as vol
from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_platform

from .api import PTZ_DIRECTIONS, SETTING_MOTION_DETECT, map_ptz_direction
from .const import LOGGER
from .coordinator import GwellIPCamCoordinator
from .entity import GwellIPCamEntity

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .api import CameraIdentity
    from .data import GwellIPCamConfigEntry

SERVICE_PTZ = "ptz"
_PTZ_MOVE_SCHEMA = vol.Schema(
    {
        vol.Required("direction"): vol.In(PTZ_DIRECTIONS),
        vol.Optional("steps", default=1): vol.All(vol.Coerce(int), vol.Range(min=1, max=50)),
        vol.Optional("step_delay_ms", default=200): vol.All(vol.Coerce(int), vol.Range(min=0, max=2000)),
    }
)
_SERVICE_PTZ_SCHEMA = {vol.Required("moves"): vol.All(cv.ensure_list, [_PTZ_MOVE_SCHEMA])}

_turbojpeg: object | None = None


async def async_setup_entry(
    hass: HomeAssistant,  # noqa: ARG001
    entry: GwellIPCamConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the camera platform."""
    async_add_entities(
        [
            GwellIPCamCamera(
                coordinator=entry.runtime_data.coordinator,
                identity=entry.runtime_data.identity,
            )
        ]
    )
    platform = entity_platform.async_get_current_platform()
    platform.async_register_entity_service(SERVICE_PTZ, _SERVICE_PTZ_SCHEMA, "async_ptz")


class GwellIPCamCamera(GwellIPCamEntity[GwellIPCamCoordinator], Camera):
    """Live feed for a Gwell IP camera."""

    _attr_translation_key = "live_stream"
    _attr_supported_features = CameraEntityFeature.STREAM

    def __init__(self, coordinator: GwellIPCamCoordinator, identity: CameraIdentity) -> None:
        """Initialize the camera."""
        super().__init__(coordinator, identity)
        Camera.__init__(self)
        self._identity = identity
        self._attr_unique_id = f"{coordinator.config_entry.unique_id}_live"

    async def stream_source(self) -> str | None:
        """Return the live stream URL, or None if unavailable (see api.py)."""
        return await self.coordinator.config_entry.runtime_data.client.async_get_live_stream_url()

    @property
    def brand(self) -> str | None:
        """Return the camera manufacturer."""
        return "Gwell"

    @property
    def model(self) -> str | None:
        """Return the camera model."""
        return self._identity.model

    @property
    def is_recording(self) -> bool:
        """Mirror the `record` switch."""
        return self.coordinator.data.recording

    @property
    def is_streaming(self) -> bool:
        """Whether the upstream RTSP connection is currently established."""
        return self.coordinator.config_entry.runtime_data.client.rtsp_session.online

    @property
    def motion_detection_enabled(self) -> bool:
        """Mirror the `motion_detect` switch, for `camera.enable_motion_detection`/`disable_motion_detection`."""
        return bool(self.coordinator.data.settings.get(SETTING_MOTION_DETECT, 0))

    async def async_enable_motion_detection(self) -> None:
        """Handle the `camera.enable_motion_detection` action."""
        client = self.coordinator.config_entry.runtime_data.client
        await client.async_set_setting(SETTING_MOTION_DETECT, 1)
        await self.coordinator.async_request_refresh()

    async def async_disable_motion_detection(self) -> None:
        """Handle the `camera.disable_motion_detection` action."""
        client = self.coordinator.config_entry.runtime_data.client
        await client.async_set_setting(SETTING_MOTION_DETECT, 0)
        await self.coordinator.async_request_refresh()

    async def async_camera_image(
        self,
        width: int | None = None,  # noqa: ARG002
        height: int | None = None,  # noqa: ARG002
    ) -> bytes | None:
        """Grab one live frame, or None if the camera is offline (HA shows its own unavailable placeholder)."""
        client = self.coordinator.config_entry.runtime_data.client
        session = client.rtsp_session
        if not session.online:
            return None
        url = await self.stream_source()
        if url is None:
            return None
        LOGGER.debug("Grabbing a snapshot for %s from %s", self.entity_id, url)
        jpeg = await self.hass.async_add_executor_job(lambda: _grab_snapshot_jpeg(url))
        if jpeg is not None:
            LOGGER.debug("Snapshot for %s succeeded (%d bytes)", self.entity_id, len(jpeg))
        return jpeg

    async def async_ptz(self, moves: list[dict]) -> None:
        """Run a sequence of PTZ moves, mapping each direction for the camera's image-flip setting."""
        LOGGER.debug("User called ptz service on %s with %s", self.entity_id, moves)
        client = self.coordinator.config_entry.runtime_data.client
        settings = self.coordinator.data.settings
        for move in moves:
            direction = map_ptz_direction(move["direction"], settings)
            await client.async_ptz(direction, steps=move["steps"], step_delay_ms=move["step_delay_ms"])


def _grab_snapshot_jpeg(url: str) -> bytes | None:
    global _turbojpeg  # noqa: PLW0603
    from turbojpeg import TurboJPEG  # noqa: PLC0415

    if _turbojpeg is None:
        _turbojpeg = TurboJPEG()

    try:
        with av.open(url, options={"rtsp_transport": "tcp"}, timeout=(5.0, 5.0)) as container:
            for frame in container.decode(video=0):
                bgr = frame.to_ndarray(format="bgr24")
                return bytes(_turbojpeg.encode(bgr))
    except av.error.FFmpegError:
        LOGGER.warning("failed to grab a snapshot frame from %s", url, exc_info=True)
    return None
