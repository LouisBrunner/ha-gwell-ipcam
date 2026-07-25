"""API client for Gwell (Sricam/ieGeek) IP cameras. See docs/PROTOCOL.md for the wire format."""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import random
import socket
import struct
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from datetime import time as dtime
from typing import TYPE_CHECKING

from cryptography.hazmat.decrepit.ciphers.algorithms import TripleDES
from cryptography.hazmat.primitives.ciphers import Cipher, modes
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DEFAULT_PORT, LOGGER, RTSP_PATH, WIRE_LOGGER
from .rtsp import RTSPSession, TalkSession
from .rtsp_proxy import RTSPProxyServer

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable
    from typing import Any

    from homeassistant.core import HomeAssistant

# -- setting IDs (iSetNPCSettings dump, see docs/PROTOCOL.md) ---------------

SETTING_REMOTE_DEFENCE = 0
SETTING_BUZZER = 1
SETTING_MOTION_DETECT = 2
SETTING_RECORD_TYPE = 3
SETTING_REMOTE_RECORD = 4
SETTING_RECORD_PLAN_TIME = 5
SETTING_VIDEO_FORMAT = 8
SETTING_RECORD_TIME = 11
SETTING_NET_TYPE = 13
SETTING_VIDEO_VOLUME = 14
SETTING_IMAGE_FLIP = 24
SETTING_MOTION_SENSITIVITY = 28
SETTING_DEFENCE_SWITCH = 43

RECORD_TYPE_MANUAL = 0

PTZ_DIRECTIONS = ("up", "down", "left", "right")
_PTZ_MIRROR = {"left": "right", "right": "left"}


def map_ptz_direction(direction: str, settings: dict[int, int]) -> str:
    """With `SETTING_IMAGE_FLIP` on, pan directions must be swapped since the motor doesn't follow the flip."""
    if settings.get(SETTING_IMAGE_FLIP, 0) and direction in _PTZ_MIRROR:
        return _PTZ_MIRROR[direction]
    return direction


def encode_record_plan_time(start: dtime, end: dtime) -> int:
    """Pack the Timing record schedule into `SETTING_RECORD_PLAN_TIME`'s wire value (see `MyUtils.convertPlanTime`)."""
    return end.minute | (start.minute << 8) | (end.hour << 16) | (start.hour << 24)


def _decode_plan_hour_minute(hour: int, minute: int) -> dtime | None:
    if hour == 24 and minute == 0:  # noqa: PLR2004 -- the firmware's own "end of day" marker (e.g. default 00:00-24:00)
        return dtime(23, 59)
    if 0 <= hour <= 23 and 0 <= minute <= 59:  # noqa: PLR2004
        return dtime(hour, minute)
    return None


def decode_record_plan_time(value: int) -> tuple[dtime, dtime] | None:
    """Unpack `SETTING_RECORD_PLAN_TIME`'s wire value into (start, end); None if genuinely out-of-range."""
    start = _decode_plan_hour_minute((value >> 24) & 0xFF, (value >> 8) & 0xFF)
    end = _decode_plan_hour_minute((value >> 16) & 0xFF, value & 0xFF)
    if start is None or end is None:
        return None
    return start, end


_WEAK_PASSWORD_MIN_DIGITS = 6
_NUMERIC_PIN_MAX_DIGITS = 10

# Not real settings: cycle to random values on a timer (firmware artifact).
_NOISE_SETTING_IDS = {10, 22, 23, 33, 39, 42, 45, 51}

_DISCOVERY_PORT = 25143

_SET_SETTING_VERIFY_TIMEOUT_S = 10.0


def _log_hex(data: bytes) -> str:
    return data.rstrip(b"\x00").hex()


_DES_KEY_MESG = bytes.fromhex("8c270a3eb9ec4d0e")
_DES_KEY_PWD_CHUNK = bytes.fromhex("9cae6a5ae1fcb082")
_ENTRY_PWD_XOR_TABLE = (
    0x177BCE1F,
    0x4208ABFB,
    0xBF50695E,
    0x5C04BB9A,
    0x13ECF425,
    0x76C479AD,
    0x5B63C382,
    0xAC4217BE,
    0x8567656A,
    0x568CAAE0,
)
_FORMAT_RESULT_CODES = {80: "success", 81: "fail", 82: "no_sd", 103: "must_stop_record"}

# No wire field carries a model name, only a version string.
_DEFAULT_MODEL_NAME = "Sricam/ieGeek IP Camera"
_RECORDINGS_LOOKBACK = timedelta(days=30)


class APIError(Exception):
    """General API error."""


class APIConnectionError(APIError):
    """Unreachable host/port, or no response to an authenticated request."""


class APIAuthError(APIError):
    """Password rejected -- inferred from a reachable host ignoring authenticated requests."""


@dataclass(frozen=True)
class DiscoveredCamera:
    """A camera found via UDP broadcast discovery."""

    host: str
    port: int
    contact_id: str
    name: str


@dataclass(frozen=True)
class CameraIdentity:
    """Identity of a camera that accepted a connection."""

    contact_id: str
    name: str
    model: str
    firmware_version: str


@dataclass(frozen=True)
class Recording:
    """A single recorded clip on the camera's SD card."""

    recording_id: str
    started_at: datetime
    duration: timedelta
    motion_triggered: bool


@dataclass(frozen=True)
class StorageState:
    """SD card storage usage, in megabytes."""

    used_mb: int
    total_mb: int


@dataclass(frozen=True)
class FirmwareInfo:
    """Latest available firmware version for a camera."""

    latest_version: str
    release_summary: str | None
    release_url: str | None


# -- password hashing --------------------------------------------------------


def _is_weak_password_int(n: int) -> bool:
    s = str(n)
    if len(s) < _WEAK_PASSWORD_MIN_DIGITS:
        return True
    digits = [int(c) for c in s]
    step = digits[0] - digits[1]
    if all(digits[i] - digits[i + 1] == step for i in range(len(digits) - 1)):
        return True
    return all(d == digits[0] for d in digits)


def _entry_pwd_hash(password: str) -> int:
    digest = hashlib.md5(password.encode()).hexdigest()  # noqa: S324 -- protocol requirement, not for security
    words = [int(digest[i : i + 8], 16) for i in range(0, 32, 8)]
    x = (words[0] ^ words[1] ^ words[2] ^ words[3]) % 999999999
    for table_val in _ENTRY_PWD_XOR_TABLE:
        if not _is_weak_password_int(x):
            return x
        x = (x ^ table_val) % 999999999
    return x % 999999999


def entry_password(password: str) -> int:
    """Wire password int. Short numeric PINs pass through as-is; idempotent on its own hashed output."""
    if password.isdigit() and len(password) < _NUMERIC_PIN_MAX_DIGITS and password[0] != "0":
        return int(password)
    return _entry_pwd_hash(password)


def _rand3() -> int:
    r1, r2, r3 = random.getrandbits(31), random.getrandbits(31), random.getrandbits(31)
    return (r1 << 20 | r2 << 10 | r3) & 0xFFFFFFFF


# -- discovery ----------------------------------------------------------------


def _discover(
    *, broadcast_ip: str = "255.255.255.255", timeout: float, port: int = _DISCOVERY_PORT
) -> list[DiscoveredCamera]:
    uid = uuid.uuid4().hex[:8]
    request = bytearray(1024)
    struct.pack_into(">III", request, 0, 1, 0, 0x1C)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.bind(("0.0.0.0", 0))  # noqa: S104 -- must receive broadcast replies on any interface
    sock.settimeout(0.2)
    try:
        LOGGER.debug("[%s] Starting discovery broadcast to %s:%s", uid, broadcast_ip, port)
        WIRE_LOGGER.debug("[%s] UDP send to %s:%s: %s", uid, broadcast_ip, port, _log_hex(bytes(request)))
        sock.sendto(bytes(request), (broadcast_ip, port))
        found: dict[str, DiscoveredCamera] = {}
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                data, addr = sock.recvfrom(4096)
            except TimeoutError:
                continue
            WIRE_LOGGER.debug("[%s] UDP recv from %s: %s", uid, addr, _log_hex(data))
            if len(data) < 4 or struct.unpack_from(">I", data, 0)[0] != 2:  # noqa: PLR2004
                continue  # not our magic marker at all -- unrelated LAN broadcast traffic, not noteworthy
            if len(data) != 96:  # noqa: PLR2004
                # right marker, wrong length -- a genuine corrupted reply, not unrelated noise
                LOGGER.warning(
                    "[%s] Discovery reply from %s looks malformed (%d bytes, expected 96)", uid, addr, len(data)
                )
                continue
            contact_id = struct.unpack_from(">I", data, 16)[0]
            found[addr[0]] = DiscoveredCamera(
                host=addr[0], port=DEFAULT_PORT, contact_id=str(contact_id), name=f"IPCam-{contact_id}"
            )
        return list(found.values())
    finally:
        sock.close()


# -- low-level wire client -----------------------------------------------------


@dataclass
class _SettingsDump:
    values: dict[int, int]

    def clean_values(self) -> dict[int, int]:
        return {k: v for k, v in self.values.items() if k not in _NOISE_SETTING_IDS}


@dataclass
class _RecFileEntry:
    timestamp: datetime
    disc: int
    tag: str
    duration_s: int | None


def _resolve_ipv4(host: str) -> str:
    """Resolve `host` to a dotted-quad IPv4 address, needed since dst_id is derived from its last octet."""
    try:
        ipaddress.IPv4Address(host)
    except ValueError:
        return socket.gethostbyname(host)
    return host


def _short_ack_msgid(data: bytes) -> int | None:
    """Msgid echoed by the camera's short ack (`61 <subcmd> 6d 42 <msgid-lo> <msgid-hi>`), or None if not that shape."""
    if len(data) >= 6 and data[0] == 0x61:  # noqa: PLR2004
        return data[4] | (data[5] << 8)
    return None


def _header_counter(data: bytes) -> int | None:
    """Return the 4-byte counter at header bytes[4:8] -- our msgid on sends; on broadcasts, an unrelated sequence."""
    return struct.unpack_from("<I", data, 4)[0] if len(data) >= 8 else None  # noqa: PLR2004


def _packet_is_intact(data: bytes) -> bool:
    """Header bytes[8:12] declare the payload length; a mismatch means a truncated/corrupted UDP reply."""
    if len(data) == 0:
        return False
    if data[0] == 0x61:  # noqa: PLR2004
        return True  # the short ack format has no length field -- the camera zero-pads it past 12 bytes
    if len(data) < 12:  # noqa: PLR2004
        return False
    return struct.unpack_from("<I", data, 8)[0] == len(data) - 12


class _BroadcastSlot[T]:
    """Latest decoded value for one broadcast shape, with a monotonic seq and an event that wakes waiters."""

    def __init__(self) -> None:
        self.latest: T | None = None
        self.seq = 0
        self._updated = asyncio.Event()
        self._exc: Exception | None = None

    def publish(self, value: T) -> None:
        self.latest = value
        self.seq += 1
        event = self._updated
        self._updated = asyncio.Event()
        event.set()

    def fail(self, exc: Exception) -> None:
        self._exc = exc
        event = self._updated
        self._updated = asyncio.Event()
        event.set()

    async def wait_after(self, since_seq: int, timeout_s: float) -> tuple[int, T] | None:
        """Wait for a published value with `seq > since_seq`; returns (seq, value), or None on timeout."""
        deadline = asyncio.get_running_loop().time() + timeout_s
        while True:
            if self._exc is not None:
                raise self._exc
            if self.seq > since_seq:
                return self.seq, self.latest  # ty: ignore[invalid-return-type]
            event = self._updated
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return None
            try:
                await asyncio.wait_for(event.wait(), remaining)
            except TimeoutError:
                return None


@dataclass(frozen=True)
class _ShapeSpec:
    """A broadcast shape this session caches: how to recognize it, decode it, and where the latest value lives."""

    detect: Callable[[bytes], bool]
    decode: Callable[[bytes], Any]
    slot: _BroadcastSlot[Any]


_MSGID_MIN = 30000
_MSGID_MAX = 39999


class _WireSession(asyncio.DatagramProtocol):
    """Persistent UDP connection to one camera; dispatches acks by msgid and caches the latest per broadcast shape."""

    def __init__(self, host: str) -> None:
        self._host = host
        self._transport: asyncio.DatagramTransport | None = None
        self._by_msgid: dict[int, tuple[int, asyncio.Future[bytes]]] = {}
        self._next_msgid = random.randint(_MSGID_MIN, _MSGID_MAX)  # noqa: S311 -- not a security use
        self._rec_files_fut: asyncio.Future[bytes] | None = None
        self._format_fut: asyncio.Future[bytes] | None = None
        self._rec_files_lock = asyncio.Lock()
        self._format_lock = asyncio.Lock()
        self.settings: _BroadcastSlot[_SettingsDump] = _BroadcastSlot()
        self.record_quality: _BroadcastSlot[int] = _BroadcastSlot()
        self.sd_capacity: _BroadcastSlot[tuple[int, int, int]] = _BroadcastSlot()
        self.device_time: _BroadcastSlot[datetime] = _BroadcastSlot()
        self.device_info: _BroadcastSlot[dict[str, str | int]] = _BroadcastSlot()
        self.update_check: _BroadcastSlot[dict[str, str | int]] = _BroadcastSlot()
        self._broadcast_specs: tuple[_ShapeSpec, ...] = (
            _ShapeSpec(_is_settings_dump, _decode_settings_dump, self.settings),
            _ShapeSpec(_is_record_quality_reply, _decode_record_quality, self.record_quality),
            _ShapeSpec(_is_sd_capacity_reply, _decode_sd_capacity, self.sd_capacity),
            _ShapeSpec(_is_device_time_reply, _decode_device_time, self.device_time),
            _ShapeSpec(_is_device_info_reply, _decode_device_info, self.device_info),
            _ShapeSpec(_is_update_check_reply, _decode_update_check, self.update_check),
        )

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self._transport = transport  # ty: ignore[invalid-assignment]

    def connection_lost(self, exc: Exception | None) -> None:
        self._fail_all(exc or OSError("wire session closed"))

    def error_received(self, exc: Exception) -> None:
        self._fail_all(exc)

    def _fail_all(self, exc: Exception) -> None:
        for _subcmd, fut in self._by_msgid.values():
            if not fut.done():
                fut.set_exception(exc)
        self._by_msgid.clear()
        for one_shot in (self._rec_files_fut, self._format_fut):
            if one_shot is not None and not one_shot.done():
                one_shot.set_exception(exc)
        self._rec_files_fut = None
        self._format_fut = None
        for spec in self._broadcast_specs:
            spec.slot.fail(exc)

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:  # noqa: ARG002
        WIRE_LOGGER.debug("UDP recv from %s (counter=%s): %s", self._host, _header_counter(data), _log_hex(data))
        if not _packet_is_intact(data):
            LOGGER.warning("Dropping corrupted/truncated UDP reply from %s: %s", self._host, _log_hex(data))
            return
        msgid = _short_ack_msgid(data)
        if msgid is not None:
            self._dispatch_msgid_ack(data, msgid)
            return  # a short-ack-shaped packet is never a broadcast or one-shot reply, whether or not we're waiting
        if self._dispatch_one_shot(data):
            return
        self._dispatch_broadcast(data)

    def _dispatch_msgid_ack(self, data: bytes, msgid: int) -> None:
        entry = self._by_msgid.get(msgid)
        if entry is None:
            return
        subcmd, fut = entry
        if data[1] != subcmd:
            LOGGER.warning("Ignoring msgid=%s ack with mismatched subcmd (got %s, expected %s)", msgid, data[1], subcmd)
        elif not fut.done():
            self._by_msgid.pop(msgid, None)
            fut.set_result(data)

    def _dispatch_one_shot(self, data: bytes) -> bool:
        if _is_complete_rec_files_reply(data):
            if self._rec_files_fut is not None and not self._rec_files_fut.done():
                self._rec_files_fut.set_result(data)
            return True
        if _is_format_reply(data):
            if self._format_fut is not None and not self._format_fut.done():
                self._format_fut.set_result(data)
            return True
        return False

    def _dispatch_broadcast(self, data: bytes) -> None:
        for spec in self._broadcast_specs:
            try:
                if not spec.detect(data):
                    continue
                value = spec.decode(data)
            except (struct.error, IndexError) as err:
                LOGGER.warning("Failed to decode a broadcast reply from %s: %s", self._host, err)
                return
            spec.slot.publish(value)
            return

    def send(self, packet: bytes, uid: str) -> None:
        assert self._transport is not None  # noqa: S101
        WIRE_LOGGER.debug(
            "[%s] UDP send to %s (msgid=%s): %s", uid, self._host, _header_counter(packet), _log_hex(packet)
        )
        self._transport.sendto(packet)

    def close(self) -> None:
        if self._transport is not None:
            self._transport.close()
            self._transport = None

    def alloc_msgid(self) -> int:
        """Allocate the next msgid from a monotonic counter, skipping any value still in use (no collisions)."""
        for _ in range(_MSGID_MAX - _MSGID_MIN + 1):
            msgid = self._next_msgid
            self._next_msgid = _MSGID_MIN if msgid >= _MSGID_MAX else msgid + 1
            if msgid not in self._by_msgid:
                return msgid
        msg = "no free msgid available"
        raise APIError(msg)

    def begin_msgid(self, msgid: int, subcmd: int) -> asyncio.Future[bytes]:
        """Register the wait before sending, so the reply can never race the registration."""
        fut: asyncio.Future[bytes] = asyncio.get_running_loop().create_future()
        self._by_msgid[msgid] = (subcmd, fut)
        return fut

    async def wait_msgid(self, fut: asyncio.Future[bytes], msgid: int, timeout_s: float) -> bytes | None:
        try:
            return await asyncio.wait_for(fut, timeout_s)
        except TimeoutError:
            return None
        except OSError as err:
            raise APIConnectionError(str(err)) from err
        finally:
            self._by_msgid.pop(msgid, None)

    async def send_and_wait_rec_files(self, send: Callable[[], None], timeout_s: float) -> bytes | None:
        async with self._rec_files_lock:
            fut: asyncio.Future[bytes] = asyncio.get_running_loop().create_future()
            self._rec_files_fut = fut
            try:
                send()
                return await asyncio.wait_for(fut, timeout_s)
            except TimeoutError:
                return None
            except OSError as err:
                raise APIConnectionError(str(err)) from err
            finally:
                self._rec_files_fut = None

    async def send_and_wait_format(self, send: Callable[[], None], timeout_s: float) -> bytes | None:
        async with self._format_lock:
            fut: asyncio.Future[bytes] = asyncio.get_running_loop().create_future()
            self._format_fut = fut
            try:
                send()
                return await asyncio.wait_for(fut, timeout_s)
            except TimeoutError:
                return None
            except OSError as err:
                raise APIConnectionError(str(err)) from err
            finally:
                self._format_fut = None


class _Wire:
    """Request builder/decoder for one camera, sitting on top of a shared `_WireSession`."""

    def __init__(self, session: _WireSession, dst_id: int, password_int: int) -> None:
        self._session = session
        self._dst_id = dst_id
        self._password_int = password_int

    def _password_block(self) -> bytes:
        """DES-ECB via TripleDES(key*3) -- cryptography dropped plain DES; K1=K2=K3 is equivalent."""
        decryptor = Cipher(TripleDES(_DES_KEY_MESG * 3), modes.ECB()).decryptor()  # noqa: S305
        plaintext = struct.pack("<II", self._password_int, _rand3())
        return decryptor.update(plaintext) + decryptor.finalize()

    def _send(self, payload: bytes, msgid: int, subcmd: int, uid: str) -> None:
        header = bytearray(12)
        header[0] = 0x60
        header[1] = subcmd
        header[2] = self._dst_id
        header[3] = 100
        struct.pack_into("<I", header, 4, msgid)
        struct.pack_into("<I", header, 8, len(payload))
        self._session.send(bytes(header) + payload, uid)

    def _send_extended(self, cmd_payload: bytes, msgid: int, uid: str) -> None:
        self._send(self._password_block() + cmd_payload, msgid, subcmd=0x0B, uid=uid)

    async def _wait_broadcast[T](
        self, slot: _BroadcastSlot[T], since_seq: int, timeout_s: float, matches: Callable[[T], bool] | None = None
    ) -> T | None:
        """Wait for the next cached update after `since_seq` satisfying `matches` (any update if None)."""
        deadline = asyncio.get_running_loop().time() + timeout_s
        try:
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    return None
                result = await slot.wait_after(since_seq, remaining)
                if result is None:
                    return None
                since_seq, value = result
                if matches is None or matches(value):
                    return value
        except OSError as err:
            raise APIConnectionError(str(err)) from err

    async def get_settings(self, uid: str, timeout_s: float = 8.0) -> _SettingsDump | None:
        LOGGER.debug("[%s] get_settings()", uid)
        since_seq = self._session.settings.seq
        self._send(self._password_block() + bytes(4), self._session.alloc_msgid(), subcmd=0x03, uid=uid)
        return await self._wait_broadcast(self._session.settings, since_seq, timeout_s)

    async def get_settings_matching(
        self, uid: str, matches: Callable[[_SettingsDump], bool], timeout_s: float
    ) -> _SettingsDump | None:
        """
        Wait for a settings dump (explicit reply or unprompted broadcast) whose decoded values satisfy `matches`.

        Unlike `get_settings`, a dump that doesn't satisfy `matches` is simply not a match and dispatch keeps
        waiting -- so a stale re-broadcast of the pre-write dump can't win the race and be mistaken for confirmation.
        `since_seq` is captured before sending the write, so a dump cached from before this call can't be mistaken
        for confirmation either.
        """
        since_seq = self._session.settings.seq
        self._send(self._password_block() + bytes(4), self._session.alloc_msgid(), subcmd=0x03, uid=uid)
        return await self._wait_broadcast(self._session.settings, since_seq, timeout_s, matches)

    async def set_setting(self, setting_type: int, value: int, uid: str) -> None:
        """Fire-and-forget the write; `async_set_setting` verifies via the settings dump read-back."""
        LOGGER.debug("[%s] set_setting(type=%s, value=%s)", uid, setting_type, value)
        payload = self._password_block() + bytes.fromhex("01000100") + struct.pack("<II", setting_type, value)
        self._send(payload, self._session.alloc_msgid(), subcmd=0x0B, uid=uid)

    async def get_record_quality(self, uid: str, timeout_s: float = 5.0) -> int | None:
        LOGGER.debug("[%s] get_record_quality()", uid)
        since_seq = self._session.record_quality.seq
        self._send_extended(bytes([0xF0, 0, 0, 0, 0, 0]), self._session.alloc_msgid(), uid)
        return await self._wait_broadcast(self._session.record_quality, since_seq, timeout_s)

    async def get_record_quality_matching(self, uid: str, value: int, timeout_s: float) -> int | None:
        """Apply the same reasoning as `get_settings_matching`, for the record-quality reply."""
        since_seq = self._session.record_quality.seq
        self._send_extended(bytes([0xF0, 0, 0, 0, 0, 0]), self._session.alloc_msgid(), uid)
        return await self._wait_broadcast(self._session.record_quality, since_seq, timeout_s, lambda v: v == value)

    async def set_record_quality(self, value: int, uid: str) -> None:
        LOGGER.debug("[%s] set_record_quality(value=%s)", uid, value)
        self._send_extended(bytes([0xEF, 0, value & 0xFF, 0, 0, 0]), self._session.alloc_msgid(), uid)

    async def get_sd_card_capacity(self, uid: str, timeout_s: float = 5.0) -> tuple[int, int, int] | None:
        LOGGER.debug("[%s] get_sd_card_capacity()", uid)
        since_seq = self._session.sd_capacity.seq
        self._send_extended(bytes([0x50, 0, 0, 0]), self._session.alloc_msgid(), uid)
        return await self._wait_broadcast(self._session.sd_capacity, since_seq, timeout_s)

    async def format_sd_card(self, sd_id: int, uid: str, timeout_s: float = 3.0) -> str:
        LOGGER.debug("[%s] format_sd_card(sd_id=%s)", uid, sd_id)
        msgid = self._session.alloc_msgid()

        def send() -> None:
            self._send_extended(bytes([0x51, 0, 0, 0, sd_id & 0xFF]), msgid, uid)

        data = await self._session.send_and_wait_format(send, timeout_s)
        return _FORMAT_RESULT_CODES.get(data[13], f"unknown_{data[13]}") if data is not None else "no_response"

    async def get_device_time(self, uid: str, timeout_s: float = 5.0) -> datetime | None:
        LOGGER.debug("[%s] get_device_time()", uid)
        since_seq = self._session.device_time.seq
        self._send_extended(bytes([0x0A, 0, 0, 0, 0, 0, 0, 0, 0]), self._session.alloc_msgid(), uid)
        return await self._wait_broadcast(self._session.device_time, since_seq, timeout_s)

    async def set_device_time(self, dt: datetime, uid: str, timeout_s: float = 3.0) -> bool:
        LOGGER.debug("[%s] set_device_time(dt=%s)", uid, dt)
        msgid = self._session.alloc_msgid()
        fut = self._session.begin_msgid(msgid, subcmd=0x0B)
        body = bytes([0x0B, 0, 0, 0]) + struct.pack("<H", dt.year) + bytes([dt.month, dt.day, dt.hour, dt.minute])
        self._send_extended(body, msgid, uid)
        return await self._session.wait_msgid(fut, msgid, timeout_s) is not None

    async def get_device_info(self, uid: str, timeout_s: float = 5.0) -> dict[str, str | int] | None:
        LOGGER.debug("[%s] get_device_info()", uid)
        since_seq = self._session.device_info.seq
        payload = self._password_block() + bytes([0x27]) + bytes(35)
        self._send(payload, self._session.alloc_msgid(), subcmd=0x03, uid=uid)
        return await self._wait_broadcast(self._session.device_info, since_seq, timeout_s)

    _DEVICE_UPDATE_CHECK_TAIL = bytes.fromhex("1d6ce42301000000e0ae59cb01000000")

    async def get_device_update_check(self, uid: str, timeout_s: float = 15.0) -> dict[str, str | int] | None:
        LOGGER.debug("[%s] get_device_update_check()", uid)
        since_seq = self._session.update_check.seq
        payload = self._password_block() + self._DEVICE_UPDATE_CHECK_TAIL
        self._send(payload, self._session.alloc_msgid(), subcmd=0x03, uid=uid)
        return await self._wait_broadcast(self._session.update_check, since_seq, timeout_s)

    _GETRECFILES_FIELD = bytes.fromhex("03010000")

    @staticmethod
    def _pack_datetime(dt: datetime) -> bytes:
        return struct.pack("<HBBBB", dt.year, dt.month, dt.day, dt.hour, dt.minute)

    async def get_rec_files(
        self, start: datetime, end: datetime, uid: str, timeout_s: float = 15.0
    ) -> list[_RecFileEntry]:
        LOGGER.debug("[%s] get_rec_files(start=%s, end=%s)", uid, start, end)
        payload = (
            self._password_block() + self._GETRECFILES_FIELD + self._pack_datetime(start) + self._pack_datetime(end)
        )
        msgid = self._session.alloc_msgid()

        def send() -> None:
            self._send(payload, msgid, subcmd=0x0B, uid=uid)

        data = await self._session.send_and_wait_rec_files(send, timeout_s)
        if data is None:
            msg = "get_rec_files: no complete reply from camera"
            raise OSError(msg)
        return _decode_rec_files(data)


def _is_settings_dump(data: bytes) -> bool:
    return data[0] == 0x60 and len(data) > 200 and data[12] == 0x02 and data[13] == 0x01  # noqa: PLR2004


def _decode_settings_dump(data: bytes) -> _SettingsDump:
    payload = data[12:]
    count = struct.unpack_from("<H", payload, 2)[0]
    values = dict(struct.unpack_from("<II", payload, 4 + i * 8) for i in range(count))
    return _SettingsDump(values=values)


def _is_record_quality_reply(data: bytes) -> bool:
    return len(data) >= 15 and data[0] == 0x60 and data[12] == 0xF1  # noqa: PLR2004


def _decode_record_quality(data: bytes) -> int:
    return data[14]


def _is_sd_capacity_reply(data: bytes) -> bool:
    return len(data) >= 33 and data[0] == 0x60 and data[12] == 0x50  # noqa: PLR2004


def _decode_sd_capacity(data: bytes) -> tuple[int, int, int]:
    payload = data[12:]
    total = struct.unpack_from("<I", payload, 8)[0] * 16
    free = struct.unpack_from("<I", payload, 16)[0] * 16
    return total, free, payload[4]


def _is_format_reply(data: bytes) -> bool:
    return len(data) >= 15 and data[0] == 0x60 and data[12] == 0x51  # noqa: PLR2004


def _is_device_time_reply(data: bytes) -> bool:
    return len(data) >= 22 and data[0] == 0x60 and data[12] == 0x0C  # noqa: PLR2004


def _decode_device_time(data: bytes) -> datetime:
    p = data[12:]
    year = struct.unpack_from("<H", p, 4)[0]
    return datetime(year, p[6], p[7], p[8], p[9])  # noqa: DTZ001 -- naive camera-local time


def _is_device_info_reply(data: bytes) -> bool:
    return len(data) == 48 and data[0] == 0x60 and data[12] == 0x28  # noqa: PLR2004


def _decode_device_info(data: bytes) -> dict[str, str | int]:
    p = data[12:]
    return {"device_version": f"{p[7]}.{p[6]}.{p[5]}.{p[4]}"}


def _is_update_check_reply(data: bytes) -> bool:
    return len(data) == 24 and data[0] == 0x60 and data[12] == 0x1E  # noqa: PLR2004


def _decode_update_check(data: bytes) -> dict[str, str | int]:
    p = data[12:]
    return {
        "result": p[1],
        "cur_version": f"{p[7]}.{p[6]}.{p[5]}.{p[4]}",
        "upg_version": f"{p[11]}.{p[10]}.{p[9]}.{p[8]}",
    }


def _is_complete_rec_files_reply(data: bytes) -> bool:
    """Reject a reply too short for its own claimed count; a genuine zero-recordings reply stays well-formed."""
    if data[:1] != b"\x60" or (len(data) > 200 and data[12:14] == b"\x02\x01"):  # noqa: PLR2004
        return False
    payload = data[12:]
    return len(payload) >= 4 and payload[0] == 4 and len(payload) >= 4 + payload[3] * 8  # noqa: PLR2004


def _decode_rec_files(data: bytes) -> list[_RecFileEntry]:
    payload = data[12:]
    count = payload[3]
    durations = None
    if payload[1] & 1:
        dur_off = 4 + count * 8
        if len(payload) >= dur_off + count * 2:
            durations = struct.unpack_from(f"<{count}H", payload, dur_off)
    entries = []
    for i in range(count):
        chunk = payload[4 + i * 8 : 12 + i * 8]
        year = struct.unpack_from("<H", chunk, 0)[0]
        entries.append(
            _RecFileEntry(
                timestamp=datetime(year, chunk[2] & 0xF, chunk[3], chunk[4], chunk[5], chunk[6]),  # noqa: DTZ001
                disc=chunk[2] >> 4,
                tag=chr(chunk[7]),
                duration_s=durations[i] if durations else None,
            )
        )
    return entries


# -- async client used by the integration -------------------------------------


async def _run_blocking[T](hass: HomeAssistant, fn: Callable[[], T]) -> T:
    """Run a blocking call in the executor, mapping raw exceptions to our hierarchy."""
    try:
        return await hass.async_add_executor_job(fn)
    except APIError:
        raise
    except OSError as err:
        raise APIConnectionError(str(err)) from err
    except Exception as err:
        raise APIError(str(err)) from err


def _new_uid() -> str:
    return uuid.uuid4().hex[:8]


class GwellIPCamClient:
    """Async-facing client for a single Gwell IP camera."""

    def __init__(self, hass: HomeAssistant, host: str, port: int, password_hash: str, entry_id: str) -> None:
        """Initialize the client for a specific camera."""
        self._hass = hass
        self._host = host
        self._port = int(port)
        self._password_int = entry_password(password_hash)
        self._entry_id = entry_id
        self._rtsp_session = RTSPSession(host)
        self._rtsp_proxy = RTSPProxyServer(self._rtsp_session)
        self._quick_record_store: Store[dict[str, int | None]] | None = None
        self._quick_record_saved_type: int | None = None
        self._quick_record_lock = asyncio.Lock()
        self._wire: _WireSession | None = None
        self._dst_id = 0
        self._wire_connect_lock = asyncio.Lock()

    def _get_quick_record_store(self) -> Store[dict[str, int | None]]:
        if self._quick_record_store is None:
            self._quick_record_store = Store(self._hass, version=1, key=f"gwell_ipcam.{self._entry_id}.quick_record")
        return self._quick_record_store

    async def _get_wire(self) -> _Wire:
        if self._wire is None:
            async with self._wire_connect_lock:
                if self._wire is None:
                    session = _WireSession(self._host)
                    try:
                        host_ip = await self._hass.async_add_executor_job(_resolve_ipv4, self._host)
                        loop = asyncio.get_running_loop()
                        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                        sock.bind(("0.0.0.0", self._port))  # noqa: S104 -- any local interface
                        sock.connect((host_ip, self._port))
                        await loop.create_datagram_endpoint(lambda: session, sock=sock)
                    except OSError as err:
                        raise APIConnectionError(str(err)) from err
                    self._wire = session
                    self._dst_id = int(host_ip.split(".")[-1])
        return _Wire(self._wire, self._dst_id, self._password_int)

    async def async_close_wire(self) -> None:
        """Close the persistent UDP session; safe to call even if it was never opened."""
        if self._wire is not None:
            self._wire.close()
            self._wire = None

    @property
    def rtsp_session(self) -> RTSPSession:
        """The shared upstream RTSP session (for the assist_satellite mic feed)."""
        return self._rtsp_session

    async def async_start_streaming(self) -> None:
        """Open the shared RTSP session and start the local header-fixing proxy; kept open for the entry's lifetime."""
        await self._rtsp_session.start()
        await self._rtsp_proxy.start()

    async def async_stop_streaming(self) -> None:
        """Stop the local proxy and close the shared RTSP session."""
        started = time.monotonic()
        LOGGER.debug("Stopping local RTSP proxy")
        await self._rtsp_proxy.stop()
        LOGGER.debug("Stopped local RTSP proxy in %.3fs, stopping upstream RTSP session", time.monotonic() - started)
        await self._rtsp_session.stop()
        await self.async_close_wire()
        LOGGER.debug("Stopped streaming in %.3fs total", time.monotonic() - started)

    async def async_ptz(self, direction: str, *, steps: int = 1, step_delay_ms: int = 200) -> None:
        """Send `steps` PTZ nudges in `direction` (already mapped for image-reverse by the caller)."""
        await self._rtsp_session.ptz(direction, steps=steps, step_delay_ms=step_delay_ms)

    async def async_talk(self, pcm16_8khz_mono: bytes) -> None:
        """Push 8kHz mono PCM16 audio to the camera's speaker over a fresh talk session."""
        async with TalkSession(self._host) as talk:
            await talk.send_pcm16(pcm16_8khz_mono)

    @staticmethod
    async def async_discover(hass: HomeAssistant, timeout_s: float) -> list[DiscoveredCamera]:
        """Broadcast a UDP discovery request and collect camera responses."""
        return await _run_blocking(hass, lambda: _discover(timeout=timeout_s))

    @staticmethod
    async def async_discover_one(hass: HomeAssistant, host: str, timeout_s: float = 2.0) -> DiscoveredCamera | None:
        """Query a single known host for its contact_id (e.g. to fill in what DHCP discovery can't provide)."""
        found = await _run_blocking(hass, lambda: _discover(broadcast_ip=host, timeout=timeout_s))
        return found[0] if found else None

    @staticmethod
    def hash_password(password: str) -> str:
        """Hash a plaintext password using the camera's hashing scheme."""
        return str(entry_password(password))

    @staticmethod
    async def async_check_connection(hass: HomeAssistant, host: str, port: int, password_hash: str) -> CameraIdentity:
        """Verify that a camera is reachable and accepts the given password hash."""
        client = GwellIPCamClient(hass=hass, host=host, port=port, password_hash=password_hash, entry_id="")
        try:
            return await client.async_get_identity()
        finally:
            await client.async_close_wire()

    async def async_get_identity(self) -> CameraIdentity:
        """Fetch the camera's identity. Name is synthesized -- no wire field carries one."""
        found = await self.async_discover_one(self._hass, self._host)
        if found is None:
            msg = f"no discovery reply from {self._host}"
            raise APIConnectionError(msg)
        contact_id = found.contact_id
        wire = await self._get_wire()
        info = await wire.get_device_info(_new_uid())
        if info is None:
            msg = f"camera at {self._host} did not respond to an authenticated request"
            raise APIAuthError(msg)
        return CameraIdentity(
            contact_id=contact_id,
            name=f"IPCam-{contact_id}",
            model=_DEFAULT_MODEL_NAME,
            firmware_version=str(info["device_version"]),
        )

    async def async_get_camera_time(self, *, uid: str | None = None) -> datetime:
        """Fetch the camera's clock, localized to HA's configured timezone (camera keeps no tz of its own)."""
        wire = await self._get_wire()
        naive = await wire.get_device_time(uid or _new_uid())
        if naive is None:
            msg = "no response from camera"
            raise APIConnectionError(msg)
        return naive.replace(tzinfo=dt_util.DEFAULT_TIME_ZONE)

    async def async_sync_time(self, *, uid: str | None = None) -> datetime:
        """
        Push HA's current local time to the camera's clock, verify it actually changed, and return it.

        The write's ack can't be trusted any more than `set_setting`'s could (see `async_set_setting`),
        and the write is minute-granularity besides, so an exact-match read-back isn't meaningful here --
        instead confirm the clock is now measurably closer to the real time than before writing.
        """
        before = await self.async_get_camera_time(uid=uid)
        before_drift = abs((dt_util.now() - before).total_seconds())
        now = dt_util.now().replace(tzinfo=None)
        wire = await self._get_wire()
        await wire.set_device_time(now, uid or _new_uid())
        after = await self.async_get_camera_time(uid=uid)
        after_drift = abs((dt_util.now() - after).total_seconds())
        if after_drift >= before_drift:
            msg = "camera clock did not change after syncing"
            raise APIError(msg)
        return after

    async def async_get_storage_state(self, *, uid: str | None = None) -> StorageState:
        """Fetch SD card storage usage."""
        wire = await self._get_wire()
        capacity = await wire.get_sd_card_capacity(uid or _new_uid())
        if capacity is None:
            msg = "no response from camera"
            raise APIConnectionError(msg)
        total, free, _sd_id = capacity
        if free > total or total < 0 or free < 0:
            msg = f"implausible SD card capacity (total={total}, free={free})"  # garbled reply, not a real reading
            raise APIError(msg)
        return StorageState(used_mb=total - free, total_mb=total)

    async def async_get_settings(self, *, uid: str | None = None) -> dict[int, int]:
        """Fetch the full settingType -> value dump (noise IDs filtered out)."""
        wire = await self._get_wire()
        dump = await wire.get_settings(uid or _new_uid())
        if dump is None:
            msg = "no response from camera"
            raise APIConnectionError(msg)
        return dump.clean_values()

    async def async_set_setting(self, setting_type: int, value: int, *, uid: str | None = None) -> dict[int, int]:
        """Write a settingType/value pair, wait for a dump confirming it, and return that freshly-confirmed dump."""
        wire = await self._get_wire()
        resolved_uid = uid or _new_uid()
        await wire.set_setting(setting_type, value, resolved_uid)
        dump = await wire.get_settings_matching(
            resolved_uid, lambda d: d.clean_values().get(setting_type) == value, _SET_SETTING_VERIFY_TIMEOUT_S
        )
        if dump is None:
            msg = f"setting {setting_type} did not change to {value} after writing it"
            raise APIError(msg)
        return dump.clean_values()

    async def async_get_record_plan(self, *, uid: str | None = None) -> tuple[dtime, dtime] | None:
        """Fetch the Timing record schedule (start, end); `SETTING_RECORD_PLAN_TIME` is part of the general dump."""
        settings = await self.async_get_settings(uid=uid)
        value = settings.get(SETTING_RECORD_PLAN_TIME)
        return decode_record_plan_time(value) if value is not None else None

    async def async_set_record_plan(self, start: dtime, end: dtime, *, uid: str | None = None) -> dict[int, int]:
        """Write the Timing record schedule (start, end)."""
        return await self.async_set_setting(SETTING_RECORD_PLAN_TIME, encode_record_plan_time(start, end), uid=uid)

    async def async_set_recording_state(self, *, enabled: bool, uid: str | None = None) -> dict[int, int]:
        """Start or stop recording."""
        return await self.async_set_setting(SETTING_REMOTE_RECORD, 1 if enabled else 0, uid=uid)

    async def async_load_quick_record_state(self) -> None:
        """Load the persisted quick-record state once at startup; call before reading `quick_record_active`."""
        data = await self._get_quick_record_store().async_load()
        self._quick_record_saved_type = data.get("saved_record_type") if data else None

    @property
    def quick_record_active(self) -> bool:
        """Whether a quick-record session is currently in progress."""
        return self._quick_record_saved_type is not None

    async def async_toggle_quick_record(
        self, *, current_settings: dict[int, int] | None = None, uid: str | None = None
    ) -> tuple[bool, dict[int, int]]:
        """
        First press: switch to Manual and start recording, remembering the prior mode to restore later.

        Serialized by `_quick_record_lock`: each wire round-trip in here used to take seconds, easy to
        double-press before the first one finished, which would race both calls against the same state.
        Returns the freshly-confirmed settings dump too, since a coordinator refresh right after this can
        race an already-in-flight one and pick up stale data instead of what was just verified here.

        `saved_type` is committed right after the first write, not after both: if the second write then
        fails, a retry still restores the true original mode instead of losing it to a re-read that would
        now see the already-applied Manual switch as the "original" mode.

        `current_settings` should be the coordinator's already-verified-fresh cache when available -- an
        independent `async_get_settings()` read here can lose the race to a stale re-broadcast still in
        flight from a just-prior write, capturing the wrong "current" mode to restore later.
        """
        async with self._quick_record_lock:
            if self._quick_record_saved_type is None:
                settings = current_settings if current_settings is not None else await self.async_get_settings(uid=uid)
                saved_type = settings.get(SETTING_RECORD_TYPE, RECORD_TYPE_MANUAL)
                await self.async_set_setting(SETTING_RECORD_TYPE, RECORD_TYPE_MANUAL, uid=uid)
                self._quick_record_saved_type = saved_type
                await self._get_quick_record_store().async_save({"saved_record_type": saved_type})
                fresh = await self.async_set_recording_state(enabled=True, uid=uid)
                return True, fresh

            saved_type = self._quick_record_saved_type
            await self.async_set_recording_state(enabled=False, uid=uid)
            fresh = await self.async_set_setting(SETTING_RECORD_TYPE, saved_type, uid=uid)
            self._quick_record_saved_type = None
            await self._get_quick_record_store().async_save({"saved_record_type": None})
            return False, fresh

    async def async_get_record_quality(self, *, uid: str | None = None) -> int | None:
        """Fetch Record Quality (0-4)."""
        wire = await self._get_wire()
        return await wire.get_record_quality(uid or _new_uid())

    async def async_set_record_quality(self, value: int, *, uid: str | None = None) -> int:
        """Set Record Quality (0-4), wait for a reply confirming it, and return that freshly-confirmed value."""
        wire = await self._get_wire()
        resolved_uid = uid or _new_uid()
        await wire.set_record_quality(value, resolved_uid)
        result = await wire.get_record_quality_matching(resolved_uid, value, _SET_SETTING_VERIFY_TIMEOUT_S)
        if result is None:
            msg = f"record quality did not change to {value} after writing it"
            raise APIError(msg)
        return result

    async def async_format_sd_card(self, *, uid: str | None = None) -> None:
        """Format the camera's SD card."""
        wire = await self._get_wire()
        resolved_uid = uid or _new_uid()
        capacity = await wire.get_sd_card_capacity(resolved_uid)
        if capacity is None:
            msg = "no response from camera"
            raise APIConnectionError(msg)
        _total, _free, sd_id = capacity
        result = await wire.format_sd_card(sd_id, resolved_uid)
        if result != "success":
            msg = f"SD card format failed: {result}"
            raise APIError(msg)

    async def async_get_recordings(self, *, uid: str | None = None) -> list[Recording]:
        """List recordings currently stored on the camera's SD card."""
        end = dt_util.now().replace(tzinfo=None)
        start = end - _RECORDINGS_LOOKBACK
        wire = await self._get_wire()
        entries = await wire.get_rec_files(start, end, uid or _new_uid())
        return [_to_recording(entry) for entry in entries]

    async def async_stream_recording(self, recording_id: str) -> AsyncIterator[bytes]:  # noqa: ARG002
        """Stub: no wire format for fetching a recording's video bytes exists yet."""
        empty: tuple[bytes, ...] = ()
        for chunk in empty:
            yield chunk

    async def async_get_live_stream_url(self) -> str | None:
        """Return the local proxy's RTSP URL, or None if streaming hasn't been started yet."""
        return f"rtsp://127.0.0.1:{self._rtsp_proxy.port}{RTSP_PATH}"

    async def async_get_firmware_info(self) -> FirmwareInfo:
        """Check whether a firmware update is available for this camera."""
        wire = await self._get_wire()
        info = await wire.get_device_update_check(_new_uid())
        if info is None:
            msg = "no response from camera"
            raise APIConnectionError(msg)
        has_update = info["result"] in (1, 72)
        return FirmwareInfo(
            latest_version=str(info["upg_version"] if has_update else info["cur_version"]),
            release_summary=None,
            release_url=None,
        )

    async def async_install_firmware_update(self) -> None:
        """Stub: triggering an install has never been reverse-engineered, only checking for one."""
        msg = "firmware installation is not supported by this integration yet"
        raise APIError(msg)


def _to_recording(entry: _RecFileEntry) -> Recording:
    return Recording(
        recording_id=f"{entry.disc}-{entry.timestamp:%Y%m%d%H%M%S}",
        started_at=entry.timestamp.replace(tzinfo=dt_util.DEFAULT_TIME_ZONE),
        duration=timedelta(seconds=entry.duration_s or 0),
        motion_triggered=entry.tag == "A",
    )
