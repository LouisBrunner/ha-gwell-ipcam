"""Expose camera recordings through Home Assistant's media library."""

from __future__ import annotations

from typing import TYPE_CHECKING

from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.components.media_player import MediaClass, MediaType
from homeassistant.components.media_source import (
    BrowseMediaSource,
    MediaSource,
    MediaSourceItem,
    PlayMedia,
    Unresolvable,
)

from .const import DOMAIN

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .api import Recording
    from .data import GwellIPCamConfigEntry

STREAM_URL_FORMAT = f"/api/{DOMAIN}/stream/{{entry_id}}/{{recording_id}}"


def _directory_node(
    identifier: str | None, title: str, children: list[BrowseMediaSource] | None = None
) -> BrowseMediaSource:
    return BrowseMediaSource(
        domain=DOMAIN,
        identifier=identifier,
        media_class=MediaClass.DIRECTORY,
        media_content_type=MediaType.VIDEO,
        title=title,
        can_play=False,
        can_expand=True,
        children=children,
    )


def _recording_node(entry_id: str, recording: Recording) -> BrowseMediaSource:
    return BrowseMediaSource(
        domain=DOMAIN,
        identifier=f"{entry_id}/{recording.recording_id}",
        media_class=MediaClass.VIDEO,
        media_content_type=MediaType.VIDEO,
        title=recording.started_at.isoformat(),
        can_play=True,
        can_expand=False,
    )


def media_source_identifier(entry: GwellIPCamConfigEntry, recording_id: str) -> str:
    """Build the media-source content ID for a given camera's recording."""
    return f"media-source://{DOMAIN}/{entry.entry_id}/{recording_id}"


async def async_get_media_source(hass: HomeAssistant) -> MediaSource:
    """Register the recording-stream HTTP view and return the media source."""
    hass.http.register_view(RecordingStreamView())
    return GwellIPCamMediaSource(hass)


class GwellIPCamMediaSource(MediaSource):
    """Browse and resolve recordings stored on Gwell IP cameras."""

    name = "Gwell IP Camera"

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize, stashing `hass` since the `MediaSource` base class doesn't keep a reference to it."""
        super().__init__(DOMAIN)
        self.hass = hass

    async def async_resolve_media(self, item: MediaSourceItem) -> PlayMedia:
        """Resolve a recording identifier to a playable stream URL."""
        parts = item.identifier.split("/", 1)
        if len(parts) != 2:  # noqa: PLR2004
            msg = f"Malformed media identifier: {item.identifier!r}"
            raise Unresolvable(msg)
        entry_id, recording_id = parts
        return PlayMedia(
            url=STREAM_URL_FORMAT.format(entry_id=entry_id, recording_id=recording_id),
            mime_type="video/mp4",
        )

    async def async_browse_media(self, item: MediaSourceItem) -> BrowseMediaSource:
        """Browse cameras, then their recordings."""
        if not item.identifier:
            cameras = [
                _directory_node(entry.entry_id, entry.runtime_data.identity.name) for entry in self._config_entries()
            ]
            return _directory_node(None, "Gwell IP Cameras", cameras)

        entry_id = item.identifier.split("/", 1)[0]
        entry = self._config_entry(entry_id)
        recordings = entry.runtime_data.recordings_coordinator.data or []
        children = [_recording_node(entry_id, recording) for recording in recordings]
        return _directory_node(entry_id, entry.runtime_data.identity.name, children)

    def _config_entries(self) -> list[GwellIPCamConfigEntry]:
        return self.hass.config_entries.async_loaded_entries(DOMAIN)  # type: ignore[return-value]

    def _config_entry(self, entry_id: str) -> GwellIPCamConfigEntry:
        entry = self.hass.config_entries.async_get_entry(entry_id)
        if entry is None:
            msg = f"Unknown config entry: {entry_id}"
            raise ValueError(msg)
        return entry  # type: ignore[return-value]


class RecordingStreamView(HomeAssistantView):
    """Proxy a recording's bytes from the camera to the browser."""

    url = STREAM_URL_FORMAT
    name = f"api:{DOMAIN}:stream"
    requires_auth = True

    async def get(self, request: web.Request, entry_id: str, recording_id: str) -> web.StreamResponse:
        """Return 404 if `entry_id` is unknown, else stream the response body chunk by chunk."""
        hass: HomeAssistant = request.app["hass"]
        entry = hass.config_entries.async_get_entry(entry_id)
        if entry is None:
            return web.Response(status=404)

        response = web.StreamResponse(headers={"Content-Type": "video/mp4"})
        await response.prepare(request)
        async for chunk in entry.runtime_data.client.async_stream_recording(recording_id):
            await response.write(chunk)
        await response.write_eof()
        return response
