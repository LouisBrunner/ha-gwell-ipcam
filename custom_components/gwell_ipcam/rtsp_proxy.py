"""Local RTSP relay fixing the camera's SETUP response (missing `/TCP`); serves a fallback stream while offline."""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .const import DOMAIN, LOGGER, WIRE_LOGGER
from .fallback_stream import FALLBACK_FPS, FallbackEncoder, FrameCache
from .rtsp import AUDIO_CHANNELS, VIDEO_CHANNELS, cancel_and_wait

_CANCEL_TIMEOUT_S = 5.0
_REQUEST_READ_TIMEOUT_S = 8.0
_MAX_CONTENT_LENGTH = 262144
_ONLINE_POLL_TIMEOUT_S = 1.0

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .rtsp import RTSPSession

_SERVER_NAME = f"{DOMAIN}-proxy"
_SESSION_ID = "gwell1"

_FALLBACK_SDP = (
    "v=0\r\n"
    "o=- 0 0 IN IP4 127.0.0.1\r\n"
    f"s={DOMAIN} fallback\r\n"
    "t=0 0\r\n"
    "m=video 0 RTP/AVP 96\r\n"
    "a=control:track1\r\n"
    "a=rtpmap:96 H264/90000\r\n"
    "a=fmtp:96 packetization-mode=1\r\n"
)


@dataclass
class _Request:
    method: str
    url: str
    cseq: int
    headers: dict[str, str]


async def _read_request(reader: asyncio.StreamReader, buf: bytearray, timeout_s: float | None) -> _Request | None:
    """Parse one request out of `buf` (refilled from `reader` as needed); `timeout_s=None` once PLAY is active."""
    while True:
        idx = bytes(buf).find(b"\r\n\r\n")
        if idx == -1:
            chunk = await asyncio.wait_for(reader.read(4096), timeout=timeout_s)
            if not chunk:
                return None
            buf.extend(chunk)
            continue
        header_end = idx + 4
        text = bytes(buf[:idx]).decode(errors="replace")
        lines = text.split("\r\n")
        method, url, _version = lines[0].split(" ", 2)
        headers: dict[str, str] = {}
        for line in lines[1:]:
            key, sep, value = line.partition(":")
            if sep:
                headers[key.strip().lower()] = value.strip()
        LOGGER.debug("local RTSP proxy recv: %s %s %s", method, url, headers)
        WIRE_LOGGER.debug("local RTSP proxy recv: %s", text)
        content_length = int(headers.get("content-length", "0"))
        if not 0 <= content_length <= _MAX_CONTENT_LENGTH:
            msg = f"implausible Content-Length: {content_length}"
            raise ValueError(msg)
        while len(buf) < header_end + content_length:
            chunk = await asyncio.wait_for(reader.read(4096), timeout=timeout_s)
            if not chunk:
                return None
            buf.extend(chunk)
        del buf[: header_end + content_length]
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
    LOGGER.debug("local RTSP proxy send: %s %s", status, headers)
    WIRE_LOGGER.debug("local RTSP proxy send: %s", response_text)
    writer.write(response_text.encode())
    await writer.drain()


@dataclass
class _ConnectionState:
    subscribed_channels: set[int]
    forward_task: asyncio.Task[None] | None = None


class RTSPProxyServer:
    """Serves the shared RTSPSession's video+audio to local RTSP/TCP clients."""

    def __init__(self, session: RTSPSession, *, hass: HomeAssistant, entry_id: str) -> None:
        """Initialize the proxy in front of an already-connected (or soon-to-be) RTSPSession."""
        self._session = session
        self._server: asyncio.Server | None = None
        self.__hass = hass
        self.__frame_cache = FrameCache(hass, entry_id)
        self.__feeder_task: asyncio.Task[None] | None = None

    @property
    def port(self) -> int:
        """The local port this proxy is listening on."""
        assert self._server is not None  # noqa: S101
        return self._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]

    async def async_load_persisted_frame(self) -> None:
        """Load the last real frame saved to disk, if any; call before `start()` picks up its first client."""
        await self.__frame_cache.async_load_persisted()

    async def start(self) -> None:
        """Start listening on an OS-assigned local port."""
        self._server = await asyncio.start_server(self.__handle_client, host="127.0.0.1", port=0)
        self.__feeder_task = asyncio.get_running_loop().create_task(self.__feed_frame_cache())

    async def stop(self) -> None:
        """Stop listening and drop any in-flight downstream clients."""
        if self.__feeder_task is not None:
            await cancel_and_wait(self.__feeder_task)
            self.__feeder_task = None
        if self._server is not None:
            started = time.monotonic()
            self._server.close()
            # abort_clients() (sync, 3.13+) is needed too: close()/wait_closed() alone spare already-accepted clients.
            self._server.abort_clients()
            LOGGER.debug("Aborted local RTSP proxy clients in %.3fs", time.monotonic() - started)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._server.wait_closed(), timeout=_CANCEL_TIMEOUT_S)
            LOGGER.debug("Local RTSP proxy server closed in %.3fs total", time.monotonic() - started)
            self._server = None

    async def __handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await self.__serve(reader, writer)
        except OSError, asyncio.IncompleteReadError, ValueError, TimeoutError:
            LOGGER.debug("local RTSP proxy client disconnected", exc_info=True)
        finally:
            writer.close()
            with contextlib.suppress(OSError, TimeoutError):
                await asyncio.wait_for(writer.wait_closed(), timeout=_CANCEL_TIMEOUT_S)

    async def __serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        state = _ConnectionState(subscribed_channels=set())
        buf = bytearray()
        try:
            while True:
                timeout_s = None if state.forward_task is not None else _REQUEST_READ_TIMEOUT_S
                request = await _read_request(reader, buf, timeout_s)
                if request is None:
                    return
                if not await self.__handle_request(request, writer, state):
                    return
        finally:
            if state.forward_task is not None:
                await cancel_and_wait(state.forward_task)

    async def __handle_request(self, request: _Request, writer: asyncio.StreamWriter, state: _ConnectionState) -> bool:
        """Handle one request; returns False once TEARDOWN closes the connection."""
        if request.method == "OPTIONS":
            await _write_response(
                writer, request.cseq, extra_headers={"Public": "OPTIONS, DESCRIBE, SETUP, PLAY, TEARDOWN"}
            )
        elif request.method == "DESCRIBE":
            await _write_response(
                writer,
                request.cseq,
                extra_headers={"Content-Type": "application/sdp", "Content-Base": request.url},
                body=self._session.sdp if self._session.online and self._session.sdp else _FALLBACK_SDP,
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
                coro = self.__forward_loop(writer, tuple(sorted(state.subscribed_channels)))
                state.forward_task = asyncio.get_running_loop().create_task(coro)
        elif request.method == "TEARDOWN":
            await _write_response(writer, request.cseq, extra_headers={"Session": _SESSION_ID})
            return False
        else:
            await _write_response(writer, request.cseq, status="501 Not Implemented")
        return True

    @staticmethod
    def __setup_transport(url: str, state: _ConnectionState) -> str | None:
        # Always interleaved TCP on the upstream's fixed channel numbers, regardless of the client's request.
        if url.endswith("/track1"):
            state.subscribed_channels.update(VIDEO_CHANNELS)
            return f"RTP/AVP/TCP;unicast;interleaved={VIDEO_CHANNELS[0]}-{VIDEO_CHANNELS[1]}"
        if url.endswith("/track2"):
            state.subscribed_channels.update(AUDIO_CHANNELS)
            return f"RTP/AVP/TCP;unicast;interleaved={AUDIO_CHANNELS[0]}-{AUDIO_CHANNELS[1]}"
        return None

    async def __forward_loop(self, writer: asyncio.StreamWriter, channels: tuple[int, ...]) -> None:
        while True:
            if self._session.online:
                await self.__forward(writer, channels)
            else:
                await self.__forward_fallback(writer)

    async def __feed_frame_cache(self) -> None:
        async with self._session.subscribe(VIDEO_CHANNELS) as frames:
            async for channel, payload in frames:
                await self.__hass.async_add_executor_job(self.__frame_cache.feed, channel, payload)

    async def __forward(self, writer: asyncio.StreamWriter, channels: tuple[int, ...]) -> None:
        LOGGER.debug("local RTSP proxy: forward task started for channels=%s", channels)
        count = 0
        try:
            async with self._session.subscribe(channels) as frames:
                frame_iter = frames.__aiter__()
                while self._session.online:
                    try:
                        channel, payload = await asyncio.wait_for(
                            frame_iter.__anext__(), timeout=_ONLINE_POLL_TIMEOUT_S
                        )
                    except TimeoutError:
                        continue
                    count += 1
                    if count <= 3 or count % 50 == 0:  # noqa: PLR2004
                        LOGGER.debug(
                            "local RTSP proxy: forwarded frame #%d channel=%d len=%d", count, channel, len(payload)
                        )
                    header = bytes([0x24, channel]) + len(payload).to_bytes(2, "big")
                    writer.write(header + payload)
                    await writer.drain()
        finally:
            LOGGER.debug("local RTSP proxy: forward task ended after %d frames", count)

    async def __forward_fallback(self, writer: asyncio.StreamWriter) -> None:
        LOGGER.debug("local RTSP proxy: fallback forward task started")
        encoder = FallbackEncoder()
        try:
            count = 0
            while not self._session.online:
                message = str(self._session.last_error) if self._session.last_error else "camera offline"
                packets = await self.__hass.async_add_executor_job(self.__render_and_encode, encoder, message)
                for frame in packets:
                    writer.write(frame)
                await writer.drain()
                count = encoder.get_count()
                if count <= 3 or count % 50 == 0:  # noqa: PLR2004
                    LOGGER.debug("local RTSP proxy: sent fallback frame #%d", count)
                await asyncio.sleep(1 / FALLBACK_FPS)
        finally:
            LOGGER.info("local RTSP proxy: fallback forward task ended after %d frames", count)

    def __render_and_encode(self, encoder: FallbackEncoder, error: str) -> list[bytes]:
        image = self.__frame_cache.render(error=error)
        return list(encoder.encode(image))
