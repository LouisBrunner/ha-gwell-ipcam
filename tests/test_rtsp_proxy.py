"""Tests for the local RTSP proxy's port stability (custom_components/gwell_ipcam/rtsp_proxy.py)."""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.gwell_ipcam import rtsp_proxy as sc

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


def make_proxy(hass: object = None, entry_id: str = "e") -> sc.RTSPProxyServer:
    return sc.RTSPProxyServer(MagicMock(), host="127.0.0.1", hass=hass or object(), entry_id=entry_id)


@pytest.mark.asyncio
async def test_port_allocator_gives_a_fresh_entry_the_next_high_water_mark_port(mock_store):  # noqa: ARG001
    assert await sc._ProxyPortAllocator(object(), "entry-a").async_get_port() == sc._PROXY_PORT_BASE
    assert await sc._ProxyPortAllocator(object(), "entry-b").async_get_port() == sc._PROXY_PORT_BASE + 1


@pytest.mark.asyncio
async def test_port_allocator_reuses_the_same_entrys_port_on_a_later_call(mock_store):  # noqa: ARG001
    allocator = sc._ProxyPortAllocator(object(), "entry-a")
    first = await allocator.async_get_port()
    assert await allocator.async_get_port() == first


@pytest.mark.asyncio
async def test_port_allocator_survives_a_fresh_instance_via_the_store(mock_store):  # noqa: ARG001
    """The whole point of persisting to a Store: a new allocator instance still returns the same port."""
    first = await sc._ProxyPortAllocator(object(), "entry-a").async_get_port()
    second = await sc._ProxyPortAllocator(object(), "entry-a").async_get_port()
    assert first == second


@pytest.mark.asyncio
async def test_start_binds_the_entrys_persisted_port(mock_store):  # noqa: ARG001
    proxy = make_proxy(entry_id="entry-a")
    with patch("custom_components.gwell_ipcam.rtsp_proxy.asyncio.start_server", AsyncMock()) as start_server:
        await proxy.start()
    assert start_server.call_args.kwargs["port"] == sc._PROXY_PORT_BASE


@pytest.mark.asyncio
async def test_start_raises_on_a_taken_port_instead_of_falling_back_to_a_random_one(mock_store):  # noqa: ARG001
    """A silent fallback to `port=0` would reproduce the exact stale-URL bug this allocator exists to prevent."""
    proxy = make_proxy(entry_id="entry-a")
    with (
        patch(
            "custom_components.gwell_ipcam.rtsp_proxy.asyncio.start_server",
            AsyncMock(side_effect=OSError("address already in use")),
        ),
        pytest.raises(OSError, match="address already in use"),
    ):
        await proxy.start()


CAMERA_SDP = "v=0\r\ns=cam\r\nm=video 0 RTP/AVP 96\r\na=control:track1\r\nm=audio 0 RTP/AVP 8\r\na=control:track2\r\n"
ONVIF_DESCRIBE_HEADER = "Require: www.onvif.org/ver20/backchannel\r\n"


@contextlib.asynccontextmanager
async def idle_frames(_channels: object) -> AsyncIterator[AsyncIterator[None]]:
    async def idle() -> AsyncIterator[None]:
        await asyncio.sleep(3600)
        yield

    yield idle()


async def rtsp_request(reader, writer, request):
    writer.write(request.encode())
    head = (await reader.readuntil(b"\r\n\r\n")).decode()
    length = next((int(h.split(":")[1]) for h in head.split("\r\n") if h.lower().startswith("content-length")), 0)
    return head + (await reader.readexactly(length)).decode()


@pytest.mark.asyncio
@pytest.mark.usefixtures("socket_enabled")
async def test_backchannel_track_is_offered_only_on_request_and_its_audio_reaches_the_talk_session(mock_store):  # noqa: ARG001
    session = MagicMock(online=True, sdp=CAMERA_SDP, subscribe=idle_frames)
    proxy = sc.RTSPProxyServer(session, host="127.0.0.1", hass=object(), entry_id="entry-a")
    talk = MagicMock(send_pcm16=AsyncMock())
    talk.__aenter__.return_value = talk
    with (
        patch.object(sc._ProxyPortAllocator, "async_get_port", AsyncMock(return_value=0)),
        patch.object(sc, "TalkSession", MagicMock(return_value=talk)) as talk_cls,
    ):
        await proxy.start()
        reader, writer = await asyncio.open_connection("127.0.0.1", proxy.port)
        url = f"rtsp://127.0.0.1:{proxy.port}/onvif1"
        plain = await rtsp_request(reader, writer, f"DESCRIBE {url} RTSP/1.0\r\nCSeq: 1\r\n\r\n")
        onvif = await rtsp_request(reader, writer, f"DESCRIBE {url} RTSP/1.0\r\nCSeq: 2\r\n{ONVIF_DESCRIBE_HEADER}\r\n")
        setup = await rtsp_request(reader, writer, f"SETUP {url}/track3 RTSP/1.0\r\nCSeq: 3\r\nTransport: x\r\n\r\n")
        await rtsp_request(reader, writer, f"PLAY {url} RTSP/1.0\r\nCSeq: 4\r\nSession: gwell1\r\n\r\n")
        rtp = bytes([0x80, 8, 0, 1, 0, 0, 0, 0, 0, 0, 0, 1]) + b"\xd5" * 160
        for _ in range(3):
            writer.write(bytes([0x24, 4]) + len(rtp).to_bytes(2, "big") + rtp)
        await writer.drain()
        await asyncio.sleep(0.2)
        writer.close()
        await proxy.stop()
    assert "sendonly" not in plain
    assert "a=rtpmap:8 PCMA/8000\r\na=sendonly" in onvif
    assert "interleaved=4-5" in setup
    talk_cls.assert_called_once_with("127.0.0.1")
    assert [call.args[0] for call in talk.send_pcm16.await_args_list] == [(8).to_bytes(2, "little") * 160] * 3
    assert all(call.kwargs == {"paced": False} for call in talk.send_pcm16.await_args_list)
