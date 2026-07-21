"""API client for Gwell (Sricam/ieGeek) IP cameras. See docs/PROTOCOL.md for the wire format."""

from __future__ import annotations

import asyncio
import contextlib
import functools
import hashlib
import ipaddress
import random
import socket
import struct
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from cryptography.hazmat.decrepit.ciphers.algorithms import TripleDES
from cryptography.hazmat.primitives.ciphers import Cipher, modes
from homeassistant.util import dt as dt_util

from .const import DEFAULT_PORT, LOGGER, RTSP_PATH
from .fallback import FrameCache
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
    """
    Map an on-screen PTZ direction to the camera's own motor direction.

    Confirmed live: with `SETTING_IMAGE_FLIP` on, commanding the raw wire direction `left`
    visibly moves the camera right on screen (the motor's frame of reference doesn't follow
    the image flip); tilt is unaffected. Pan only, and only while the setting is enabled.
    """
    if settings.get(SETTING_IMAGE_FLIP, 0) and direction in _PTZ_MIRROR:
        return _PTZ_MIRROR[direction]
    return direction


_WEAK_PASSWORD_MIN_DIGITS = 6
_NUMERIC_PIN_MAX_DIGITS = 10

# Not real settings: cycle to random values on a timer (firmware artifact).
_NOISE_SETTING_IDS = {10, 22, 23, 33, 39, 42, 45, 51}

_DISCOVERY_PORT = 25143
# Once a "keep the latest of possibly-several replies" call gets its first response, only wait
# this much longer for a fresher one instead of the full timeout -- the camera essentially never
# sends a second, better reply in practice, so waiting out the full window every call is wasted time.
_RESPONSE_SETTLE_S = 0.3


def _log_hex(data: bytes) -> str:
    """Hex-encode for logging, dropping trailing zero-padding that just clutters the line."""
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
    """Identity information returned once a camera accepts a connection."""

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
    """Firmware availability information."""

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
    request = bytearray(1024)
    struct.pack_into(">III", request, 0, 1, 0, 0x1C)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.bind(("0.0.0.0", 0))  # noqa: S104 -- must receive broadcast replies on any interface
    sock.settimeout(0.2)
    try:
        LOGGER.debug("UDP send to %s:%s: %s", broadcast_ip, port, _log_hex(bytes(request)))
        sock.sendto(bytes(request), (broadcast_ip, port))
        found: dict[str, DiscoveredCamera] = {}
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                data, addr = sock.recvfrom(4096)
            except TimeoutError:
                continue
            LOGGER.debug("UDP recv from %s: %s", addr, _log_hex(data))
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

    def __init__(self, host: str, port: int, password_hash: str) -> None:
        self._host = host
        self._port = int(port)
        self._password_int = entry_password(password_hash)
        self._our_src_id = 100
        self._dst_id = int(_resolve_ipv4(host).split(".")[-1])
        self._sock: socket.socket | None = None

    def __enter__(self) -> Self:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        # the camera only replies when the client's source port matches its own port -- confirmed live
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
        LOGGER.debug("UDP send to %s:%s: %s", self._host, self._port, _log_hex(packet))
        self._sock_or_raise().sendto(packet, (self._host, self._port))

    def _send_extended(self, cmd_payload: bytes, msgid: int) -> None:
        self._send(self._password_block(_DES_KEY_MESG) + cmd_payload, msgid, subcmd=0x0B)

    def _recv(self) -> bytes | None:
        """Receive one datagram (or None on the socket's 0.2s timeout), logging it either way."""
        try:
            data, addr = self._sock_or_raise().recvfrom(4096)
        except TimeoutError:
            return None
        LOGGER.debug("UDP recv from %s: %s", addr, _log_hex(data))
        return data

    def __drain_settling(self, duration: float) -> list[bytes]:
        """Like `_drain`, but stop shortly after the first packet instead of always waiting `duration`."""
        packets = []
        deadline = time.monotonic() + duration
        settling = False
        while time.monotonic() < deadline:
            data = self._recv()
            if data is None:
                continue
            packets.append(data)
            # See get_record_quality() -- only start the settle countdown once, so the camera's
            # own unrelated periodic broadcasts on this port can't keep renewing it forever.
            if not settling:
                settling = True
                deadline = min(deadline, time.monotonic() + _RESPONSE_SETTLE_S)
        return packets

    def _drain(self, duration: float) -> list[bytes]:
        packets = []
        deadline = time.monotonic() + duration
        while time.monotonic() < deadline:
            data = self._recv()
            if data is None:
                continue
            packets.append(data)
        return packets

    def _flush_stale(self) -> None:
        while self._recv() is not None:
            pass

    @staticmethod
    def _msgid() -> int:
        return random.randint(30000, 39999)  # noqa: S311 -- not a security use

    def get_settings(self, timeout: float = 8.0) -> _SettingsDump | None:
        self._flush_stale()
        msgid = self._msgid()
        self._send(self._password_block(_DES_KEY_MESG) + bytes(4), msgid, subcmd=0x03)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            data = self._recv()
            if data is None:
                continue
            if data[0] == 0x60 and len(data) > 200:  # noqa: PLR2004
                payload = data[12:]
                count = struct.unpack_from("<H", payload, 2)[0]
                values = dict(struct.unpack_from("<II", payload, 4 + i * 8) for i in range(count))
                return _SettingsDump(values=values)
        return None

    def set_setting(self, setting_type: int, value: int) -> bool:
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
        self._flush_stale()
        self._send_extended(bytes([0xF0, 0x00, 0x00, 0x00, 0x00, 0x00]), self._msgid())
        deadline = time.monotonic() + timeout
        settling = False
        latest = None
        while time.monotonic() < deadline:
            data = self._recv()
            if data is None:
                continue
            if len(data) >= 15 and data[0] == 0x60 and data[12] == 0xF1:  # noqa: PLR2004
                latest = data[14]
                # The camera keeps re-broadcasting these values on its own regardless of our
                # request, so only start the settle countdown once (on the *first* match) --
                # otherwise a steady stream of unrelated broadcasts keeps renewing it forever.
                if not settling:
                    settling = True
                    deadline = min(deadline, time.monotonic() + _RESPONSE_SETTLE_S)
        return latest

    def set_record_quality(self, value: int) -> None:
        self._send_extended(bytes([0xEF, 0x00, value & 0xFF, 0x00, 0x00, 0x00]), self._msgid())
        self._drain(2.0)

    def get_sd_card_capacity(self, timeout: float = 5.0) -> tuple[int, int, int] | None:
        self._flush_stale()
        self._send_extended(bytes([0x50, 0x00, 0x00, 0x00]), self._msgid())
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            data = self._recv()
            if data is None:
                continue
            if len(data) >= 33 and data[0] == 0x60 and data[12] == 0x50:  # noqa: PLR2004
                payload = data[12:]
                total = struct.unpack_from("<I", payload, 8)[0] * 16
                free = struct.unpack_from("<I", payload, 16)[0] * 16
                return total, free, payload[4]
        return None

    def format_sd_card(self, sd_id: int, timeout: float = 3.0) -> str:
        self._send_extended(bytes([0x51, 0x00, 0x00, 0x00, sd_id & 0xFF]), self._msgid())
        for p in self._drain(timeout):
            if len(p) >= 15 and p[0] == 0x60 and p[12] == 0x51:  # noqa: PLR2004
                return _FORMAT_RESULT_CODES.get(p[13], f"unknown_{p[13]}")
        return "no_response"

    def get_device_time(self, timeout: float = 5.0) -> datetime | None:
        self._flush_stale()
        self._send_extended(bytes([0x0A, 0, 0, 0, 0, 0, 0, 0, 0]), self._msgid())
        deadline = time.monotonic() + timeout
        settling = False
        latest = None
        while time.monotonic() < deadline:
            data = self._recv()
            if data is None:
                continue
            if len(data) >= 22 and data[0] == 0x60 and data[12] == 0x0C:  # noqa: PLR2004
                p = data[12:]
                year = struct.unpack_from("<H", p, 4)[0]
                latest = datetime(year, p[6], p[7], p[8], p[9])  # noqa: DTZ001 -- naive camera-local time
                # See get_record_quality() -- only start the settle countdown once.
                if not settling:
                    settling = True
                    deadline = min(deadline, time.monotonic() + _RESPONSE_SETTLE_S)
        return latest

    def set_device_time(self, dt: datetime, timeout: float = 3.0) -> None:
        body = bytes([0x0B, 0, 0, 0]) + struct.pack("<H", dt.year) + bytes([dt.month, dt.day, dt.hour, dt.minute])
        self._send_extended(body, self._msgid())
        self._drain(timeout)

    def get_device_info(self, timeout: float = 5.0) -> dict[str, str | int] | None:
        self._flush_stale()
        payload = self._password_block(_DES_KEY_MESG) + bytes([0x27]) + bytes(35)
        self._send(payload, self._msgid(), subcmd=0x03)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            data = self._recv()
            if data is None:
                continue
            if len(data) == 48 and data[0] == 0x60 and data[12] == 0x28:  # noqa: PLR2004
                p = data[12:]
                return {"device_version": f"{p[7]}.{p[6]}.{p[5]}.{p[4]}"}
        return None

    _DEVICE_UPDATE_CHECK_TAIL = bytes.fromhex("1d6ce42301000000e0ae59cb01000000")

    def get_device_update_check(self, timeout: float = 15.0) -> dict[str, str | int] | None:
        self._flush_stale()
        payload = self._password_block(_DES_KEY_MESG) + self._DEVICE_UPDATE_CHECK_TAIL
        self._send(payload, self._msgid(), subcmd=0x03)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            data = self._recv()
            if data is None:
                continue
            if len(data) == 24 and data[0] == 0x60 and data[12] == 0x1E:  # noqa: PLR2004
                p = data[12:]
                return {
                    "result": p[1],
                    "cur_version": f"{p[7]}.{p[6]}.{p[5]}.{p[4]}",
                    "upg_version": f"{p[11]}.{p[10]}.{p[9]}.{p[8]}",
                }
        return None

    _GETRECFILES_FIELD = bytes.fromhex("03010000")

    @staticmethod
    def _pack_datetime(dt: datetime) -> bytes:
        return struct.pack("<HBBBB", dt.year, dt.month, dt.day, dt.hour, dt.minute)

    def get_rec_files(self, start: datetime, end: datetime, timeout: float = 15.0) -> list[_RecFileEntry]:
        self._flush_stale()
        payload = (
            self._password_block(_DES_KEY_MESG)
            + self._GETRECFILES_FIELD
            + self._pack_datetime(start)
            + self._pack_datetime(end)
        )
        self._send(payload, self._msgid(), subcmd=0x0B)
        candidates = [
            p
            for p in self.__drain_settling(timeout)
            if p[:1] == b"\x60" and not (len(p) > 200 and p[12:14] == b"\x02\x01")  # noqa: PLR2004
        ]
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


# _SricamProtocol binds its local UDP socket to the camera's own port (the camera only replies to
# that exact source port), so two cameras sharing a port can't have overlapping sessions -- serialize
# per port to avoid an "Address already in use" race between concurrently-polled cameras.
_port_locks: dict[int, asyncio.Lock] = {}


def _lock_for_port(port: int) -> asyncio.Lock:
    lock = _port_locks.get(port)
    if lock is None:
        lock = asyncio.Lock()
        _port_locks[port] = lock
    return lock


async def _run_blocking[T](hass: HomeAssistant, fn: Callable[[], T], *, port: int | None = None) -> T:
    """
    Run a blocking call in the executor, mapping any raw exception into our own hierarchy.

    `port` should be set whenever `fn` binds a local UDP socket to a fixed port (i.e. any
    `_SricamProtocol` session) so concurrent calls sharing that port are serialized instead of
    racing for the bind. Discovery uses an ephemeral local port and doesn't need it.
    """
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

    def __init__(self, hass: HomeAssistant, host: str, port: int, password_hash: str) -> None:
        """Initialize the client for a specific camera."""
        self._hass = hass
        self._host = host
        self._port = port
        self._password_hash = password_hash
        self._quick_record_saved_type: int | None = None
        self._rtsp_session = RTSPSession(host)
        self._frame_cache = FrameCache()
        self._rtsp_proxy = RTSPProxyServer(self._rtsp_session, self._frame_cache)

    async def _run[T](self, op: Callable[[_SricamProtocol], T]) -> T:
        def call() -> T:
            with _SricamProtocol(self._host, self._port, self._password_hash) as client:
                return op(client)

        return await _run_blocking(self._hass, call, port=self._port)

    @property
    def rtsp_session(self) -> RTSPSession:
        """The shared upstream RTSP session (for the assist_satellite mic feed)."""
        return self._rtsp_session

    @property
    def frame_cache(self) -> FrameCache:
        """The last-known-good frame, used as a background for the offline fallback image."""
        return self._frame_cache

    async def async_start_streaming(self) -> None:
        """
        Open the shared RTSP session and start the local header-fixing proxy.

        Kept open for the config entry's lifetime -- continuous streaming, not opened
        on demand per viewer.
        """
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
        return await _run_blocking(hass, functools.partial(_discover, timeout=timeout_s))

    @staticmethod
    async def async_discover_one(hass: HomeAssistant, host: str, timeout_s: float = 2.0) -> DiscoveredCamera | None:
        """Query a single known host for its contact_id (e.g. to fill in what DHCP discovery can't provide)."""
        found = await _run_blocking(hass, functools.partial(_discover, broadcast_ip=host, timeout=timeout_s))
        return found[0] if found else None

    @staticmethod
    def hash_password(password: str) -> str:
        """Hash a plaintext password using the camera's hashing scheme."""
        return str(entry_password(password))

    @staticmethod
    async def async_check_connection(hass: HomeAssistant, host: str, port: int, password_hash: str) -> CameraIdentity:
        """Verify that a camera is reachable and accepts the given password hash."""
        return await GwellIPCamClient(hass=hass, host=host, port=port, password_hash=password_hash).async_get_identity()

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

    async def async_get_camera_time(self) -> datetime:
        """Fetch the camera's clock, localized to HA's configured timezone (camera keeps no tz of its own)."""
        naive = await self._run(lambda c: c.get_device_time())
        if naive is None:
            msg = "no response from camera"
            raise APIConnectionError(msg)
        return naive.replace(tzinfo=dt_util.DEFAULT_TIME_ZONE)

    async def async_sync_time(self) -> None:
        """Push HA's current local time to the camera's clock."""
        now = dt_util.now().replace(tzinfo=None)
        await self._run(lambda c: c.set_device_time(now))

    async def async_get_storage_state(self) -> StorageState:
        """Fetch SD card storage usage."""
        capacity = await self._run(lambda c: c.get_sd_card_capacity())
        if capacity is None:
            msg = "no response from camera"
            raise APIConnectionError(msg)
        total, free, _sd_id = capacity
        return StorageState(used_mb=total - free, total_mb=total)

    async def async_get_settings(self) -> dict[int, int]:
        """Fetch the full settingType -> value dump (noise IDs filtered out)."""
        dump = await self._run(lambda c: c.get_settings())
        if dump is None:
            msg = "no response from camera"
            raise APIConnectionError(msg)
        return dump.clean_values()

    async def async_set_setting(self, setting_type: int, value: int) -> None:
        """Write a settingType/value pair. Applies with latency; a missing ack doesn't mean it failed."""
        await self._run(lambda c: c.set_setting(setting_type, value))

    async def async_set_recording_state(self, *, enabled: bool) -> None:
        """Start or stop recording."""
        await self.async_set_setting(SETTING_REMOTE_RECORD, 1 if enabled else 0)

    async def async_toggle_quick_record(self) -> bool:
        """Start recording in Manual mode, remembering the prior mode; next call stops and restores it."""
        if self._quick_record_saved_type is None:
            settings = await self.async_get_settings()
            self._quick_record_saved_type = settings.get(SETTING_RECORD_TYPE, RECORD_TYPE_MANUAL)
            await self.async_set_setting(SETTING_RECORD_TYPE, RECORD_TYPE_MANUAL)
            await self.async_set_recording_state(enabled=True)
            return True

        saved_type = self._quick_record_saved_type
        self._quick_record_saved_type = None
        await self.async_set_recording_state(enabled=False)
        await self.async_set_setting(SETTING_RECORD_TYPE, saved_type)
        return False

    async def async_get_record_quality(self) -> int | None:
        """Fetch Record Quality (0-4)."""
        return await self._run(lambda c: c.get_record_quality())

    async def async_set_record_quality(self, value: int) -> None:
        """Set Record Quality (0-4)."""
        await self._run(lambda c: c.set_record_quality(value))

    async def async_format_sd_card(self) -> None:
        """Format the camera's SD card."""
        result = await self._run(_format_sd_card_op)
        if result != "success":
            msg = f"SD card format failed: {result}"
            raise APIError(msg)

    async def async_get_recordings(self) -> list[Recording]:
        """List recordings currently stored on the camera's SD card."""
        end = dt_util.now().replace(tzinfo=None)
        start = end - _RECORDINGS_LOOKBACK
        entries = await self._run(lambda c: c.get_rec_files(start, end))
        return [_to_recording(entry) for entry in entries]

    async def async_stream_recording(self, recording_id: str) -> AsyncIterator[bytes]:  # noqa: ARG002
        """Stub: no wire format for fetching a recording's video bytes exists yet."""
        empty: tuple[bytes, ...] = ()
        for chunk in empty:
            yield chunk

    async def async_get_live_stream_url(self) -> str | None:
        """Return the local proxy's RTSP URL, or None if streaming hasn't been started yet."""
        return f"rtsp://127.0.0.1:{self._rtsp_proxy.port}{RTSP_PATH}"

    async def async_get_latest_recording_thumbnail(self) -> bytes | None:
        """Stub: no wire format for fetching a thumbnail exists yet."""
        return None

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
