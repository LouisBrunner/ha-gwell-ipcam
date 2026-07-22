"""Shared RTSP session for a Gwell IP camera, with vendor extensions for PTZ/talk; see docs/PROTOCOL.md."""

from __future__ import annotations

import asyncio
import contextlib
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .const import LOGGER, RTSP_PATH, RTSP_PORT, TALK_SAMPLE_RATE_HZ, WIRE_LOGGER

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from typing import Self

_USER_AGENT = "gwell_ipcam/0.1"
_INTERLEAVE_MARKER = 0x24
_INTERLEAVE_HEADER_BYTES = 4
_REQUEST_TIMEOUT_S = 8.0
_RECONNECT_INTERVAL_S = 15.0
_CANCEL_TIMEOUT_S = 5.0


async def cancel_and_wait(task: asyncio.Task) -> None:
    """Cancel `task` and wait for it, bounded so a stuck task can never hang shutdown indefinitely."""
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError, TimeoutError):
        await asyncio.wait_for(task, timeout=_CANCEL_TIMEOUT_S)

VIDEO_CHANNELS = (0, 1)
AUDIO_CHANNELS = (2, 3)

# "DWON" is the vendor's own typo, baked into the camera's compiled command table.
_PTZ_WIRE = {"up": "UP", "down": "DWON", "left": "LEFT", "right": "RIGHT"}

_TALK_CHANNEL = 0x02
_TALK_GAP_BYTES = 12
_TALK_CHUNK_BYTES = 320  # 160 samples @ 8kHz mono PCM16 == 20ms


class RTSPError(Exception):
    """Raised when the camera's RTSP control connection misbehaves."""


@dataclass
class _Response:
    status_line: str
    headers: dict[str, str]
    body: str
    cseq: int

    @classmethod
    def parse(cls, header_text: str, body: str) -> _Response:
        """Parse from the raw header block (no trailing blank line) and the already-extracted body."""
        lines = header_text.split("\r\n")
        headers: dict[str, str] = {}
        for line in lines[1:]:
            key, sep, value = line.partition(":")
            if sep:
                headers[key.strip().lower()] = value.strip()
        return cls(status_line=lines[0], headers=headers, body=body, cseq=int(headers.get("cseq", "0")))

    @property
    def ok(self) -> bool:
        return " 200 " in f" {self.status_line} "

    @property
    def session_id(self) -> str | None:
        """The RTSP Session header value, stripped of its `;timeout=...` suffix."""
        raw = self.headers.get("session")
        return raw.split(";")[0] if raw else None


def _parse_content_length(header_text: str) -> int:
    for line in header_text.split("\r\n")[1:]:
        key, sep, value = line.partition(":")
        if sep and key.strip().lower() == "content-length":
            with contextlib.suppress(ValueError):
                return int(value.strip())
    return 0


async def _simple_request(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    cseq: int,
    request_line: str,
    extra_headers: dict[str, str] | None = None,
) -> _Response:
    """Send one request and read its response. Only valid before any interleaved media is flowing."""
    headers = {"CSeq": str(cseq), "User-Agent": _USER_AGENT, **(extra_headers or {})}
    header_text = "\r\n".join(f"{key}: {value}" for key, value in headers.items())
    request_text = f"{request_line}\r\n{header_text}\r\n\r\n"
    LOGGER.debug("RTSP send: %s %s", request_line, headers)
    WIRE_LOGGER.debug("RTSP send: %s", request_text)
    writer.write(request_text.encode())
    await writer.drain()

    method = request_line.split(" ", 1)[0]
    buf = bytearray()
    while True:
        chunk = await asyncio.wait_for(reader.read(4096), timeout=_REQUEST_TIMEOUT_S)
        if not chunk:
            msg = f"connection closed while waiting for {method} response"
            raise RTSPError(msg)
        buf.extend(chunk)
        header_end = bytes(buf).find(b"\r\n\r\n")
        if header_end == -1:
            continue
        header_text_in = bytes(buf[:header_end]).decode(errors="replace")
        content_length = _parse_content_length(header_text_in)
        total_len = header_end + 4 + content_length
        if len(buf) < total_len:
            continue
        body = bytes(buf[header_end + 4 : total_len]).decode(errors="replace")
        response = _Response.parse(header_text_in, body)
        LOGGER.debug("RTSP recv: %s %s", response.status_line, response.headers)
        WIRE_LOGGER.debug("RTSP recv: %s", bytes(buf[:total_len]).decode(errors="replace"))
        if not response.ok:
            msg = f"{method} failed: {response.status_line}"
            raise RTSPError(msg)
        return response


class RTSPSession:
    """One long-lived, auto-reconnecting RTSP connection; PTZ shares it, push-to-talk uses a separate TalkSession."""

    def __init__(self, host: str) -> None:
        """Initialize with the camera's LAN host/IP. Call start() before use."""
        self._host = host
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._supervisor_task: asyncio.Task[None] | None = None
        self._cseq = 0
        self._sdp: str | None = None
        self._pending: dict[int, asyncio.Future[_Response]] = {}
        self._subscribers: dict[int, list[asyncio.Queue[tuple[int, bytes]]]] = {}
        self._online = False
        self._last_error: Exception | None = None

    @property
    def sdp(self) -> str | None:
        """The SDP returned by the camera's DESCRIBE, once connected."""
        return self._sdp

    @property
    def online(self) -> bool:
        """Whether the upstream connection is currently established."""
        return self._online

    @property
    def last_error(self) -> Exception | None:
        """The most recent connection error, or None while online."""
        return self._last_error

    async def start(self) -> None:
        """Start the supervising connect/reconnect loop. Does not block for the first connect."""
        self._supervisor_task = asyncio.get_running_loop().create_task(self.__supervise())

    async def stop(self) -> None:
        """Stop reconnecting and tear down the upstream connection."""
        if self._supervisor_task is not None:
            await cancel_and_wait(self._supervisor_task)
            self._supervisor_task = None
        await self.__disconnect()

    async def __supervise(self) -> None:
        while True:
            try:
                await self.__connect_once()
            except (OSError, RTSPError, TimeoutError) as exception:
                self.__mark_offline(exception)
                await asyncio.sleep(_RECONNECT_INTERVAL_S)
                continue
            self.__mark_online()
            assert self._reader_task is not None  # noqa: S101
            try:
                await self._reader_task
            except OSError as exception:
                self.__mark_offline(exception)
            else:
                self.__mark_offline(RTSPError("connection dropped"))
            await self.__disconnect()
            await asyncio.sleep(_RECONNECT_INTERVAL_S)

    def __mark_online(self) -> None:
        was_offline = not self._online
        self._online = True
        self._last_error = None
        if was_offline:
            LOGGER.info("RTSP connection to %s is back online", self._host)

    def __mark_offline(self, exception: Exception) -> None:
        # Warning only on the online->offline edge; the camera is usually off, so every retry would spam the log.
        was_online = self._online
        self._online = False
        self._last_error = exception
        if was_online:
            LOGGER.warning("RTSP connection to %s went offline: %s", self._host, exception)
        else:
            LOGGER.debug("RTSP connection to %s still offline: %s", self._host, exception)

    async def __connect_once(self) -> None:
        self._reader, self._writer = await asyncio.open_connection(self._host, RTSP_PORT)
        url = f"rtsp://{self._host}{RTSP_PATH}"
        await self.__simple(f"OPTIONS {url} RTSP/1.0")
        desc = await self.__simple(f"DESCRIBE {url} RTSP/1.0", {"Accept": "application/sdp"})
        self._sdp = desc.body
        session1 = await self.__simple(
            f"SETUP {url}/track1 RTSP/1.0", {"Transport": "RTP/AVP/TCP;unicast;interleaved=0-1"}
        )
        session_id = session1.session_id or ""
        await self.__simple(
            f"SETUP {url}/track2 RTSP/1.0",
            {"Transport": "RTP/AVP/TCP;unicast;interleaved=2-3", "Session": session_id},
        )
        await self.__simple(f"PLAY {url} RTSP/1.0", {"Session": session_id, "Range": "npt=0.000-"})
        self._reader_task = asyncio.get_running_loop().create_task(self.__read_loop())

    async def __disconnect(self) -> None:
        if self._reader_task is not None:
            await cancel_and_wait(self._reader_task)
            self._reader_task = None
        if self._writer is not None:
            self._writer.close()
            with contextlib.suppress(OSError, TimeoutError):
                await asyncio.wait_for(self._writer.wait_closed(), timeout=_CANCEL_TIMEOUT_S)
        self._reader = None
        self._writer = None
        self._sdp = None
        for future in self._pending.values():
            if not future.done():
                future.cancel()
        self._pending.clear()

    @asynccontextmanager
    async def subscribe(self, channels: tuple[int, ...]) -> AsyncIterator[AsyncIterator[tuple[int, bytes]]]:
        """Yield an async iterator of (channel, payload) interleaved frames for the given channels."""
        queue: asyncio.Queue[tuple[int, bytes]] = asyncio.Queue(maxsize=256)
        for channel in channels:
            self._subscribers.setdefault(channel, []).append(queue)
        try:

            async def _iter() -> AsyncIterator[tuple[int, bytes]]:
                while True:
                    yield await queue.get()

            yield _iter()
        finally:
            for channel in channels:
                self._subscribers.get(channel, []).remove(queue)

    async def ptz(self, direction: str, *, steps: int = 1, step_delay_ms: int = 200) -> None:
        """Send `steps` ptzCmd nudges; the wire protocol has no distance/speed concept, only a fixed step."""
        url = f"rtsp://{self._host}{RTSP_PATH}"
        for i in range(steps):
            await self.__request(
                f"SET_PARAMETER {url} RTSP/1.0",
                {"Content-length": "strlen(Content-type)", "Content-type": f"ptzCmd:{_PTZ_WIRE[direction]}"},
            )
            if i < steps - 1:
                await asyncio.sleep(step_delay_ms / 1000)

    def __next_cseq(self) -> int:
        self._cseq += 1
        return self._cseq

    async def __simple(self, request_line: str, extra_headers: dict[str, str] | None = None) -> _Response:
        if self._reader is None or self._writer is None:
            msg = "RTSP session is not connected"
            raise RTSPError(msg)
        return await _simple_request(self._reader, self._writer, self.__next_cseq(), request_line, extra_headers)

    async def __request(self, request_line: str, extra_headers: dict[str, str] | None = None) -> _Response:
        """Send a request while the interleaved read loop is active, resolved via its CSeq."""
        if self._writer is None:
            msg = "RTSP session is not connected"
            raise RTSPError(msg)
        cseq = self.__next_cseq()
        headers = {"CSeq": str(cseq), "User-Agent": _USER_AGENT, **(extra_headers or {})}
        header_text = "\r\n".join(f"{key}: {value}" for key, value in headers.items())
        future: asyncio.Future[_Response] = asyncio.get_running_loop().create_future()
        self._pending[cseq] = future
        request_text = f"{request_line}\r\n{header_text}\r\n\r\n"
        LOGGER.debug("RTSP send: %s %s", request_line, headers)
        WIRE_LOGGER.debug("RTSP send: %s", request_text)
        self._writer.write(request_text.encode())
        await self._writer.drain()
        try:
            response = await asyncio.wait_for(future, timeout=_REQUEST_TIMEOUT_S)
        except TimeoutError as exception:
            self._pending.pop(cseq, None)
            method = request_line.split(" ", 1)[0]
            msg = f"no response to {method}"
            raise RTSPError(msg) from exception
        if not response.ok:
            method = request_line.split(" ", 1)[0]
            msg = f"{method} failed: {response.status_line}"
            raise RTSPError(msg)
        return response

    async def __read_loop(self) -> None:
        assert self._reader is not None  # noqa: S101
        buf = bytearray()
        while True:
            chunk = await self._reader.read(4096)
            if not chunk:
                return  # __supervise() logs the offline transition
            buf.extend(chunk)
            self.__drain_buffer(buf)

    def __drain_buffer(self, buf: bytearray) -> None:
        while buf:
            if buf[0] == _INTERLEAVE_MARKER:
                if len(buf) < _INTERLEAVE_HEADER_BYTES:
                    return
                channel = buf[1]
                length = (buf[2] << 8) | buf[3]
                total_len = _INTERLEAVE_HEADER_BYTES + length
                if len(buf) < total_len:
                    return
                frame = bytes(buf[_INTERLEAVE_HEADER_BYTES : total_len])
                del buf[:total_len]
                for queue in self._subscribers.get(channel, ()):
                    self.__enqueue_dropping_oldest(queue, channel, frame)
            elif buf[:4] == b"RTSP":
                header_end = bytes(buf).find(b"\r\n\r\n")
                if header_end == -1:
                    return
                header_text = bytes(buf[:header_end]).decode(errors="replace")
                content_length = _parse_content_length(header_text)
                total_len = header_end + 4 + content_length
                if len(buf) < total_len:
                    return
                body = bytes(buf[header_end + 4 : total_len]).decode(errors="replace")
                del buf[:total_len]
                response = _Response.parse(header_text, body)
                LOGGER.debug("RTSP recv: %s %s", response.status_line, response.headers)
                WIRE_LOGGER.debug("RTSP recv: %s %s", response.status_line, header_text)
                future = self._pending.pop(response.cseq, None)
                if future is not None and not future.done():
                    future.set_result(response)
            else:
                del buf[:1]

    @staticmethod
    def __enqueue_dropping_oldest(queue: asyncio.Queue[tuple[int, bytes]], channel: int, frame: bytes) -> None:
        # Drop the oldest frame first so a slow consumer catches back up to live instead of growing a stale backlog.
        if queue.full():
            with contextlib.suppress(asyncio.QueueEmpty):
                queue.get_nowait()
        with contextlib.suppress(asyncio.QueueFull):
            queue.put_nowait((channel, frame))


class TalkSession:
    """Short-lived push-to-talk connection, kept separate since its channel (0x02) could collide with track2's audio."""

    def __init__(self, host: str) -> None:
        """Initialize with the camera's LAN host/IP."""
        self._host = host
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._cseq = 0

    async def __aenter__(self) -> Self:
        """Open the connection and issue AudioCtlCmd:OPEN."""
        self._reader, self._writer = await asyncio.open_connection(self._host, RTSP_PORT)
        url = f"rtsp://{self._host}{RTSP_PATH}"
        await self.__simple(f"OPTIONS {url} RTSP/1.0")
        await self.__simple(
            f"USER_CMD_SET {url} RTSP/1.0",
            {"Content-length": "strlen(Content-type)", "Content-type": "AudioCtlCmd:OPEN"},
        )
        return self

    async def __aexit__(self, *_exc: object) -> None:
        """Issue AudioCtlCmd:CLOSE and close the connection."""
        with contextlib.suppress(RTSPError, TimeoutError):
            await self.__simple(
                f"USER_CMD_SET rtsp://{self._host}{RTSP_PATH} RTSP/1.0",
                {"Content-length": "strlen(Content-type)", "Content-type": "AudioCtlCmd:CLOSE"},
            )
        if self._writer is not None:
            self._writer.close()
            with contextlib.suppress(OSError, TimeoutError):
                await asyncio.wait_for(self._writer.wait_closed(), timeout=_CANCEL_TIMEOUT_S)

    async def send_pcm16(self, pcm16_8khz_mono: bytes) -> None:
        """Push PCM16 audio paced to real time; each frame is `$`+channel+u16 length+12-byte gap+320-byte payload."""
        assert self._writer is not None  # noqa: S101
        LOGGER.debug("RTSP talk: sending %d bytes of PCM16 audio to %s", len(pcm16_8khz_mono), self._host)
        header_prefix = bytes([_INTERLEAVE_MARKER, _TALK_CHANNEL])
        length_field = (_TALK_GAP_BYTES + _TALK_CHUNK_BYTES).to_bytes(2, "little")
        gap = bytes(_TALK_GAP_BYTES)
        chunk_duration_s = (_TALK_CHUNK_BYTES // 2) / TALK_SAMPLE_RATE_HZ
        loop = asyncio.get_running_loop()
        start = loop.time()
        offsets = range(0, len(pcm16_8khz_mono), _TALK_CHUNK_BYTES)
        for sent, offset in enumerate(offsets, start=1):
            chunk = pcm16_8khz_mono[offset : offset + _TALK_CHUNK_BYTES]
            if len(chunk) < _TALK_CHUNK_BYTES:
                chunk = chunk + bytes(_TALK_CHUNK_BYTES - len(chunk))
            self._writer.write(header_prefix + length_field + gap + chunk)
            await self._writer.drain()
            delay = (start + sent * chunk_duration_s) - loop.time()
            if delay > 0:
                await asyncio.sleep(delay)

    def __next_cseq(self) -> int:
        self._cseq += 1
        return self._cseq

    async def __simple(self, request_line: str, extra_headers: dict[str, str] | None = None) -> _Response:
        if self._reader is None or self._writer is None:
            msg = "talk session is not connected"
            raise RTSPError(msg)
        return await _simple_request(self._reader, self._writer, self.__next_cseq(), request_line, extra_headers)
