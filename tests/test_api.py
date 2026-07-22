"""
Wire-format regression tests for custom_components/gwell_ipcam/api.py.

Plain pytest, no HA test harness needed -- these exercise the private
synchronous protocol client (`_SricamProtocol`) and free functions directly,
asserting exact byte layout against values confirmed live against a real
camera (see docs/PROTOCOL.md). A FakeSocket stands in for the network.
"""

from __future__ import annotations

import struct
from datetime import datetime, timedelta
from datetime import time as dtime
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.gwell_ipcam import api as sc


class FakeSocket:
    """Minimal socket stand-in. `recvfrom` only yields queued replies after a `sendto`."""

    def __init__(self, responses: list[bytes] | None = None) -> None:
        self.responses = list(responses or [])
        self.sent: list[bytes] = []

    def setsockopt(self, *_args: object) -> None:
        return

    def bind(self, _addr: tuple[str, int]) -> None:
        return

    def settimeout(self, _seconds: float) -> None:
        return

    def sendto(self, data: bytes, _addr: tuple[str, int]) -> None:
        self.sent.append(data)

    def recvfrom(self, _bufsize: int) -> tuple[bytes, tuple[str, int]]:
        if not self.sent or not self.responses:
            raise TimeoutError
        return self.responses.pop(0), ("192.168.0.66", 51880)

    def close(self) -> None:
        return


def make_client(responses: list[bytes] | None = None, password_hash: str = "888888") -> sc._SricamProtocol:
    client = sc._SricamProtocol("192.168.0.66", 51880, password_hash)
    client._sock = FakeSocket(responses)  # ty: ignore[invalid-assignment]
    return client


def sent(client: sc._SricamProtocol) -> list[bytes]:
    """Typed accessor for the FakeSocket packets a test client sent."""
    return client._sock.sent  # ty: ignore[unresolved-attribute]


class _FakeHass:
    async def async_add_executor_job(self, fn):
        return fn()


# -- entry_password / weak-password reroll, hardcoded against real camera output ------


@pytest.mark.parametrize(
    ("password", "expected"),
    [
        ("888888", 888888),  # short numeric PIN: used as-is, no hashing
        ("012345", 493320785),  # leading zero disqualifies it as a PIN -> hashed instead
        ("camtest12", 636734832),  # non-numeric password -> hashed (confirmed live)
    ],
)
def test_entry_password(password, expected):
    assert sc.entry_password(password) == expected


def test_hash_password_round_trips_through_entry_password():
    """A stored hash must survive being fed back through entry_password() unchanged."""
    hashed = sc.GwellIPCamClient.hash_password("camtest12")
    assert hashed == "636734832"
    assert sc.entry_password(hashed) == 636734832


def test_sricam_protocol_coerces_float_port_to_int():
    client = sc._SricamProtocol("192.168.0.66", 51880.0, "888888")  # ty: ignore[invalid-argument-type]
    assert client._port == 51880
    assert isinstance(client._port, int)


# -- record_plan_time (settingType 5) encoding, hardcoded against MyUtils.convertPlanTime ------


@pytest.mark.parametrize(
    ("start", "end", "expected"),
    [
        (dtime(8, 0), dtime(18, 30), 0x08120000 | (30)),  # 08:00-18:30
        (dtime(0, 0), dtime(23, 59), 0x00170000 | 59),  # 00:00-23:59
        (dtime(9, 5), dtime(17, 45), (9 << 24) | (17 << 16) | (5 << 8) | 45),
    ],
)
def test_encode_record_plan_time(start, end, expected):
    assert sc.encode_record_plan_time(start, end) == expected


@pytest.mark.parametrize(
    ("value", "expected_start", "expected_end"),
    [
        (0x08000000 | (18 << 16) | (0 << 8) | 30, dtime(8, 0), dtime(18, 30)),
        ((9 << 24) | (17 << 16) | (5 << 8) | 45, dtime(9, 5), dtime(17, 45)),
    ],
)
def test_decode_record_plan_time(value, expected_start, expected_end):
    assert sc.decode_record_plan_time(value) == (expected_start, expected_end)


def test_record_plan_time_round_trips():
    start, end = dtime(6, 15), dtime(22, 40)
    assert sc.decode_record_plan_time(sc.encode_record_plan_time(start, end)) == (start, end)


def test_decode_record_plan_time_treats_24_00_as_end_of_day():
    # 1572864 (0x180000) is the firmware's observed factory-default value: 00:00-24:00.
    assert sc.decode_record_plan_time(1572864) == (dtime(0, 0), dtime(23, 59))


@pytest.mark.parametrize("value", [25 << 16, 25 << 24, 60 << 8, 60])
def test_decode_record_plan_time_returns_none_for_out_of_range(value):
    assert sc.decode_record_plan_time(value) is None


# -- discovery -----------------------------------------------------------------


def test_discover_parses_search_reply():
    reply = bytearray(96)
    struct.pack_into(">I", reply, 0, 2)
    struct.pack_into(">I", reply, 16, 1283250)
    fake = FakeSocket([bytes(reply)])
    with patch("custom_components.gwell_ipcam.api.socket.socket", return_value=fake):
        found = sc._discover(broadcast_ip="192.168.0.66", timeout=0.05)
    assert found == [sc.DiscoveredCamera(host="192.168.0.66", port=51880, contact_id="1283250", name="IPCam-1283250")]


def test_discover_ignores_non_reply_packets():
    junk = bytes(96)  # op=0, not SEARCH_REPLY(2)
    fake = FakeSocket([junk])
    with patch("custom_components.gwell_ipcam.api.socket.socket", return_value=fake):
        found = sc._discover(broadcast_ip="192.168.0.66", timeout=0.05)
    assert found == []


# -- settings read/write --------------------------------------------------------


def test_get_settings_parses_dump_and_filters_noise():
    payload = bytearray(4 + 3 * 8)
    payload[0:2] = bytes([0x02, 0x01])  # settings-dump response tag
    struct.pack_into("<H", payload, 2, 3)
    struct.pack_into("<II", payload, 4, 0, 1)  # remote_defence=1
    struct.pack_into("<II", payload, 12, 4, 1)  # remote_record=1
    struct.pack_into("<II", payload, 20, 10, 999)  # noise ID, must be filtered
    resp = bytes([0x60]) + bytes(11) + bytes(payload)
    resp = resp + bytes(201 - len(resp))  # pad past the "len(data) > 200" gate
    client = make_client(responses=[resp])
    dump = client.get_settings(timeout=0.05)
    assert dump is not None
    assert dump.clean_values() == {0: 1, 4: 1}


def test_get_settings_ignores_other_large_0x60_responses():
    """A same-shape response with a different tag (e.g. recorded-file listing) must not be misread as a dump."""
    other_response_payload = bytes([0x04, 0x01]) + bytes(200)
    resp = bytes([0x60]) + bytes(11) + other_response_payload
    client = make_client(responses=[resp])
    assert client.get_settings(timeout=0.05) is None


def test_set_setting_wire_format():
    client = make_client()
    client.set_setting(4, 1)
    sent_bytes = sent(client)[0]
    assert sent_bytes[0] == 0x60
    assert sent_bytes[1] == 0x0B
    payload = sent_bytes[12:]
    assert payload[8:12] == bytes.fromhex("01000100")
    setting_type, value = struct.unpack_from("<II", payload, 12)
    assert (setting_type, value) == (4, 1)


def test_set_setting_detects_ack():
    ack = bytes([0x60, 0x02]) + bytes(10) + bytes([0x02]) + bytes(3) + struct.pack("<II", 4, 1)
    client = make_client(responses=[ack])
    assert client.set_setting(4, 1) is True


def test_set_setting_no_ack_returns_false():
    client = make_client(responses=[])
    assert client.set_setting(4, 1) is False


# -- record quality: read (0xF0) and write (0xEF) are DIFFERENT commands --------


def test_get_record_quality_uses_read_tag_0xf0():
    client = make_client()
    client.get_record_quality(timeout=0.05)
    sent_cmd_byte = sent(client)[0][12 + 8]
    assert sent_cmd_byte == 0xF0


def test_get_record_quality_parses_response():
    resp = bytes([0x60]) + bytes(11) + bytes([0xF1, 0, 3, 0, 0, 0])
    client = make_client(responses=[resp])
    assert client.get_record_quality(timeout=0.05) == 3


def test_set_record_quality_uses_write_tag_0xef_not_0xf0():
    client = make_client()
    client.set_record_quality(3)
    sent_bytes = sent(client)[0]
    assert sent_bytes[12 + 8] == 0xEF
    assert sent_bytes[12 + 8 + 2] == 3


# -- SD card capacity: exact offsets, including SDcardID at offset 4 -----------


def test_get_sd_card_capacity_offsets():
    payload = bytearray(33 - 12)
    payload[0] = 0x50
    payload[4] = 0x10
    struct.pack_into("<I", payload, 8, 3691)
    struct.pack_into("<I", payload, 16, 2894)
    resp = bytes([0x60]) + bytes(11) + bytes(payload)
    client = make_client(responses=[resp])
    capacity = client.get_sd_card_capacity(timeout=0.05)
    assert capacity == (3691 * 16, 2894 * 16, 0x10)


# -- Format SD card: wire format + result-code decoding -------------------------


def test_format_sd_card_wire_format():
    client = make_client()
    client.format_sd_card(0x10, timeout=0.05)
    body = sent(client)[0][12 + 8 :]
    assert body[0] == 0x51
    assert body[1:4] == bytes(3)
    assert body[4] == 0x10


@pytest.mark.parametrize(
    ("code", "expected"), [(80, "success"), (81, "fail"), (82, "no_sd"), (103, "must_stop_record")]
)
def test_format_sd_card_decodes_result_code(code, expected):
    resp = bytes([0x60]) + bytes(11) + bytes([0x51, code, 0x00, 0x00, 0x10])
    client = make_client(responses=[resp])
    assert client.format_sd_card(0x10, timeout=0.05) == expected


def test_format_sd_card_no_response():
    client = make_client(responses=[])
    assert client.format_sd_card(0x10, timeout=0.05) == "no_response"


# -- device time -----------------------------------------------------------------


def test_get_device_time_request_tag():
    client = make_client()
    client.get_device_time(timeout=0.05)
    assert sent(client)[0][12 + 8] == 0x0A


def test_get_device_time_parses_response():
    payload = bytes([0x0C, 0, 0, 0]) + struct.pack("<H", 2026) + bytes([7, 13, 8, 15])
    resp = bytes([0x60]) + bytes(11) + payload
    client = make_client(responses=[resp])
    dt = client.get_device_time(timeout=0.05)
    assert dt == datetime(2026, 7, 13, 8, 15)


def test_set_device_time_wire_format():
    client = make_client()
    client.set_device_time(datetime(2026, 7, 13, 8, 15))
    body = sent(client)[0][12 + 8 :]
    assert body[0] == 0x0B
    year = struct.unpack_from("<H", body, 4)[0]
    assert (year, body[6], body[7], body[8], body[9]) == (2026, 7, 13, 8, 15)


# -- device info / firmware update check -----------------------------------------


def test_get_device_info_request_tag():
    client = make_client()
    client.get_device_info(timeout=0.05)
    assert sent(client)[0][12 + 8] == 0x27


def test_get_device_info_reverse_byte_version():
    payload = bytes([0x28, 0, 0, 0]) + bytes([30, 0, 0, 21]) + bytes(12)
    resp = bytes([0x60]) + bytes(11) + payload + bytes(48 - 12 - len(payload))
    client = make_client(responses=[resp])
    info = client.get_device_info(timeout=0.05)
    assert info == {"device_version": "21.0.0.30"}


def test_get_device_update_check_reverse_byte_versions():
    payload = bytes([0x1E, 1, 0, 0]) + bytes([30, 0, 0, 21]) + bytes([31, 0, 0, 21])
    resp = bytes([0x60]) + bytes(11) + payload
    client = make_client(responses=[resp])
    info = client.get_device_update_check(timeout=0.05)
    assert info == {"result": 1, "cur_version": "21.0.0.30", "upg_version": "21.0.0.31"}


# -- recorded file listing -------------------------------------------------------


def test_get_rec_files_parses_entries_without_duration():
    entry = struct.pack("<H", 2026) + bytes([7, 13, 8, 15, 48]) + b"M"
    payload = bytes([4, 0, 0, 1]) + entry
    resp = bytes([0x60]) + bytes(11) + payload
    client = make_client(responses=[resp])
    entries = client.get_rec_files(datetime(2026, 7, 1), datetime(2026, 7, 14), timeout=0.05)
    assert entries == [
        sc._RecFileEntry(timestamp=datetime(2026, 7, 13, 8, 15, 48), disc=0, tag="M", duration_s=None)
    ]


def test_get_rec_files_parses_durations_when_flagged():
    entry = struct.pack("<H", 2026) + bytes([7, 13, 8, 15, 48]) + b"A"
    payload = bytes([4, 1, 0, 1]) + entry + struct.pack("<H", 120)
    resp = bytes([0x60]) + bytes(11) + payload
    client = make_client(responses=[resp])
    entries = client.get_rec_files(datetime(2026, 7, 1), datetime(2026, 7, 14), timeout=0.05)
    assert entries == [sc._RecFileEntry(timestamp=datetime(2026, 7, 13, 8, 15, 48), disc=0, tag="A", duration_s=120)]


def test_get_rec_files_excludes_settings_dump_lookalikes():
    settings_lookalike = bytes([0x60]) + bytes(11) + bytes([0x02, 0x01]) + bytes(200)
    client = make_client(responses=[settings_lookalike])
    entries = client.get_rec_files(datetime(2026, 7, 1), datetime(2026, 7, 14), timeout=0.05)
    assert entries == []


# -- _to_recording mapping --------------------------------------------------------


def test_to_recording_maps_fields():
    entry = sc._RecFileEntry(timestamp=datetime(2026, 7, 13, 8, 15, 48), disc=0, tag="A", duration_s=120)
    assert sc._to_recording(entry) == sc.Recording(
        recording_id="0-20260713081548",
        started_at=datetime(2026, 7, 13, 8, 15, 48, tzinfo=sc.dt_util.DEFAULT_TIME_ZONE),
        duration=timedelta(seconds=120),
        motion_triggered=True,
    )


def test_to_recording_missing_duration_defaults_to_zero():
    entry = sc._RecFileEntry(timestamp=datetime(2026, 7, 13, 8, 15, 48), disc=1, tag="M", duration_s=None)
    assert sc._to_recording(entry) == sc.Recording(
        recording_id="1-20260713081548",
        started_at=datetime(2026, 7, 13, 8, 15, 48, tzinfo=sc.dt_util.DEFAULT_TIME_ZONE),
        duration=timedelta(0),
        motion_triggered=False,
    )


# -- GwellIPCamClient: quick record ---------------------------------------------


@pytest.mark.asyncio
class _FakeQuickRecordStore:
    """Stands in for the persisted `Store`, so tests don't need real HA storage plumbing."""

    def __init__(self) -> None:
        self.saved: dict[str, int | None] | None = None

    async def async_load(self) -> dict[str, int | None] | None:
        return self.saved

    async def async_save(self, data: dict[str, int | None]) -> None:
        self.saved = data


async def test_toggle_quick_record_starts_then_stops_and_restores_prior_mode():
    """Also covers that the *original* record mode (not a hardcoded one) is what gets restored."""
    client = sc.GwellIPCamClient(hass=object(), host="192.168.0.66", port=51880, password_hash="888888", entry_id="e")  # ty: ignore[invalid-argument-type]
    fake_store = _FakeQuickRecordStore()
    with (
        patch.object(client, "_get_quick_record_store", return_value=fake_store),
        patch.object(client, "async_get_settings", AsyncMock(return_value={sc.SETTING_RECORD_TYPE: 1})),
        patch.object(client, "async_set_setting", AsyncMock()) as set_setting,
        patch.object(client, "async_set_recording_state", AsyncMock()) as set_recording,
    ):
        assert client.quick_record_active is False
        started = await client.async_toggle_quick_record()
        assert started is True
        assert client.quick_record_active is True
        set_setting.assert_called_once_with(sc.SETTING_RECORD_TYPE, sc.RECORD_TYPE_MANUAL, uid=None)
        set_recording.assert_called_once_with(enabled=True, uid=None)

        set_setting.reset_mock()
        set_recording.reset_mock()
        stopped = await client.async_toggle_quick_record()
        assert stopped is False
        assert client.quick_record_active is False
        set_recording.assert_called_once_with(enabled=False, uid=None)
        set_setting.assert_called_once_with(sc.SETTING_RECORD_TYPE, 1, uid=None)  # the mode saved before starting


async def test_quick_record_state_survives_a_reload_via_the_store():
    """The whole point of persisting to a Store: a fresh client instance picks up the in-progress state."""
    fake_store = _FakeQuickRecordStore()
    fake_store.saved = {"saved_record_type": 2}
    client = sc.GwellIPCamClient(hass=object(), host="192.168.0.66", port=51880, password_hash="888888", entry_id="e")  # ty: ignore[invalid-argument-type]
    with patch.object(client, "_get_quick_record_store", return_value=fake_store):
        assert client.quick_record_active is False
        await client.async_load_quick_record_state()
        assert client.quick_record_active is True


# -- GwellIPCamClient: thin async wrappers around _run --------------------------


@pytest.mark.asyncio
async def test_async_get_camera_time_localizes_naive_result():
    client = sc.GwellIPCamClient(hass=object(), host="192.168.0.66", port=51880, password_hash="888888", entry_id="e")  # ty: ignore[invalid-argument-type]
    with patch.object(client, "_run", AsyncMock(return_value=datetime(2026, 7, 13, 8, 15))):
        result = await client.async_get_camera_time()
    assert result == datetime(2026, 7, 13, 8, 15, tzinfo=sc.dt_util.DEFAULT_TIME_ZONE)


@pytest.mark.asyncio
async def test_async_get_camera_time_raises_on_no_response():
    client = sc.GwellIPCamClient(hass=object(), host="192.168.0.66", port=51880, password_hash="888888", entry_id="e")  # ty: ignore[invalid-argument-type]
    with (
        patch.object(client, "_run", AsyncMock(return_value=None)),
        pytest.raises(sc.APIConnectionError),
    ):
        await client.async_get_camera_time()


@pytest.mark.asyncio
async def test_async_sync_time_pushes_current_local_time():
    client = sc.GwellIPCamClient(hass=object(), host="192.168.0.66", port=51880, password_hash="888888", entry_id="e")  # ty: ignore[invalid-argument-type]
    with patch.object(client, "_run", AsyncMock()) as run:
        await client.async_sync_time()
    run.assert_called_once()


@pytest.mark.asyncio
async def test_async_get_storage_state_computes_used_from_total_minus_free():
    client = sc.GwellIPCamClient(hass=object(), host="192.168.0.66", port=51880, password_hash="888888", entry_id="e")  # ty: ignore[invalid-argument-type]
    with patch.object(client, "_run", AsyncMock(return_value=(3691 * 16, 2894 * 16, 0x10))):
        result = await client.async_get_storage_state()
    assert result == sc.StorageState(used_mb=(3691 - 2894) * 16, total_mb=3691 * 16)


@pytest.mark.asyncio
async def test_async_get_storage_state_raises_on_no_response():
    client = sc.GwellIPCamClient(hass=object(), host="192.168.0.66", port=51880, password_hash="888888", entry_id="e")  # ty: ignore[invalid-argument-type]
    with (
        patch.object(client, "_run", AsyncMock(return_value=None)),
        pytest.raises(sc.APIConnectionError),
    ):
        await client.async_get_storage_state()


@pytest.mark.asyncio
async def test_async_get_settings_filters_noise_via_clean_values():
    client = sc.GwellIPCamClient(hass=object(), host="192.168.0.66", port=51880, password_hash="888888", entry_id="e")  # ty: ignore[invalid-argument-type]
    dump = sc._SettingsDump(values={0: 1, 10: 999})  # 10 is a noise ID
    with patch.object(client, "_run", AsyncMock(return_value=dump)):
        result = await client.async_get_settings()
    assert result == {0: 1}


@pytest.mark.asyncio
async def test_async_get_settings_raises_on_no_response():
    client = sc.GwellIPCamClient(hass=object(), host="192.168.0.66", port=51880, password_hash="888888", entry_id="e")  # ty: ignore[invalid-argument-type]
    with (
        patch.object(client, "_run", AsyncMock(return_value=None)),
        pytest.raises(sc.APIConnectionError),
    ):
        await client.async_get_settings()


@pytest.mark.asyncio
async def test_async_set_recording_state_writes_remote_record_setting():
    client = sc.GwellIPCamClient(hass=object(), host="192.168.0.66", port=51880, password_hash="888888", entry_id="e")  # ty: ignore[invalid-argument-type]
    with patch.object(client, "async_set_setting", AsyncMock()) as set_setting:
        await client.async_set_recording_state(enabled=True)
    set_setting.assert_called_once_with(sc.SETTING_REMOTE_RECORD, 1, uid=None)


@pytest.mark.asyncio
async def test_async_get_record_quality_delegates_to_run():
    client = sc.GwellIPCamClient(hass=object(), host="192.168.0.66", port=51880, password_hash="888888", entry_id="e")  # ty: ignore[invalid-argument-type]
    with patch.object(client, "_run", AsyncMock(return_value=3)):
        assert await client.async_get_record_quality() == 3


@pytest.mark.asyncio
async def test_async_get_record_plan_decodes_from_settings():
    client = sc.GwellIPCamClient(hass=object(), host="192.168.0.66", port=51880, password_hash="888888", entry_id="e")  # ty: ignore[invalid-argument-type]
    value = sc.encode_record_plan_time(dtime(8, 0), dtime(18, 30))
    with patch.object(client, "async_get_settings", AsyncMock(return_value={sc.SETTING_RECORD_PLAN_TIME: value})):
        assert await client.async_get_record_plan() == (dtime(8, 0), dtime(18, 30))


@pytest.mark.asyncio
async def test_async_get_record_plan_returns_none_when_setting_missing():
    client = sc.GwellIPCamClient(hass=object(), host="192.168.0.66", port=51880, password_hash="888888", entry_id="e")  # ty: ignore[invalid-argument-type]
    with patch.object(client, "async_get_settings", AsyncMock(return_value={})):
        assert await client.async_get_record_plan() is None


@pytest.mark.asyncio
async def test_async_set_record_plan_writes_encoded_setting():
    client = sc.GwellIPCamClient(hass=object(), host="192.168.0.66", port=51880, password_hash="888888", entry_id="e")  # ty: ignore[invalid-argument-type]
    with patch.object(client, "async_set_setting", AsyncMock()) as set_setting:
        await client.async_set_record_plan(dtime(8, 0), dtime(18, 30))
    set_setting.assert_called_once_with(
        sc.SETTING_RECORD_PLAN_TIME, sc.encode_record_plan_time(dtime(8, 0), dtime(18, 30)), uid=None
    )


@pytest.mark.asyncio
async def test_async_format_sd_card_raises_on_failure_result():
    client = sc.GwellIPCamClient(hass=object(), host="192.168.0.66", port=51880, password_hash="888888", entry_id="e")  # ty: ignore[invalid-argument-type]
    with (
        patch.object(client, "_run", AsyncMock(return_value="fail")),
        pytest.raises(sc.APIError, match="fail"),
    ):
        await client.async_format_sd_card()


@pytest.mark.asyncio
async def test_async_format_sd_card_succeeds_silently():
    client = sc.GwellIPCamClient(hass=object(), host="192.168.0.66", port=51880, password_hash="888888", entry_id="e")  # ty: ignore[invalid-argument-type]
    with patch.object(client, "_run", AsyncMock(return_value="success")):
        await client.async_format_sd_card()


@pytest.mark.asyncio
async def test_async_get_recordings_maps_entries():
    client = sc.GwellIPCamClient(hass=object(), host="192.168.0.66", port=51880, password_hash="888888", entry_id="e")  # ty: ignore[invalid-argument-type]
    entry = sc._RecFileEntry(timestamp=datetime(2026, 7, 13, 8, 15, 48), disc=0, tag="A", duration_s=120)
    with patch.object(client, "_run", AsyncMock(return_value=[entry])):
        result = await client.async_get_recordings()
    assert result == [sc._to_recording(entry)]


@pytest.mark.asyncio
async def test_async_get_live_stream_url_uses_local_proxy_port():
    client = sc.GwellIPCamClient(hass=object(), host="192.168.0.66", port=51880, password_hash="888888", entry_id="e")  # ty: ignore[invalid-argument-type]
    with patch.object(type(client._rtsp_proxy), "port", new=40000):
        url = await client.async_get_live_stream_url()
    assert url == f"rtsp://127.0.0.1:40000{sc.RTSP_PATH}"


@pytest.mark.asyncio
async def test_async_get_firmware_info_reports_update_available():
    client = sc.GwellIPCamClient(hass=object(), host="192.168.0.66", port=51880, password_hash="888888", entry_id="e")  # ty: ignore[invalid-argument-type]
    info = {"result": 1, "cur_version": "21.0.0.30", "upg_version": "21.0.0.31"}
    with patch.object(client, "_run", AsyncMock(return_value=info)):
        result = await client.async_get_firmware_info()
    assert result == sc.FirmwareInfo(latest_version="21.0.0.31", release_summary=None, release_url=None)


@pytest.mark.asyncio
async def test_async_get_firmware_info_reports_no_update():
    client = sc.GwellIPCamClient(hass=object(), host="192.168.0.66", port=51880, password_hash="888888", entry_id="e")  # ty: ignore[invalid-argument-type]
    info = {"result": 53, "cur_version": "21.0.0.30", "upg_version": "0.0.0.0"}  # noqa: S104 -- a version string, not a bind address
    with patch.object(client, "_run", AsyncMock(return_value=info)):
        result = await client.async_get_firmware_info()
    assert result == sc.FirmwareInfo(latest_version="21.0.0.30", release_summary=None, release_url=None)


@pytest.mark.asyncio
async def test_async_get_firmware_info_raises_on_no_response():
    client = sc.GwellIPCamClient(hass=object(), host="192.168.0.66", port=51880, password_hash="888888", entry_id="e")  # ty: ignore[invalid-argument-type]
    with (
        patch.object(client, "_run", AsyncMock(return_value=None)),
        pytest.raises(sc.APIConnectionError),
    ):
        await client.async_get_firmware_info()


@pytest.mark.asyncio
async def test_async_install_firmware_update_is_not_supported():
    client = sc.GwellIPCamClient(hass=object(), host="192.168.0.66", port=51880, password_hash="888888", entry_id="e")  # ty: ignore[invalid-argument-type]
    with pytest.raises(sc.APIError, match="not supported"):
        await client.async_install_firmware_update()


@pytest.mark.asyncio
async def test_async_ptz_delegates_to_rtsp_session():
    client = sc.GwellIPCamClient(hass=object(), host="192.168.0.66", port=51880, password_hash="888888", entry_id="e")  # ty: ignore[invalid-argument-type]
    with patch.object(client.rtsp_session, "ptz", AsyncMock()) as ptz:
        await client.async_ptz("up", steps=3, step_delay_ms=100)
    ptz.assert_called_once_with("up", steps=3, step_delay_ms=100)


@pytest.mark.asyncio
async def test_async_start_stop_streaming_delegates_to_session_and_proxy():
    client = sc.GwellIPCamClient(hass=object(), host="192.168.0.66", port=51880, password_hash="888888", entry_id="e")  # ty: ignore[invalid-argument-type]
    with (
        patch.object(client.rtsp_session, "start", AsyncMock()) as session_start,
        patch.object(client.rtsp_session, "stop", AsyncMock()) as session_stop,
        patch.object(client._rtsp_proxy, "start", AsyncMock()) as proxy_start,
        patch.object(client._rtsp_proxy, "stop", AsyncMock()) as proxy_stop,
    ):
        await client.async_start_streaming()
        await client.async_stop_streaming()
    session_start.assert_called_once()
    proxy_start.assert_called_once()
    proxy_stop.assert_called_once()
    session_stop.assert_called_once()


@pytest.mark.asyncio
async def test_async_discover_delegates_to_discover_function():
    with patch("custom_components.gwell_ipcam.api._discover", return_value=[]) as discover:
        result = await sc.GwellIPCamClient.async_discover(_FakeHass(), timeout_s=1.0)  # ty: ignore[invalid-argument-type]
    assert result == []
    discover.assert_called_once_with(timeout=1.0)


@pytest.mark.asyncio
async def test_async_discover_one_returns_first_match_or_none():
    camera = sc.DiscoveredCamera(host="192.168.0.66", port=51880, contact_id="1283250", name="IPCam-1283250")
    with patch("custom_components.gwell_ipcam.api._discover", return_value=[camera]):
        assert await sc.GwellIPCamClient.async_discover_one(_FakeHass(), "192.168.0.66") == camera  # ty: ignore[invalid-argument-type]
    with patch("custom_components.gwell_ipcam.api._discover", return_value=[]):
        assert await sc.GwellIPCamClient.async_discover_one(_FakeHass(), "192.168.0.66") is None  # ty: ignore[invalid-argument-type]


@pytest.mark.asyncio
async def test_async_get_identity_raises_when_not_discoverable():
    client = sc.GwellIPCamClient(  # ty: ignore[invalid-argument-type]
        hass=_FakeHass(),
        host="192.168.0.66",
        port=51880,
        password_hash="888888",
        entry_id="e",
    )
    with (
        patch.object(sc.GwellIPCamClient, "async_discover_one", AsyncMock(return_value=None)),
        pytest.raises(sc.APIConnectionError),
    ):
        await client.async_get_identity()


@pytest.mark.asyncio
async def test_async_get_identity_raises_auth_error_when_unauthenticated():
    client = sc.GwellIPCamClient(  # ty: ignore[invalid-argument-type]
        hass=_FakeHass(),
        host="192.168.0.66",
        port=51880,
        password_hash="888888",
        entry_id="e",
    )
    camera = sc.DiscoveredCamera(host="192.168.0.66", port=51880, contact_id="1283250", name="IPCam-1283250")
    with (
        patch.object(sc.GwellIPCamClient, "async_discover_one", AsyncMock(return_value=camera)),
        patch.object(client, "_run", AsyncMock(return_value=None)),
        pytest.raises(sc.APIAuthError),
    ):
        await client.async_get_identity()


@pytest.mark.asyncio
async def test_async_get_identity_builds_camera_identity():
    client = sc.GwellIPCamClient(  # ty: ignore[invalid-argument-type]
        hass=_FakeHass(),
        host="192.168.0.66",
        port=51880,
        password_hash="888888",
        entry_id="e",
    )
    camera = sc.DiscoveredCamera(host="192.168.0.66", port=51880, contact_id="1283250", name="IPCam-1283250")
    with (
        patch.object(sc.GwellIPCamClient, "async_discover_one", AsyncMock(return_value=camera)),
        patch.object(client, "_run", AsyncMock(return_value={"device_version": "21.0.0.30"})),
    ):
        identity = await client.async_get_identity()
    assert identity == sc.CameraIdentity(
        contact_id="1283250", name="IPCam-1283250", model=sc._DEFAULT_MODEL_NAME, firmware_version="21.0.0.30"
    )


@pytest.mark.asyncio
async def test_async_talk_opens_and_closes_a_talk_session():
    sent_pcm = b"\x00\x01" * 160
    with patch("custom_components.gwell_ipcam.api.TalkSession") as talk_session_cls:
        session = talk_session_cls.return_value
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)
        session.send_pcm16 = AsyncMock()
        client = sc.GwellIPCamClient(  # ty: ignore[invalid-argument-type]
            hass=object(),
            host="192.168.0.66",
            port=51880,
            password_hash="888888",
            entry_id="e",
        )
        await client.async_talk(sent_pcm)
    session.send_pcm16.assert_called_once_with(sent_pcm)


# -- _run_blocking: every raw exception gets mapped to our own hierarchy --------


@pytest.mark.asyncio
async def test_run_blocking_wraps_oserror_as_connection_error():
    def fn():
        raise OSError("Name or service not known")

    with pytest.raises(sc.APIConnectionError, match="Name or service not known"):
        await sc._run_blocking(_FakeHass(), fn)  # ty: ignore[invalid-argument-type]


@pytest.mark.asyncio
async def test_run_blocking_wraps_other_exceptions_as_api_error():
    def fn():
        raise TypeError("'float' object cannot be interpreted as an integer")

    with pytest.raises(sc.APIError, match="float"):
        await sc._run_blocking(_FakeHass(), fn)  # ty: ignore[invalid-argument-type]


@pytest.mark.asyncio
async def test_run_blocking_passes_api_errors_through_unchanged():
    def fn():
        raise sc.APIAuthError("nope")

    with pytest.raises(sc.APIAuthError, match="nope"):
        await sc._run_blocking(_FakeHass(), fn)  # ty: ignore[invalid-argument-type]
