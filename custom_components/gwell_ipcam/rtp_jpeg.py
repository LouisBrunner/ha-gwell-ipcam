"""
Minimal RFC 2435 (RTP/JPEG) payloader for the offline-fallback stream.

Only handles what we produce ourselves: a baseline JFIF JPEG at 4:2:0 subsampling and a
quality factor < 100, so the receiver derives the standard quantization tables from the Q
field alone -- no quantization-table header needed (see RFC 2435 SS3.1, SS3.1.8). This is
the same simplification ffmpeg's own MJPEG-over-RTP muxer relies on for a plain baseline
frame, and is not yet live-verified against a real RTSP client here -- test before trusting.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

_MARKER_PREFIX = 0xFF
_EOI = b"\xff\xd9"
_SOS_MARKER = 0xDA
_STANDALONE_MARKERS = {0xD8, 0x01} | set(range(0xD0, 0xD8))

_MAX_FRAGMENT_BYTES = 1400
_RTP_VERSION_BYTE = 0x80
_RTP_PAYLOAD_TYPE_JPEG = 26
RTP_CLOCK_HZ = 90000


class JPEGParseError(Exception):
    """Raised when a JPEG doesn't have the structure this minimal payloader expects."""


@dataclass
class FrameParams:
    """Everything needed to payload one JPEG frame, besides the JPEG bytes themselves."""

    width: int
    height: int
    quality: int
    sequence_start: int
    timestamp: int
    ssrc: int = 1


def _scan_data(jpeg_bytes: bytes) -> bytes:
    pos = 2  # skip SOI (0xFFD8)
    while pos < len(jpeg_bytes) - 1:
        if jpeg_bytes[pos] != _MARKER_PREFIX:
            msg = f"expected a marker at offset {pos}"
            raise JPEGParseError(msg)
        marker = jpeg_bytes[pos + 1]
        if marker in _STANDALONE_MARKERS:
            pos += 2
            continue
        if marker == _SOS_MARKER:
            length = struct.unpack_from(">H", jpeg_bytes, pos + 2)[0]
            scan_start = pos + 2 + length
            scan_end = jpeg_bytes.rfind(_EOI)
            if scan_end == -1 or scan_end < scan_start:
                msg = "no EOI marker found after SOS"
                raise JPEGParseError(msg)
            return jpeg_bytes[scan_start:scan_end]
        length = struct.unpack_from(">H", jpeg_bytes, pos + 2)[0]
        pos += 2 + length
    msg = "no SOS marker found"
    raise JPEGParseError(msg)


def _rtp_header(sequence: int, timestamp: int, ssrc: int, *, marker: bool) -> bytes:
    second_byte = (0x80 if marker else 0x00) | _RTP_PAYLOAD_TYPE_JPEG
    return struct.pack("!BBHII", _RTP_VERSION_BYTE, second_byte, sequence & 0xFFFF, timestamp & 0xFFFFFFFF, ssrc)


def build_packets(jpeg_bytes: bytes, params: FrameParams) -> list[bytes]:
    """Fragment one 4:2:0 baseline JPEG frame into RTP/JPEG (RFC 2435) packets."""
    scan = _scan_data(jpeg_bytes)
    packets: list[bytes] = []
    sequence = params.sequence_start
    for offset in range(0, len(scan), _MAX_FRAGMENT_BYTES):
        fragment = scan[offset : offset + _MAX_FRAGMENT_BYTES]
        is_last = offset + len(fragment) >= len(scan)
        jpeg_header = struct.pack(
            "!BBBBBBBB",
            0,  # type-specific
            (offset >> 16) & 0xFF,
            (offset >> 8) & 0xFF,
            offset & 0xFF,
            0,  # type 0: 4:2:0, no restart markers
            params.quality,
            params.width // 8,
            params.height // 8,
        )
        rtp_header = _rtp_header(sequence, params.timestamp, params.ssrc, marker=is_last)
        packets.append(rtp_header + jpeg_header + fragment)
        sequence += 1
    return packets
