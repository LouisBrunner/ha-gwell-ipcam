"""Local RTSP relay fixing the camera's SETUP response (missing `/TCP`)."""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .const import LOGGER, WIRE_LOGGER
from .rtsp import AUDIO_CHANNELS, VIDEO_CHANNELS, cancel_and_wait

_CANCEL_TIMEOUT_S = 5.0

if TYPE_CHECKING:
    from .rtsp import RTSPSession

_SERVER_NAME = "gwell_ipcam-proxy"
_SESSION_ID = "gwell1"


@dataclass
class _Request:
    """A parsed incoming RTSP request from a downstream client (e.g. ffmpeg/go2rtc)."""

    method: str
    url: str
    cseq: int
    headers: dict[str, str]


async def _read_request(reader: asyncio.StreamReader, buf: bytearray) -> _Request | None:
    """Parse one request out of `buf` (refilled from `reader` as needed); leftover bytes stay in `buf`."""
    while True:
        idx = bytes(buf).find(b"\r\n\r\n")
        if idx == -1:
            chunk = await reader.read(4096)
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
        while len(buf) < header_end + content_length:
            chunk = await reader.read(4096)
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
    """Per-downstream-connection state threaded through request handling."""

    subscribed_channels: set[int]
    forward_task: asyncio.Task[None] | None = None


class RTSPProxyServer:
    """Serves the shared RTSPSession's video+audio to local RTSP/TCP clients."""

    def __init__(self, session: RTSPSession) -> None:
        """Initialize the proxy in front of an already-connected (or soon-to-be) RTSPSession."""
        self._session = session
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
            # close()/wait_closed() alone never touch already-accepted connections, only new ones.
            await self._server.abort_clients()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._server.wait_closed(), timeout=_CANCEL_TIMEOUT_S)
            self._server = None

    async def __handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await self.__serve(reader, writer)
        except (OSError, asyncio.IncompleteReadError, ValueError):
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
                request = await _read_request(reader, buf)
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
            if not self._session.online or not self._session.sdp:
                await _write_response(writer, request.cseq, status="454 Session Not Found")
            else:
                await _write_response(
                    writer,
                    request.cseq,
                    extra_headers={"Content-Type": "application/sdp", "Content-Base": request.url},
                    body=self._session.sdp,
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
                coro = self.__forward(writer, tuple(sorted(state.subscribed_channels)))
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

    async def __forward(self, writer: asyncio.StreamWriter, channels: tuple[int, ...]) -> None:
        async with self._session.subscribe(channels) as frames:
            async for channel, payload in frames:
                header = bytes([0x24, channel]) + len(payload).to_bytes(2, "big")
                writer.write(header + payload)
                await writer.drain()
