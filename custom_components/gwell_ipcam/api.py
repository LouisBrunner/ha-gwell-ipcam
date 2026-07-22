"""API client for Gwell (Sricam/ieGeek) IP cameras. See docs/PROTOCOL.md for the wire format."""

from __future__ import annotations

import asyncio
import contextlib
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
    from typing import Self

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
# After the first matching reply, wait only this long for a better one instead of the full timeout.
_RESPONSE_SETTLE_S = 0.3


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
            if len(data) == 96 and struct.unpack_from(">I", data, 0)[0] == 2:  # noqa: PLR2004
                contact_id = struct.unpack_from(">I", data, 16)[0]
                found[addr[0]] = DiscoveredCamera(
                    host=addr[0], port=DEFAULT_PORT, contact_id=str(contact_id), name=f"IPCam-{contact_id}"
                )
        return list(found.values())
    finally:
        sock.close()


# -- low-level sync protocol client -------------------------------------------


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


class _SricamProtocol:
    """Synchronous wire client for one request/response exchange. Blocking -- always run via an executor."""

    def __init__(self, host: str, port: int, password_hash: str, *, uid: str | None = None) -> None:
        self._host = host
        self._port = int(port)
        self._password_int = entry_password(password_hash)
        self._our_src_id = 100
        self._dst_id = int(_resolve_ipv4(host).split(".")[-1])
        self._sock: socket.socket | None = None
        # Logged with every UDP send/recv so a grep on the uid correlates one exchange across calls.
        self._uid = uid or uuid.uuid4().hex[:8]

    def __enter__(self) -> Self:
        LOGGER.debug("[%s] Starting session to %s:%s", self._uid, self._host, self._port)
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        # the camera only replies when the client's source port matches its own port
        self._sock.bind(("0.0.0.0", self._port))  # noqa: S104 -- any local interface
        self._sock.settimeout(0.2)
        return self

    def __exit__(self, *_exc: object) -> None:
        if self._sock is not None:
            self._sock.close()
            self._sock = None

    def _sock_or_raise(self) -> socket.socket:
        if self._sock is None:
            msg = "protocol client used outside a `with` block"
            raise RuntimeError(msg)
        return self._sock

    def _password_block(self, key8: bytes) -> bytes:
        """DES-ECB via TripleDES(key*3) -- cryptography dropped plain DES; K1=K2=K3 is equivalent."""
        decryptor = Cipher(TripleDES(key8 * 3), modes.ECB()).decryptor()  # noqa: S305 -- protocol mandates DES-ECB
        plaintext = struct.pack("<II", self._password_int, _rand3())
        return decryptor.update(plaintext) + decryptor.finalize()

    def _send(self, payload: bytes, msgid: int, subcmd: int) -> None:
        header = bytearray(12)
        header[0] = 0x60
        header[1] = subcmd
        header[2] = self._dst_id
        header[3] = self._our_src_id & 0xFF
        struct.pack_into("<I", header, 4, msgid)
        struct.pack_into("<I", header, 8, len(payload))
        packet = bytes(header) + payload
        WIRE_LOGGER.debug("[%s] UDP send to %s:%s: %s", self._uid, self._host, self._port, _log_hex(packet))
        self._sock_or_raise().sendto(packet, (self._host, self._port))

    def _send_extended(self, cmd_payload: bytes, msgid: int) -> None:
        self._send(self._password_block(_DES_KEY_MESG) + cmd_payload, msgid, subcmd=0x0B)

    def _recv(self) -> bytes | None:
        """Receive one datagram (or None on the socket's 0.2s timeout), logging it either way."""
        try:
            # Sized well above get_rec_files' largest realistic payload to avoid silent truncation.
            data, addr = self._sock_or_raise().recvfrom(16384)
        except TimeoutError:
            return None
        WIRE_LOGGER.debug("[%s] UDP recv from %s: %s", self._uid, addr, _log_hex(data))
        return data

    def _drain(self, duration: float) -> list[bytes]:
        """Collect every packet for the full `duration` (no early exit) -- used to catch an ack among noise."""
        packets = []
        deadline = time.monotonic() + duration
        while time.monotonic() < deadline:
            data = self._recv()
            if data is not None:
                packets.append(data)
        return packets

    def _poll_collecting[T](self, timeout: float, decode: Callable[[bytes], T | None]) -> list[T]:
        """Collect every `decode` match, stopping `_RESPONSE_SETTLE_S` after the first one (not the full timeout)."""
        deadline = time.monotonic() + timeout
        settling = False
        matches: list[T] = []
        while time.monotonic() < deadline:
            data = self._recv()
            if data is None:
                continue
            value = decode(data)
            if value is None:
                continue
            matches.append(value)
            # Only start the settle countdown on the first match, or the camera's own periodic
            # broadcasts would keep renewing it forever.
            if not settling:
                settling = True
                deadline = min(deadline, time.monotonic() + _RESPONSE_SETTLE_S)
        return matches

    def _poll_settling[T](self, timeout: float, decode: Callable[[bytes], T | None]) -> T | None:
        """Like `_poll_collecting`, but keeps only the freshest match."""
        matches = self._poll_collecting(timeout, decode)
        return matches[-1] if matches else None

    def _poll_first[T](self, timeout: float, decode: Callable[[bytes], T | None]) -> T | None:
        """Return as soon as `decode` matches a packet, without waiting out the rest of `timeout`."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            data = self._recv()
            if data is None:
                continue
            value = decode(data)
            if value is not None:
                return value
        return None

    def _flush_stale(self) -> None:
        while self._recv() is not None:
            pass

    @staticmethod
    def _msgid() -> int:
        return random.randint(30000, 39999)  # noqa: S311 -- not a security use

    def get_settings(self, timeout: float = 8.0) -> _SettingsDump | None:
        LOGGER.debug("[%s] get_settings()", self._uid)
        self._flush_stale()
        msgid = self._msgid()
        self._send(self._password_block(_DES_KEY_MESG) + bytes(4), msgid, subcmd=0x03)

        def decode(data: bytes) -> _SettingsDump | None:
            if not (data[0] == 0x60 and len(data) > 200 and data[12] == 0x02 and data[13] == 0x01):  # noqa: PLR2004
                return None
            payload = data[12:]
            count = struct.unpack_from("<H", payload, 2)[0]
            values = dict(struct.unpack_from("<II", payload, 4 + i * 8) for i in range(count))
            return _SettingsDump(values=values)

        return self._poll_first(timeout, decode)

    def set_setting(self, setting_type: int, value: int) -> bool:
        LOGGER.debug("[%s] set_setting(type=%s, value=%s)", self._uid, setting_type, value)
        msgid = self._msgid()
        payload = (
            self._password_block(_DES_KEY_MESG) + bytes.fromhex("01000100") + struct.pack("<II", setting_type, value)
        )
        self._send(payload, msgid, subcmd=0x0B)
        expect_tail = struct.pack("<II", setting_type, value)
        return any(
            p[:2] == bytes.fromhex("6002") and len(p) >= 24 and p[12] == 0x02 and p[16:24] == expect_tail  # noqa: PLR2004
            for p in self._drain(3.0)
        )

    def get_record_quality(self, timeout: float = 5.0) -> int | None:
        LOGGER.debug("[%s] get_record_quality()", self._uid)
        self._flush_stale()
        self._send_extended(bytes([0xF0, 0x00, 0x00, 0x00, 0x00, 0x00]), self._msgid())

        def decode(data: bytes) -> int | None:
            if len(data) >= 15 and data[0] == 0x60 and data[12] == 0xF1:  # noqa: PLR2004
                return data[14]
            return None

        return self._poll_settling(timeout, decode)

    def set_record_quality(self, value: int) -> None:
        LOGGER.debug("[%s] set_record_quality(value=%s)", self._uid, value)
        self._send_extended(bytes([0xEF, 0x00, value & 0xFF, 0x00, 0x00, 0x00]), self._msgid())
        self._drain(2.0)

    def get_sd_card_capacity(self, timeout: float = 5.0) -> tuple[int, int, int] | None:
        LOGGER.debug("[%s] get_sd_card_capacity()", self._uid)
        self._flush_stale()
        self._send_extended(bytes([0x50, 0x00, 0x00, 0x00]), self._msgid())

        def decode(data: bytes) -> tuple[int, int, int] | None:
            if not (len(data) >= 33 and data[0] == 0x60 and data[12] == 0x50):  # noqa: PLR2004
                return None
            payload = data[12:]
            total = struct.unpack_from("<I", payload, 8)[0] * 16
            free = struct.unpack_from("<I", payload, 16)[0] * 16
            return total, free, payload[4]

        return self._poll_first(timeout, decode)

    def format_sd_card(self, sd_id: int, timeout: float = 3.0) -> str:
        LOGGER.debug("[%s] format_sd_card(sd_id=%s)", self._uid, sd_id)
        self._send_extended(bytes([0x51, 0x00, 0x00, 0x00, sd_id & 0xFF]), self._msgid())

        def decode(data: bytes) -> str | None:
            if len(data) >= 15 and data[0] == 0x60 and data[12] == 0x51:  # noqa: PLR2004
                return _FORMAT_RESULT_CODES.get(data[13], f"unknown_{data[13]}")
            return None

        return self._poll_first(timeout, decode) or "no_response"

    def get_device_time(self, timeout: float = 5.0) -> datetime | None:
        LOGGER.debug("[%s] get_device_time()", self._uid)
        self._flush_stale()
        self._send_extended(bytes([0x0A, 0, 0, 0, 0, 0, 0, 0, 0]), self._msgid())

        def decode(data: bytes) -> datetime | None:
            if not (len(data) >= 22 and data[0] == 0x60 and data[12] == 0x0C):  # noqa: PLR2004
                return None
            p = data[12:]
            year = struct.unpack_from("<H", p, 4)[0]
            return datetime(year, p[6], p[7], p[8], p[9])  # noqa: DTZ001 -- naive camera-local time

        return self._poll_settling(timeout, decode)

    def set_device_time(self, dt: datetime, timeout: float = 3.0) -> None:
        LOGGER.debug("[%s] set_device_time(dt=%s)", self._uid, dt)
        body = bytes([0x0B, 0, 0, 0]) + struct.pack("<H", dt.year) + bytes([dt.month, dt.day, dt.hour, dt.minute])
        self._send_extended(body, self._msgid())
        self._drain(timeout)

    def get_device_info(self, timeout: float = 5.0) -> dict[str, str | int] | None:
        LOGGER.debug("[%s] get_device_info()", self._uid)
        self._flush_stale()
        payload = self._password_block(_DES_KEY_MESG) + bytes([0x27]) + bytes(35)
        self._send(payload, self._msgid(), subcmd=0x03)

        def decode(data: bytes) -> dict[str, str | int] | None:
            if not (len(data) == 48 and data[0] == 0x60 and data[12] == 0x28):  # noqa: PLR2004
                return None
            p = data[12:]
            return {"device_version": f"{p[7]}.{p[6]}.{p[5]}.{p[4]}"}

        return self._poll_first(timeout, decode)

    _DEVICE_UPDATE_CHECK_TAIL = bytes.fromhex("1d6ce42301000000e0ae59cb01000000")

    def get_device_update_check(self, timeout: float = 15.0) -> dict[str, str | int] | None:
        LOGGER.debug("[%s] get_device_update_check()", self._uid)
        self._flush_stale()
        payload = self._password_block(_DES_KEY_MESG) + self._DEVICE_UPDATE_CHECK_TAIL
        self._send(payload, self._msgid(), subcmd=0x03)

        def decode(data: bytes) -> dict[str, str | int] | None:
            if not (len(data) == 24 and data[0] == 0x60 and data[12] == 0x1E):  # noqa: PLR2004
                return None
            p = data[12:]
            return {
                "result": p[1],
                "cur_version": f"{p[7]}.{p[6]}.{p[5]}.{p[4]}",
                "upg_version": f"{p[11]}.{p[10]}.{p[9]}.{p[8]}",
            }

        return self._poll_first(timeout, decode)

    _GETRECFILES_FIELD = bytes.fromhex("03010000")

    @staticmethod
    def _pack_datetime(dt: datetime) -> bytes:
        return struct.pack("<HBBBB", dt.year, dt.month, dt.day, dt.hour, dt.minute)

    def get_rec_files(self, start: datetime, end: datetime, timeout: float = 15.0) -> list[_RecFileEntry]:
        LOGGER.debug("[%s] get_rec_files(start=%s, end=%s)", self._uid, start, end)
        self._flush_stale()
        payload = (
            self._password_block(_DES_KEY_MESG)
            + self._GETRECFILES_FIELD
            + self._pack_datetime(start)
            + self._pack_datetime(end)
        )
        self._send(payload, self._msgid(), subcmd=0x0B)

        def decode(data: bytes) -> bytes | None:
            if data[:1] == b"\x60" and not (len(data) > 200 and data[12:14] == b"\x02\x01"):  # noqa: PLR2004
                return data
            return None

        candidates = self._poll_collecting(timeout, decode)
        best = max(candidates, key=len, default=None)
        if best is None:
            return []
        payload = best[12:]
        if len(payload) < 4 or payload[0] != 4:  # noqa: PLR2004
            return []
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


# _SricamProtocol binds to the camera's port, so same-port sessions must serialize to avoid an "Address in use" race.
_port_locks: dict[int, asyncio.Lock] = {}


def _lock_for_port(port: int) -> asyncio.Lock:
    lock = _port_locks.get(port)
    if lock is None:
        lock = asyncio.Lock()
        _port_locks[port] = lock
    return lock


async def _run_blocking[T](hass: HomeAssistant, fn: Callable[[], T], *, port: int | None = None) -> T:
    """Run a blocking call in the executor, mapping raw exceptions to our hierarchy; set `port` if `fn` binds one."""
    context = _lock_for_port(port) if port is not None else contextlib.nullcontext()
    async with context:
        try:
            return await hass.async_add_executor_job(fn)
        except APIError:
            raise
        except OSError as err:
            raise APIConnectionError(str(err)) from err
        except Exception as err:
            raise APIError(str(err)) from err


class GwellIPCamClient:
    """Async-facing client for a single Gwell IP camera; wraps the sync protocol client above."""

    def __init__(self, hass: HomeAssistant, host: str, port: int, password_hash: str, entry_id: str) -> None:
        """Initialize the client for a specific camera."""
        self._hass = hass
        self._host = host
        self._port = port
        self._password_hash = password_hash
        self._entry_id = entry_id
        self._rtsp_session = RTSPSession(host)
        self._rtsp_proxy = RTSPProxyServer(self._rtsp_session)
        self._quick_record_store: Store[dict[str, int | None]] | None = None
        self._quick_record_saved_type: int | None = None

    def _get_quick_record_store(self) -> Store[dict[str, int | None]]:
        if self._quick_record_store is None:
            self._quick_record_store = Store(self._hass, version=1, key=f"gwell_ipcam.{self._entry_id}.quick_record")
        return self._quick_record_store

    async def _run[T](self, op: Callable[[_SricamProtocol], T], *, uid: str | None = None) -> T:
        def call() -> T:
            with _SricamProtocol(self._host, self._port, self._password_hash, uid=uid) as client:
                return op(client)

        return await _run_blocking(self._hass, call, port=self._port)

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
        await self._rtsp_proxy.stop()
        await self._rtsp_session.stop()

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
        # No real config entry exists yet at this point (or isn't relevant) -- this client is
        # discarded right after, so it never touches quick-record state that would need it.
        client = GwellIPCamClient(hass=hass, host=host, port=port, password_hash=password_hash, entry_id="")
        return await client.async_get_identity()

    async def async_get_identity(self) -> CameraIdentity:
        """Fetch the camera's identity. Name is synthesized -- no wire field carries one."""
        found = await self.async_discover_one(self._hass, self._host)
        if found is None:
            msg = f"no discovery reply from {self._host}"
            raise APIConnectionError(msg)
        contact_id = found.contact_id
        info = await self._run(lambda c: c.get_device_info())
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
        naive = await self._run(lambda c: c.get_device_time(), uid=uid)
        if naive is None:
            msg = "no response from camera"
            raise APIConnectionError(msg)
        return naive.replace(tzinfo=dt_util.DEFAULT_TIME_ZONE)

    async def async_sync_time(self, *, uid: str | None = None) -> None:
        """Push HA's current local time to the camera's clock."""
        now = dt_util.now().replace(tzinfo=None)
        await self._run(lambda c: c.set_device_time(now), uid=uid)

    async def async_get_storage_state(self, *, uid: str | None = None) -> StorageState:
        """Fetch SD card storage usage."""
        capacity = await self._run(lambda c: c.get_sd_card_capacity(), uid=uid)
        if capacity is None:
            msg = "no response from camera"
            raise APIConnectionError(msg)
        total, free, _sd_id = capacity
        return StorageState(used_mb=total - free, total_mb=total)

    async def async_get_settings(self, *, uid: str | None = None) -> dict[int, int]:
        """Fetch the full settingType -> value dump (noise IDs filtered out)."""
        dump = await self._run(lambda c: c.get_settings(), uid=uid)
        if dump is None:
            msg = "no response from camera"
            raise APIConnectionError(msg)
        return dump.clean_values()

    async def async_set_setting(self, setting_type: int, value: int, *, uid: str | None = None) -> None:
        """Write a settingType/value pair. Applies with latency; a missing ack doesn't mean it failed."""
        await self._run(lambda c: c.set_setting(setting_type, value), uid=uid)

    async def async_get_record_plan(self, *, uid: str | None = None) -> tuple[dtime, dtime] | None:
        """Fetch the Timing record schedule (start, end); `SETTING_RECORD_PLAN_TIME` is part of the general dump."""
        settings = await self.async_get_settings(uid=uid)
        value = settings.get(SETTING_RECORD_PLAN_TIME)
        return decode_record_plan_time(value) if value is not None else None

    async def async_set_record_plan(self, start: dtime, end: dtime, *, uid: str | None = None) -> None:
        """Write the Timing record schedule (start, end)."""
        await self.async_set_setting(SETTING_RECORD_PLAN_TIME, encode_record_plan_time(start, end), uid=uid)

    async def async_set_recording_state(self, *, enabled: bool, uid: str | None = None) -> None:
        """Start or stop recording."""
        await self.async_set_setting(SETTING_REMOTE_RECORD, 1 if enabled else 0, uid=uid)

    async def async_load_quick_record_state(self) -> None:
        """Load the persisted quick-record state once at startup; call before reading `quick_record_active`."""
        data = await self._get_quick_record_store().async_load()
        self._quick_record_saved_type = data.get("saved_record_type") if data else None

    @property
    def quick_record_active(self) -> bool:
        """Whether a quick-record session is currently in progress."""
        return self._quick_record_saved_type is not None

    async def async_toggle_quick_record(self, *, uid: str | None = None) -> bool:
        """First press: switch to Manual and start recording, remembering the prior mode to restore later."""
        if self._quick_record_saved_type is None:
            settings = await self.async_get_settings(uid=uid)
            self._quick_record_saved_type = settings.get(SETTING_RECORD_TYPE, RECORD_TYPE_MANUAL)
            await self._get_quick_record_store().async_save({"saved_record_type": self._quick_record_saved_type})
            await self.async_set_setting(SETTING_RECORD_TYPE, RECORD_TYPE_MANUAL, uid=uid)
            await self.async_set_recording_state(enabled=True, uid=uid)
            return True

        saved_type = self._quick_record_saved_type
        self._quick_record_saved_type = None
        await self._get_quick_record_store().async_save({"saved_record_type": None})
        await self.async_set_recording_state(enabled=False, uid=uid)
        await self.async_set_setting(SETTING_RECORD_TYPE, saved_type, uid=uid)
        return False

    async def async_get_record_quality(self, *, uid: str | None = None) -> int | None:
        """Fetch Record Quality (0-4)."""
        return await self._run(lambda c: c.get_record_quality(), uid=uid)

    async def async_set_record_quality(self, value: int, *, uid: str | None = None) -> None:
        """Set Record Quality (0-4)."""
        await self._run(lambda c: c.set_record_quality(value), uid=uid)

    async def async_format_sd_card(self, *, uid: str | None = None) -> None:
        """Format the camera's SD card."""
        result = await self._run(_format_sd_card_op, uid=uid)
        if result != "success":
            msg = f"SD card format failed: {result}"
            raise APIError(msg)

    async def async_get_recordings(self, *, uid: str | None = None) -> list[Recording]:
        """List recordings currently stored on the camera's SD card."""
        end = dt_util.now().replace(tzinfo=None)
        start = end - _RECORDINGS_LOOKBACK
        entries = await self._run(lambda c: c.get_rec_files(start, end), uid=uid)
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
        info = await self._run(lambda c: c.get_device_update_check())
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


def _format_sd_card_op(client: _SricamProtocol) -> str:
    capacity = client.get_sd_card_capacity()
    if capacity is None:
        msg = "no response from camera"
        raise APIConnectionError(msg)
    _total, _free, sd_id = capacity
    return client.format_sd_card(sd_id)


def _to_recording(entry: _RecFileEntry) -> Recording:
    return Recording(
        recording_id=f"{entry.disc}-{entry.timestamp:%Y%m%d%H%M%S}",
        started_at=entry.timestamp.replace(tzinfo=dt_util.DEFAULT_TIME_ZONE),
        duration=timedelta(seconds=entry.duration_s or 0),
        motion_triggered=entry.tag == "A",
    )
