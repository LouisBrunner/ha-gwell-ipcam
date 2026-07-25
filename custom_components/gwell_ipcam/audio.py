"""Audio conversion helpers shared by media_player and assist_satellite (uses PyAV, an existing HA dependency)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import av
from homeassistant.components import media_source
from homeassistant.helpers.network import get_url

from .const import LOGGER, SATELLITE_SAMPLE_RATE_HZ, TALK_SAMPLE_RATE_HZ
from .rtsp import AUDIO_CHANNELS

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from homeassistant.core import HomeAssistant

    from .rtsp import RTSPSession

_RTP_HEADER_BYTES = 12


async def async_media_id_to_pcm16_8k(hass: HomeAssistant, media_id: str) -> bytes:
    """Resolve and decode arbitrary playable media into 8kHz mono PCM16 for the talk channel."""
    url = media_id
    if media_source.is_media_source_id(url):
        resolved = await media_source.async_resolve_media(hass, url, None)
        url = resolved.url
    if url.startswith("/"):
        url = f"{get_url(hass)}{url}"
    return await hass.async_add_executor_job(lambda: _decode_to_pcm16(url, TALK_SAMPLE_RATE_HZ))


def _decode_to_pcm16(url: str, rate_hz: int) -> bytes:
    resampler = av.AudioResampler(format="s16", layout="mono", rate=rate_hz)
    out = bytearray()
    with av.open(url, timeout=(10.0, 10.0)) as container:
        stream = container.streams.audio[0]
        for packet in container.demux(stream):
            for frame in packet.decode():
                for resampled in resampler.resample(frame):
                    out += resampled.to_ndarray().tobytes()
    return bytes(out)


async def async_listen_stream_16k(session: RTSPSession) -> AsyncIterator[bytes]:
    """Yield 16kHz mono PCM16 chunks from the camera's listen-audio track, as Assist pipelines expect."""
    codec_context = av.CodecContext.create("pcm_alaw", "r")
    codec_context.sample_rate = TALK_SAMPLE_RATE_HZ
    codec_context.layout = "mono"
    resampler = av.AudioResampler(format="s16", layout="mono", rate=SATELLITE_SAMPLE_RATE_HZ)
    rtp_packets = 0
    pcm_bytes = 0
    LOGGER.debug("Listening for camera mic audio")
    try:
        async with session.subscribe(AUDIO_CHANNELS) as frames:
            async for channel, payload in frames:
                if channel != AUDIO_CHANNELS[0] or len(payload) <= _RTP_HEADER_BYTES:
                    continue
                rtp_packets += 1
                packet = av.Packet(payload[_RTP_HEADER_BYTES:])
                for frame in codec_context.decode(packet):
                    for resampled in resampler.resample(frame):
                        chunk = resampled.to_ndarray().tobytes()
                        pcm_bytes += len(chunk)
                        yield chunk
    finally:
        LOGGER.debug("Stopped listening for camera mic audio (%d RTP packets, %d PCM bytes)", rtp_packets, pcm_bytes)
