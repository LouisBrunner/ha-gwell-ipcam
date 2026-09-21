"""Tests for the local RTSP proxy's port stability (custom_components/gwell_ipcam/rtsp_proxy.py)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.gwell_ipcam import rtsp_proxy as sc


def make_proxy(hass: object = None, entry_id: str = "e") -> sc.RTSPProxyServer:
    return sc.RTSPProxyServer(MagicMock(), hass=hass or object(), entry_id=entry_id)


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
