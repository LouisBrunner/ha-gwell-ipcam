import io
import sys
from types import SimpleNamespace

import pytest

from custom_components.gwell_ipcam import audio


class FakeTTSStream:
    async def async_stream_result(self):
        yield b"ab"
        yield b"cd"


@pytest.fixture
def tts_module(monkeypatch):
    module = SimpleNamespace(async_get_stream=lambda _hass, _token: None)
    monkeypatch.setitem(sys.modules, "homeassistant.components.tts", module)
    return module


def patch_decoder(monkeypatch):
    calls = []

    def decode(source, rate_hz):
        calls.append((source.getvalue() if isinstance(source, io.BytesIO) else source, rate_hz))
        return b"pcm"

    monkeypatch.setattr(audio, "_decode_to_pcm16", decode)
    return calls


async def test_media_source_is_resolved_for_the_target_entity_then_made_absolute(hass, monkeypatch):
    resolve_calls = []

    async def resolve(_hass, media_id, entity_id):
        resolve_calls.append((media_id, entity_id))
        return SimpleNamespace(url="/media/local/alarm.mp3")

    monkeypatch.setattr(audio.media_source, "async_resolve_media", resolve)
    monkeypatch.setattr(audio, "async_process_play_media_url", lambda _hass, url: f"https://ha.example{url}?authSig=x")
    calls = patch_decoder(monkeypatch)

    pcm = await audio.async_media_id_to_pcm16_8k(
        hass, "media-source://media_source/local/alarm.mp3", "media_player.cam"
    )

    assert pcm == b"pcm"
    assert resolve_calls == [("media-source://media_source/local/alarm.mp3", "media_player.cam")]
    assert calls == [("https://ha.example/media/local/alarm.mp3?authSig=x", audio.TALK_SAMPLE_RATE_HZ)]


async def test_tts_proxy_audio_is_read_in_process_not_fetched_over_http(hass, monkeypatch, tts_module):
    tokens = []
    monkeypatch.setattr(tts_module, "async_get_stream", lambda _hass, token: tokens.append(token) or FakeTTSStream())
    calls = patch_decoder(monkeypatch)

    pcm = await audio.async_media_id_to_pcm16_8k(hass, "https://ha.example/api/tts_proxy/abc.mp3", "media_player.cam")

    assert pcm == b"pcm"
    assert tokens == ["abc.mp3"]
    assert calls == [(b"abcd", audio.TALK_SAMPLE_RATE_HZ)]


@pytest.mark.usefixtures("tts_module")
async def test_unknown_tts_token_falls_back_to_the_url(hass, monkeypatch):
    monkeypatch.setattr(audio, "async_process_play_media_url", lambda _hass, url: f"https://ha.example{url}")
    calls = patch_decoder(monkeypatch)

    await audio.async_media_id_to_pcm16_8k(hass, "/api/tts_proxy/gone.mp3", "media_player.cam")

    assert calls == [("https://ha.example/api/tts_proxy/gone.mp3", audio.TALK_SAMPLE_RATE_HZ)]
