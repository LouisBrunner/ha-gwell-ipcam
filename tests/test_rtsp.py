"""Wire-format regression tests for custom_components/gwell_ipcam/rtsp.py; plain pytest, no HA test harness needed."""

from __future__ import annotations

import asyncio
import contextlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.gwell_ipcam import rtsp as sc


def test_parse_content_length_returns_zero_when_absent():
    assert sc._parse_content_length("RTSP/1.0 200 OK\r\nCSeq: 1") == 0


def test_parse_content_length_accepts_a_valid_value():
    assert sc._parse_content_length("RTSP/1.0 200 OK\r\nContent-Length: 42") == 42


def test_parse_content_length_rejects_an_unparsable_value():
    with pytest.raises(sc.RTSPError, match="unparsable"):
        sc._parse_content_length("RTSP/1.0 200 OK\r\nContent-Length: not-a-number")


def test_parse_content_length_rejects_a_negative_value():
    """A negative value would desync the buffer parser (`del buf[:total_len]` deletes too few bytes)."""
    with pytest.raises(sc.RTSPError, match="implausible"):
        sc._parse_content_length("RTSP/1.0 200 OK\r\nContent-Length: -1")


def test_parse_content_length_accepts_the_maximum_allowed_value():
    header = f"RTSP/1.0 200 OK\r\nContent-Length: {sc._MAX_CONTENT_LENGTH}"
    assert sc._parse_content_length(header) == sc._MAX_CONTENT_LENGTH


def test_parse_content_length_rejects_an_implausibly_large_value():
    """An unbounded value would grow the read buffer indefinitely waiting for a body that never arrives."""
    header = f"RTSP/1.0 200 OK\r\nContent-Length: {sc._MAX_CONTENT_LENGTH + 1}"
    with pytest.raises(sc.RTSPError, match="implausible"):
        sc._parse_content_length(header)


def test_parse_cseq_accepts_a_valid_value():
    assert sc._parse_cseq("42") == 42


def test_parse_cseq_rejects_an_unparsable_value():
    """A malformed CSeq used to raise a bare ValueError that killed the reconnect supervisor permanently."""
    with pytest.raises(sc.RTSPError, match="unparsable"):
        sc._parse_cseq("not-a-number")


@pytest.mark.asyncio
async def test_a_failed_handshake_step_closes_the_writer_instead_of_leaking_the_socket():
    """A failure partway through OPTIONS/DESCRIBE/SETUP/PLAY must not leave the TCP connection open and unreferenced."""
    writer = MagicMock()
    writer.wait_closed = AsyncMock()
    session = sc.RTSPSession("192.0.2.10")
    with (
        patch("asyncio.open_connection", AsyncMock(return_value=(MagicMock(), writer))),
        patch.object(sc, "_simple_request", AsyncMock(side_effect=sc.RTSPError("boom"))),
    ):
        await session._RTSPSession__try_connect_once()
    assert writer.close.called
    assert not session.online


class _HangingReader:
    async def read(self, _n: int) -> bytes:
        await asyncio.Future()
        raise AssertionError("unreachable")  # pragma: no cover


@pytest.mark.asyncio
async def test_read_loop_raises_when_the_camera_goes_silent_without_closing_the_socket():
    """A stalled-but-not-closed connection must not hang the reader forever, or `online` never flips to False."""
    session = sc.RTSPSession("192.0.2.10")
    session._RTSPSession__reader = _HangingReader()
    with (
        patch.object(sc, "_IDLE_READ_TIMEOUT_S", 0.05),
        pytest.raises(sc.RTSPError, match="no data received"),
    ):
        await session._RTSPSession__read_loop()


@pytest.mark.asyncio
async def test_disconnect_does_not_hang_after_the_read_loop_already_raised():
    """`__disconnect()` cancels the same already-finished `__reader_task` `__supervise` just awaited -- mustn't hang."""
    session = sc.RTSPSession("192.0.2.10")

    async def _already_failed() -> None:
        msg = "no data received for 20s"
        raise sc.RTSPError(msg)

    reader_task = asyncio.ensure_future(_already_failed())
    with pytest.raises(sc.RTSPError, match="no data received"):
        await reader_task  # mirrors __supervise's `try: await self.__reader_task except ...`
    session._RTSPSession__reader_task = reader_task

    writer = MagicMock()
    writer.wait_closed = AsyncMock()
    session._RTSPSession__writer = writer

    await asyncio.wait_for(session._RTSPSession__disconnect(), timeout=2)


@pytest.mark.asyncio
async def test_supervise_survives_an_unanticipated_bug_instead_of_dying_silently():
    """An exception type nobody anticipated must not permanently kill the reconnect loop unnoticed."""
    session = sc.RTSPSession("192.0.2.10")
    session._RTSPSession__online = False

    async def _boom() -> None:
        msg = "not a network error at all"
        raise TypeError(msg)

    with patch.object(session, "_RTSPSession__try_connect_once", _boom):
        task = asyncio.ensure_future(session._RTSPSession__supervise())
        try:
            await asyncio.sleep(0.05)
            assert not task.done(), f"supervisor task died: {task.exception() if task.done() else None}"
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
