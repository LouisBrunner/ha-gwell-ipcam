"""Wire-format regression tests for custom_components/gwell_ipcam/rtsp.py; plain pytest, no HA test harness needed."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.gwell_ipcam import rtsp as sc


def test_parse_content_length_returns_zero_when_absent():
    assert sc._parse_content_length("RTSP/1.0 200 OK\r\nCSeq: 1") == 0


def test_parse_content_length_accepts_a_valid_value():
    assert sc._parse_content_length("RTSP/1.0 200 OK\r\nContent-Length: 42") == 42


def test_parse_content_length_rejects_an_unparseable_value():
    with pytest.raises(sc.RTSPError, match="unparseable"):
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


def test_parse_cseq_rejects_an_unparseable_value():
    """A malformed CSeq used to raise a bare ValueError that killed the reconnect supervisor permanently."""
    with pytest.raises(sc.RTSPError, match="unparseable"):
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
