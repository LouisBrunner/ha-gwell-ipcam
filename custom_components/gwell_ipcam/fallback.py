"""
Fallback-image rendering: last-known-good frame with the current error overlaid.

Mirrors rtsp-fixer's `overlayError` -- word-wrapped red text on a semi-transparent black
bar, bottom-anchored, over the last successfully-grabbed frame (or a black placeholder if
none has ever been grabbed). Used by both the camera snapshot path and the RTSP proxy's
offline fallback stream, so both show the same image.
"""

from __future__ import annotations

import io
import textwrap

from PIL import Image, ImageDraw

Image.init()  # ensure the JPEG codec is registered before the first save() call

PLACEHOLDER_SIZE = (640, 480)
JPEG_QUALITY = 75  # kept < 100 so the RTP/JPEG payloader can rely on standard quant tables

_FONT_CHAR_WIDTH_PX = 6
_LINE_HEIGHT_PX = 16
_PADDING_PX = 10
_TEXT_COLOR = (255, 80, 80)
_BAR_COLOR = (0, 0, 0, 180)


class FrameCache:
    """Holds the last successfully-grabbed JPEG frame, for use as a fallback background."""

    def __init__(self) -> None:
        """Initialize with no cached frame."""
        self._last_good: bytes | None = None

    def update(self, jpeg_bytes: bytes) -> None:
        """Record a freshly-grabbed frame as the new fallback background."""
        self._last_good = jpeg_bytes

    def render_error(self, message: str) -> tuple[bytes, int, int]:
        """Return (jpeg_bytes, width, height) for the last-known frame with `message` overlaid."""
        if self._last_good is not None:
            image = Image.open(io.BytesIO(self._last_good)).convert("RGB")
        else:
            image = Image.new("RGB", PLACEHOLDER_SIZE, (0, 0, 0))

        max_chars = max(image.width // _FONT_CHAR_WIDTH_PX, 1)
        lines = textwrap.wrap(message, width=max_chars) or [message]

        bar_height = len(lines) * _LINE_HEIGHT_PX + _PADDING_PX * 2
        overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)
        bar_top = image.height - bar_height
        draw.rectangle([0, bar_top, image.width, image.height], fill=_BAR_COLOR)
        for i, line in enumerate(lines):
            draw.text((_PADDING_PX, bar_top + _PADDING_PX + i * _LINE_HEIGHT_PX), line, fill=_TEXT_COLOR)

        composited = Image.alpha_composite(image.convert("RGBA"), overlay).convert("RGB")
        buf = io.BytesIO()
        composited.save(buf, format="JPEG", quality=JPEG_QUALITY, subsampling=2)
        return buf.getvalue(), composited.width, composited.height
