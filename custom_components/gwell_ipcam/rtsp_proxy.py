"""Local RTSP relay fixing the camera's SETUP response (missing `/TCP`); serves a fallback stream while offline."""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, TypedDict

import av
from homeassistant.helpers.storage import Store

from .const import DOMAIN, LOGGER, TALK_SAMPLE_RATE_HZ, WIRE_LOGGER
from .fallback_stream import FALLBACK_FPS, FallbackEncoder, FrameCache
from .rtsp import (
    _INTERLEAVE_HEADER_BYTES,
    _INTERLEAVE_MARKER,
    AUDIO_CHANNELS,
    AUDIO_TRACK,
    VIDEO_CHANNELS,
    VIDEO_TRACK,
    RTSPError,
    TalkSession,
    cancel_and_wait,
)

_BACKCHANNEL_CHANNELS = (4, 5)
_BACKCHANNEL_TRACK = "track3"
_BACKCHANNEL_SDP = f"m=audio 0 RTP/AVP 8\r\na=control:{_BACKCHANNEL_TRACK}\r\na=rtpmap:8 PCMA/8000\r\na=sendonly\r\n"
_RTP_FIXED_HEADER_BYTES = 12
_CANCEL_TIMEOUT_S = 5.0
_REQUEST_READ_TIMEOUT_S = 8.0
_MAX_CONTENT_LENGTH = 262144
_ONLINE_POLL_TIMEOUT_S = 1.0
_VIDEO_RTP_CLOCK_HZ = 90000  # RFC 6184
_PROXY_PORT_BASE = 39000

assert {*VIDEO_CHANNELS, *AUDIO_CHANNELS, *_BACKCHANNEL_CHANNELS} == set(range(6)), "channel numbers must be 0..5"  # noqa: S101

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .rtsp import RTSPSession


class _PortData(TypedDict):
    hwm: int
    ports: dict[str, int]


class _ProxyPortAllocator:
    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self.__store: Store[_PortData] = Store(hass, version=1, key=f"{DOMAIN}.proxy_ports")
        self.__entry_id = entry_id

    async def async_get_port(self) -> int:
        data = await self.__store.async_load() or _PortData(hwm=_PROXY_PORT_BASE, ports={})
        if self.__entry_id in data["ports"]:
            return data["ports"][self.__entry_id]
        port = data["hwm"]
        data["ports"][self.__entry_id] = port
        data["hwm"] = port + 1
        await self.__store.async_save(data)
        return port


_SERVER_NAME = f"{DOMAIN}-proxy"
_SESSION_ID = "gwell1"

_FALLBACK_SDP = (
    "v=0\r\n"
    "o=- 0 0 IN IP4 127.0.0.1\r\n"
    f"s={DOMAIN} fallback\r\n"
    "t=0 0\r\n"
    "m=video 0 RTP/AVP 96\r\n"
    f"a=control:{VIDEO_TRACK}\r\n"
    "a=rtpmap:96 H264/90000\r\n"
    "a=fmtp:96 packetization-mode=1\r\n"
)


@dataclass
class _Request:
    method: str
    url: str
    cseq: int
    headers: dict[str, str]


@dataclass
class _Interleaved:
    channel: int
    payload: bytes

    @staticmethod
    def build(buf: bytearray) -> _Interleaved | None:
        if len(buf) < _INTERLEAVE_HEADER_BYTES:
            return None
        total_len = _INTERLEAVE_HEADER_BYTES + int.from_bytes(buf[2:4], "big")
        if len(buf) < total_len:
            return None
        frame = _Interleaved(channel=buf[1], payload=bytes(buf[_INTERLEAVE_HEADER_BYTES:total_len]))
        del buf[:total_len]
        return frame


async def _read_request(  # noqa: C901
    reader: asyncio.StreamReader, buf: bytearray, timeout_s: float | None
) -> _Request | _Interleaved | None:
    """Parse one request or interleaved frame out of `buf`; `timeout_s=None` once PLAY is active."""
    while True:
        idx = bytes(buf).find(b"\r\n\r\n")
        if buf[:1] == bytes([_INTERLEAVE_MARKER]):
            if (frame := _Interleaved.build(buf)) is not None:
                return frame
            idx = -1
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


class _BackchannelSink:
    def __init__(self, host: str) -> None:
        self.__host = host
        self.__stack = contextlib.AsyncExitStack()
        self.__talk: TalkSession | None = None
        self.__decoder = av.CodecContext.create("pcm_alaw", "r")
        self.__decoder.sample_rate = TALK_SAMPLE_RATE_HZ
        self.__decoder.layout = "mono"

    async def feed(self, rtp_packet: bytes) -> None:
        payload = self.__rtp_payload(rtp_packet)
        if not payload:
            return
        try:
            if self.__talk is None:
                self.__talk = await self.__stack.enter_async_context(TalkSession(self.__host))
            for frame in self.__decoder.decode(av.Packet(payload)):
                await self.__talk.send_pcm16(frame.to_ndarray().tobytes(), paced=False)
        except RTSPError, OSError, TimeoutError:
            LOGGER.debug("local RTSP proxy: backchannel to %s failed", self.__host, exc_info=True)

    async def close(self) -> None:
        await self.__stack.aclose()

    @staticmethod
    def __rtp_payload(packet: bytes) -> bytes:
        if len(packet) <= _RTP_FIXED_HEADER_BYTES:
            return b""
        start = _RTP_FIXED_HEADER_BYTES + 4 * (packet[0] & 0x0F)
        if packet[0] & 0x10:
            start += 4 + 4 * int.from_bytes(packet[start + 2 : start + 4], "big")
        end = len(packet) - packet[-1] if packet[0] & 0x20 else len(packet)
        return packet[start:end]


@dataclass
class _ConnectionState:
    subscribed_channels: set[int]
    forward_task: asyncio.Task[None] | None = None
    backchannel: _BackchannelSink | None = None


class RTSPProxyServer:
    """Serves the shared RTSPSession's video+audio to local RTSP/TCP clients."""

    def __init__(self, session: RTSPSession, *, host: str, hass: HomeAssistant, entry_id: str) -> None:
        """Initialize the proxy in front of an already-connected (or soon-to-be) RTSPSession."""
        self._session = session
        self.__host = host
        self._server: asyncio.Server | None = None
        self.__hass = hass
        self.__frame_cache = FrameCache(hass, entry_id)
        self.__port_allocator = _ProxyPortAllocator(hass, entry_id)
        self.__feeder_task: asyncio.Task[None] | None = None

    @staticmethod
    def __current_video_rtp_timestamp() -> int:
        return int(time.monotonic() * _VIDEO_RTP_CLOCK_HZ) & 0xFFFFFFFF

    @staticmethod
    def __with_rtp_timestamp(rtp_packet: bytes, timestamp: int) -> bytes:
        return rtp_packet[:4] + timestamp.to_bytes(4, "big") + rtp_packet[8:]

    @property
    def port(self) -> int:
        """The local port this proxy is listening on."""
        assert self._server is not None  # noqa: S101
        return self._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]

    async def async_load_persisted_frame(self) -> None:
        """Load the last real frame saved to disk, if any; call before `start()` picks up its first client."""
        await self.__frame_cache.async_load_persisted()

    async def async_render_offline_snapshot(self, error: str) -> bytes:
        """Render a JPEG snapshot for the offline/fallback stream, using the last real frame if available."""
        return await self.__hass.async_add_executor_job(self.__frame_cache.render_jpeg, error)

    async def start(self) -> None:
        """Start listening on this entry's assigned local port (stable across reloads/restarts)."""
        port = await self.__port_allocator.async_get_port()
        self._server = await asyncio.start_server(self.__handle_client, host="127.0.0.1", port=port)
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
            if state.backchannel is not None:
                await state.backchannel.close()

    async def __handle_request(
        self, request: _Request | _Interleaved, writer: asyncio.StreamWriter, state: _ConnectionState
    ) -> bool:
        """Handle one request; returns False once TEARDOWN closes the connection."""
        if isinstance(request, _Interleaved):
            if state.backchannel is not None and request.channel == _BACKCHANNEL_CHANNELS[0]:
                await state.backchannel.feed(request.payload)
            return True
        if request.method == "OPTIONS":
            await _write_response(
                writer, request.cseq, extra_headers={"Public": "OPTIONS, DESCRIBE, SETUP, PLAY, TEARDOWN"}
            )
        elif request.method == "DESCRIBE":
            await _write_response(
                writer,
                request.cseq,
                extra_headers={"Content-Type": "application/sdp", "Content-Base": request.url},
                body=self.__describe_body(request),
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

    def __describe_body(self, request: _Request) -> str:
        if not (self._session.online and self._session.sdp):
            return _FALLBACK_SDP
        body = self._session.sdp
        if "backchannel" in request.headers.get("require", ""):
            body = body.rstrip("\r\n") + "\r\n" + _BACKCHANNEL_SDP
        return body

    def __setup_transport(self, url: str, state: _ConnectionState) -> str | None:
        # Always interleaved TCP on the upstream's fixed channel numbers, regardless of the client's request.
        if url.endswith(f"/{_BACKCHANNEL_TRACK}"):
            if state.backchannel is None:
                state.backchannel = _BackchannelSink(self.__host)
            return f"RTP/AVP/TCP;unicast;interleaved={_BACKCHANNEL_CHANNELS[0]}-{_BACKCHANNEL_CHANNELS[1]}"
        if url.endswith(f"/{VIDEO_TRACK}"):
            state.subscribed_channels.update(VIDEO_CHANNELS)
            return f"RTP/AVP/TCP;unicast;interleaved={VIDEO_CHANNELS[0]}-{VIDEO_CHANNELS[1]}"
        if url.endswith(f"/{AUDIO_TRACK}"):
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
        video_ts_offset: int | None = None
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
                    except StopAsyncIteration:
                        return
                    count += 1
                    if count <= 3 or count % 50 == 0:  # noqa: PLR2004
                        LOGGER.debug(
                            "local RTSP proxy: forwarded frame #%d channel=%d len=%d", count, channel, len(payload)
                        )
                    if channel == VIDEO_CHANNELS[0]:
                        upstream_ts = int.from_bytes(payload[4:8], "big")
                        if video_ts_offset is None:
                            video_ts_offset = self.__current_video_rtp_timestamp() - upstream_ts
                        payload = self.__with_rtp_timestamp(payload, (upstream_ts + video_ts_offset) & 0xFFFFFFFF)
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
                rtp_timestamp = self.__current_video_rtp_timestamp()
                packets = await self.__hass.async_add_executor_job(
                    self.__render_and_encode, encoder, message, rtp_timestamp
                )
                for frame in packets:
                    writer.write(frame)
                await writer.drain()
                count = encoder.get_count()
                if count <= 3 or count % 50 == 0:  # noqa: PLR2004
                    LOGGER.debug("local RTSP proxy: sent fallback frame #%d", count)
                await asyncio.sleep(1 / FALLBACK_FPS)
        finally:
            LOGGER.info("local RTSP proxy: fallback forward task ended after %d frames", count)

    def __render_and_encode(self, encoder: FallbackEncoder, error: str, rtp_timestamp: int) -> list[bytes]:
        image = self.__frame_cache.render(error=error)
        return list(encoder.encode(image, rtp_timestamp=rtp_timestamp))
