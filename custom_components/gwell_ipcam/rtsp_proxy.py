"""
Local RTSP relay that fixes the camera's broken SETUP Transport header for ffmpeg/go2rtc.

The camera's real SETUP response omits `/TCP` from the Transport header even though
interleaved TCP is the only transport that actually works -- ffmpeg's RTSP demuxer refuses
to play the stream without it (this is exactly what the rtsp-fixer addon patched; see its
`FixForceTCPInTransport` option). We front the shared RTSPSession with a small
standards-compliant RTSP/TCP server so downstream consumers never see the broken header at
all, and multiple downstream viewers all share the one upstream connection for free.

The camera is only powered on when actively protecting the house, so the upstream session is
offline most of the time. Rather than ever failing DESCRIBE/SETUP/PLAY, this proxy falls back
to a single-track MJPEG "stream" of the last-known frame with the current error overlaid
(mirrors rtsp-fixer's own thumbnail-stream fallback) -- dashboards and go2rtc always get a
valid, playable session instead of a broken tile.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from typing import TYPE_CHECKING

from . import rtp_jpeg
from .const import LOGGER
from .fallback import JPEG_QUALITY
from .rtsp import AUDIO_CHANNELS, VIDEO_CHANNELS

if TYPE_CHECKING:
    from .fallback import FrameCache
    from .rtsp import RTSPSession

_SERVER_NAME = "gwell_ipcam-proxy"
_SESSION_ID = "gwell1"
_FALLBACK_SDP = (
    "v=0\r\n"
    "o=- 0 0 IN IP4 127.0.0.1\r\n"
    "s=gwell_ipcam fallback\r\n"
    "t=0 0\r\n"
    "m=video 0 RTP/AVP 26\r\n"
    "a=control:track1\r\n"
)
_FALLBACK_FRAME_INTERVAL_S = 1.0


@dataclass
class _Request:
    """A parsed incoming RTSP request from a downstream client (e.g. ffmpeg/go2rtc)."""

    method: str
    url: str
    cseq: int
    headers: dict[str, str]


async def _read_request(reader: asyncio.StreamReader) -> _Request | None:
    buf = bytearray()
    while True:
        chunk = await reader.read(4096)
        if not chunk:
            return None
        buf.extend(chunk)
        idx = bytes(buf).find(b"\r\n\r\n")
        if idx == -1:
            continue
        text = bytes(buf[:idx]).decode(errors="replace")
        LOGGER.debug("local RTSP proxy recv: %s", text)
        lines = text.split("\r\n")
        method, url, _version = lines[0].split(" ", 2)
        headers: dict[str, str] = {}
        for line in lines[1:]:
            key, sep, value = line.partition(":")
            if sep:
                headers[key.strip().lower()] = value.strip()
        return _Request(method=method, url=url, cseq=int(headers.get("cseq", "0")), headers=headers)


async def _write_response(
    writer: asyncio.StreamWriter,
    cseq: int,
    *,
    status: str = "200 OK",
    extra_headers: dict[str, str] | None = None,
    body: str = "",
) -> None:
    headers = {"CSeq": str(cseq), "Server": _SERVER_NAME}
    if body:
        headers["Content-Length"] = str(len(body.encode()))
    headers.update(extra_headers or {})
    header_text = "\r\n".join(f"{key}: {value}" for key, value in headers.items())
    response_text = f"RTSP/1.0 {status}\r\n{header_text}\r\n\r\n{body}"
    LOGGER.debug("local RTSP proxy send: %s", response_text)
    writer.write(response_text.encode())
    await writer.drain()


@dataclass
class _ConnectionState:
    """Per-downstream-connection state threaded through request handling."""

    subscribed_channels: set[int]
    is_fallback: bool = False
    forward_task: asyncio.Task[None] | None = None


class RTSPProxyServer:
    """Serves the shared RTSPSession's video+audio (or an offline fallback) to local RTSP/TCP clients."""

    def __init__(self, session: RTSPSession, frame_cache: FrameCache) -> None:
        """Initialize the proxy in front of an already-connected (or soon-to-be) RTSPSession."""
        self._session = session
        self._frame_cache = frame_cache
        self._server: asyncio.Server | None = None

    @property
    def port(self) -> int:
        """The ephemeral local port this proxy is listening on."""
        assert self._server is not None  # noqa: S101
        return self._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]

    async def start(self) -> None:
        """Start listening on an OS-assigned local port."""
        self._server = await asyncio.start_server(self.__handle_client, host="127.0.0.1", port=0)

    async def stop(self) -> None:
        """Stop listening and drop any in-flight downstream clients."""
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def __handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await self.__serve(reader, writer)
        except (OSError, asyncio.IncompleteReadError, ValueError):
            LOGGER.debug("local RTSP proxy client disconnected", exc_info=True)
        finally:
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()

    async def __serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        state = _ConnectionState(subscribed_channels=set())
        try:
            while True:
                request = await _read_request(reader)
                if request is None:
                    return
                if request.method == "TEARDOWN":
                    await _write_response(writer, request.cseq, extra_headers={"Session": _SESSION_ID})
                    return
                await self.__handle_request(request, writer, state)
        finally:
            if state.forward_task is not None:
                state.forward_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await state.forward_task

    async def __handle_request(self, request: _Request, writer: asyncio.StreamWriter, state: _ConnectionState) -> None:
        if request.method == "OPTIONS":
            await _write_response(
                writer, request.cseq, extra_headers={"Public": "OPTIONS, DESCRIBE, SETUP, PLAY, TEARDOWN"}
            )
        elif request.method == "DESCRIBE":
            state.is_fallback = not self._session.online
            body = _FALLBACK_SDP if state.is_fallback else (self._session.sdp or "")
            await _write_response(
                writer,
                request.cseq,
                extra_headers={"Content-Type": "application/sdp", "Content-Base": request.url},
                body=body,
            )
        elif request.method == "SETUP":
            transport = self.__setup_transport(request.url, state)
            if transport is None:
                await _write_response(writer, request.cseq, status="454 Session Not Found")
            else:
                await _write_response(
                    writer, request.cseq, extra_headers={"Transport": transport, "Session": _SESSION_ID}
                )
        elif request.method == "PLAY":
            await _write_response(writer, request.cseq, extra_headers={"Session": _SESSION_ID})
            if state.forward_task is None:
                coro = (
                    self.__forward_fallback(writer)
                    if state.is_fallback
                    else self.__forward(writer, tuple(sorted(state.subscribed_channels)))
                )
                state.forward_task = asyncio.get_running_loop().create_task(coro)
        else:
            await _write_response(writer, request.cseq, status="501 Not Implemented")

    @staticmethod
    def __setup_transport(url: str, state: _ConnectionState) -> str | None:
        # We always speak pure interleaved TCP with fixed channel numbers matching the real
        # upstream session, regardless of what the client's Transport header asked for --
        # that lets us tee raw upstream frames straight through with no per-connection remap.
        if url.endswith("/track1"):
            state.subscribed_channels.update(VIDEO_CHANNELS)
            return f"RTP/AVP/TCP;unicast;interleaved={VIDEO_CHANNELS[0]}-{VIDEO_CHANNELS[1]}"
        if not state.is_fallback and url.endswith("/track2"):
            state.subscribed_channels.update(AUDIO_CHANNELS)
            return f"RTP/AVP/TCP;unicast;interleaved={AUDIO_CHANNELS[0]}-{AUDIO_CHANNELS[1]}"
        return None

    async def __forward(self, writer: asyncio.StreamWriter, channels: tuple[int, ...]) -> None:
        async with self._session.subscribe(channels) as frames:
            async for channel, payload in frames:
                header = bytes([0x24, channel]) + len(payload).to_bytes(2, "big")
                writer.write(header + payload)
                await writer.drain()

    async def __forward_fallback(self, writer: asyncio.StreamWriter) -> None:
        """
        Stream the last-known frame (error overlaid) as RTP/JPEG at ~1fps until back online.

        Closes the connection once the real session reconnects, so the downstream client
        (ffmpeg/go2rtc) naturally re-DESCRIBEs and picks up the real H.264 track.
        """
        sequence = 0
        frame_count = 0
        video_channel = VIDEO_CHANNELS[0]
        while not self._session.online:
            message = str(self._session.last_error) if self._session.last_error else "camera offline"
            jpeg, width, height = await asyncio.get_running_loop().run_in_executor(
                None, self._frame_cache.render_error, message
            )
            params = rtp_jpeg.FrameParams(
                width=width,
                height=height,
                quality=JPEG_QUALITY,
                sequence_start=sequence,
                timestamp=frame_count * rtp_jpeg.RTP_CLOCK_HZ,
            )
            packets = rtp_jpeg.build_packets(jpeg, params)
            sequence += len(packets)
            frame_count += 1
            for packet in packets:
                header = bytes([0x24, video_channel]) + len(packet).to_bytes(2, "big")
                writer.write(header + packet)
            await writer.drain()
            await asyncio.sleep(_FALLBACK_FRAME_INTERVAL_S)
