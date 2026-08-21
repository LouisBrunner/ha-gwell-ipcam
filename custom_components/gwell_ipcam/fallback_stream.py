"""Fallback video: last-known frame with the connection error overlaid."""

from __future__ import annotations

import base64
import io
import itertools
import struct
import time
from fractions import Fraction
from typing import TYPE_CHECKING

import av
from homeassistant.helpers.storage import Store
from PIL import Image, ImageDraw, ImageFont

from .const import DOMAIN, LOGGER
from .rtsp import VIDEO_CHANNELS

if TYPE_CHECKING:
    from collections.abc import Generator

    from homeassistant.core import HomeAssistant

FALLBACK_FPS = 2
_FALLBACK_SIZE = (640, 480)


class FrameCache:
    """Decodes real H264 frames opportunistically, to keep a last-known-good still for the fallback background."""

    _RTP_HEADER_BYTES = 12

    _RTP_EXTENSION_BIT = 0x10
    _RTP_CSRC_COUNT_MASK = 0x0F
    _NAL_TYPE_MASK = 0x1F
    _NAL_TYPE_FU_A = 28
    _FU_START_BIT = 0x80
    _FU_END_BIT = 0x40
    _ANNEXB_START_CODE = b"\x00\x00\x00\x01"

    _MAX_DECODED_FRAME_DIMENSION_PX = 1920

    _FONT_SIZE_PX = 20
    _LINE_SPACING_PX = 6
    _PADDING_PX = 14
    _TEXT_COLOR = (255, 80, 80)
    _BAR_COLOR = (0, 0, 0, 180)
    _TEXT_MARGIN_PX = 24

    _FONT = ImageFont.load_default(size=_FONT_SIZE_PX)

    _PERSIST_INTERVAL_S = 30.0
    _PERSIST_JPEG_QUALITY = 70

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        """Initialize with no cached frame; `async_load_persisted()`/`feed()` populate it."""
        self.__decoder = av.CodecContext.create("h264", "r")
        self.__last_frame: Image.Image | None = None
        self.__hass = hass
        self.__store: Store[dict[str, str]] = Store(hass, version=1, key=f"{DOMAIN}.{entry_id}.last_frame")
        self.__last_persisted_monotonic = 0.0
        self.__fu_buffer: bytearray | None = None

    async def async_load_persisted(self) -> None:
        """Load the last real frame saved to disk (if any), so a reload doesn't start from a blank placeholder."""
        data = await self.__store.async_load()
        if data is None:
            return
        try:
            image = Image.open(io.BytesIO(base64.b64decode(data["jpeg_b64"])))
            image.load()
        except Exception:  # noqa: BLE001
            LOGGER.debug("Failed to load the persisted fallback frame, starting blank", exc_info=True)
            return
        self.__last_frame = image

    @staticmethod
    def __rtp_payload_offset(payload: bytes) -> int | None:
        if len(payload) < FrameCache._RTP_HEADER_BYTES:
            return None
        cc = payload[0] & FrameCache._RTP_CSRC_COUNT_MASK
        offset = FrameCache._RTP_HEADER_BYTES + 4 * cc
        if payload[0] & FrameCache._RTP_EXTENSION_BIT:
            if len(payload) < offset + 4:
                return None
            ext_length_words = struct.unpack_from("!H", payload, offset + 2)[0]
            offset += 4 + ext_length_words * 4
        return offset

    @staticmethod
    def __split_long_word(
        word: str, *, font: ImageFont.ImageFont | ImageFont.FreeTypeFont, max_width: int
    ) -> list[str]:
        chunks: list[str] = []
        current = ""
        for char in word:
            candidate = current + char
            if current and font.getlength(candidate) > max_width:
                chunks.append(current)
                current = char
            else:
                current = candidate
        if current:
            chunks.append(current)
        return chunks

    @staticmethod
    def __wrap_to_width(text: str, *, font: ImageFont.ImageFont | ImageFont.FreeTypeFont, max_width: int) -> list[str]:
        words = text.split()
        if not words:
            return [text]
        lines: list[str] = []
        current = ""
        for word in words:
            candidate = f"{current} {word}" if current else word
            if font.getlength(candidate) <= max_width:
                current = candidate
                continue
            if current:
                lines.append(current)
                current = ""
            if font.getlength(word) <= max_width:
                current = word
            else:
                *full_chunks, current = FrameCache.__split_long_word(word, font=font, max_width=max_width)
                lines.extend(full_chunks)
        if current:
            lines.append(current)
        return lines

    def feed(self, channel: int, payload: bytes) -> None:
        """Feed one interleaved RTP payload from the real camera's video channel (a no-op for other channels)."""
        if channel != VIDEO_CHANNELS[0]:
            return
        offset = self.__rtp_payload_offset(payload)
        if offset is None or len(payload) <= offset:
            return
        nal = self.__depacketize(payload[offset:])
        if nal is None:
            return
        try:
            packets = self.__decoder.parse(self._ANNEXB_START_CODE + nal)
            for packet in packets:
                for frame in self.__decoder.decode(packet):
                    if (
                        frame.width > self._MAX_DECODED_FRAME_DIMENSION_PX
                        or frame.height > self._MAX_DECODED_FRAME_DIMENSION_PX
                    ):
                        LOGGER.warning(
                            "Dropping an oversized decoded frame (%dx%d) for the fallback cache",
                            frame.width,
                            frame.height,
                        )
                        continue
                    self.__last_frame = frame.to_image()
                    self.__maybe_persist()
        except av.FFmpegError:
            LOGGER.debug("Failed to decode a frame for the fallback cache", exc_info=True)

    def __depacketize(self, rtp_payload: bytes) -> bytes | None:
        nal_header = rtp_payload[0]
        nal_type = nal_header & self._NAL_TYPE_MASK
        if nal_type != self._NAL_TYPE_FU_A:
            return rtp_payload
        if len(rtp_payload) < 2:  # noqa: PLR2004
            return None
        fu_header = rtp_payload[1]
        fragment = rtp_payload[2:]
        if fu_header & self._FU_START_BIT:
            reconstructed_header = (nal_header & 0xE0) | (fu_header & self._NAL_TYPE_MASK)
            self.__fu_buffer = bytearray([reconstructed_header]) + fragment
        elif self.__fu_buffer is not None:
            self.__fu_buffer.extend(fragment)
        else:
            return None
        if not fu_header & self._FU_END_BIT:
            return None
        nal = bytes(self.__fu_buffer)
        self.__fu_buffer = None
        return nal

    def __maybe_persist(self) -> None:
        now = time.monotonic()
        if now - self.__last_persisted_monotonic < self._PERSIST_INTERVAL_S:
            return
        self.__last_persisted_monotonic = now
        self.__hass.loop.call_soon_threadsafe(
            self.__hass.async_create_background_task, self.__async_persist(), f"{DOMAIN}-persist-last-frame"
        )

    async def __async_persist(self) -> None:
        last_frame = self.__last_frame
        if last_frame is None:
            return
        jpeg_b64 = await self.__hass.async_add_executor_job(self.__encode_jpeg_b64, last_frame)
        await self.__store.async_save({"jpeg_b64": jpeg_b64})

    @staticmethod
    def __encode_jpeg_b64(image: Image.Image) -> str:
        buf = io.BytesIO()
        image.convert("RGB").save(buf, format="JPEG", quality=FrameCache._PERSIST_JPEG_QUALITY)
        return base64.b64encode(buf.getvalue()).decode()

    def render(self, error: str) -> Image.Image:
        """Return the last frame (or a black placeholder) with `error` overlaid, centered on a semi-opaque box."""
        image = (
            self.__last_frame.convert("RGB")
            if self.__last_frame is not None
            else Image.new("RGB", _FALLBACK_SIZE, (0, 0, 0))
        )

        overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)

        max_width = image.width - self._TEXT_MARGIN_PX * 2
        lines = self.__wrap_to_width(error, font=self._FONT, max_width=max_width)
        text = "\n".join(lines)
        text_left, text_top, text_right, text_bottom = draw.multiline_textbbox(
            (0, 0), text, font=self._FONT, spacing=self._LINE_SPACING_PX
        )
        text_width, text_height = text_right - text_left, text_bottom - text_top

        box_left = (image.width - text_width) // 2 - self._PADDING_PX
        box_top = (image.height - text_height) // 2 - self._PADDING_PX
        box_right = box_left + text_width + self._PADDING_PX * 2
        box_bottom = box_top + text_height + self._PADDING_PX * 2
        draw.rectangle([box_left, box_top, box_right, box_bottom], fill=self._BAR_COLOR)
        draw.multiline_text(
            (image.width / 2, image.height / 2),
            text,
            font=self._FONT,
            fill=self._TEXT_COLOR,
            spacing=self._LINE_SPACING_PX,
            align="center",
            anchor="mm",
        )

        return Image.alpha_composite(image.convert("RGBA"), overlay).convert("RGB")


class FallbackEncoder:
    """Encodes rendered fallback frames to Annex-B H264 NAL units, ready for RTP packetization."""

    _RTP_VERSION_BYTE = 0x80
    _RTP_PAYLOAD_TYPE_H264 = 96
    _RTP_CLOCK_HZ = 90000  # RFC 6184

    _TIMESTAMP_STEP = _RTP_CLOCK_HZ // FALLBACK_FPS
    _CHANNEL = VIDEO_CHANNELS[0]
    _SSRC = 0xFA11BACC

    def __init__(self, size: tuple[int, int] = _FALLBACK_SIZE) -> None:
        """Initialize a still-image-tuned H264 encoder at `size`."""
        self.__codec = av.CodecContext.create("h264", "w")
        self.__codec.width, self.__codec.height = size
        self.__codec.pix_fmt = "yuv420p"
        self.__codec.framerate = Fraction(FALLBACK_FPS, 1)
        self.__codec.time_base = Fraction(1, FALLBACK_FPS)
        self.__codec.options = {"tune": "stillimage", "preset": "ultrafast", "g": "1"}
        self.__timestamp = 0
        self.__count = 0
        self.__seq = itertools.count()

    def __encode_image(self, image: Image.Image, *, pts: int) -> list[bytes]:
        frame = av.VideoFrame.from_image(image)
        frame.pts = pts
        nals: list[bytes] = []
        for packet in self.__codec.encode(frame):
            parts = bytes(packet).split(b"\x00\x00\x00\x01")
            nals.extend([p for p in parts if p])
        return nals

    @staticmethod
    def __rtp_packets_for_nal(nal: bytes, *, sequence: int, timestamp: int, ssrc: int, marker: bool) -> bytes:
        second_byte = (0x80 if marker else 0x00) | FallbackEncoder._RTP_PAYLOAD_TYPE_H264
        header = struct.pack(
            "!BBHII",
            FallbackEncoder._RTP_VERSION_BYTE,
            second_byte,
            sequence & 0xFFFF,
            timestamp & 0xFFFFFFFF,
            ssrc & 0xFFFFFFFF,
        )
        return header + nal

    def get_count(self) -> int:
        """Return the number of frames encoded so far."""
        return self.__count

    def encode(self, image: Image.Image) -> Generator[bytes]:
        """Encode the given image to H264 NAL units and yield RTP packets."""
        nals = self.__encode_image(image, pts=self.__count)
        for i, nal in enumerate(nals):
            packet = self.__rtp_packets_for_nal(
                nal,
                sequence=next(self.__seq),
                timestamp=self.__timestamp,
                ssrc=self._SSRC,
                marker=(i == len(nals) - 1),
            )
            header = bytes([0x24, self._CHANNEL]) + len(packet).to_bytes(2, "big")
            yield header + packet
        self.__count += 1
        self.__timestamp = (self.__timestamp + self._TIMESTAMP_STEP) & 0xFFFFFFFF
