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


# -- entry_password / weak-password reroll -----------------------------------


def test_entry_password_numeric_pin_used_as_is():
    assert sc.entry_password("888888") == 888888


def test_entry_password_leading_zero_is_hashed_not_used_as_pin():
    assert sc.entry_password("012345") != 12345


def test_entry_password_non_numeric_is_hashed():
    value = sc.entry_password("camtest12")
    assert isinstance(value, int)
    assert 0 <= value < 999999999


def test_entry_password_deterministic():
    assert sc.entry_password("camtest12") == sc.entry_password("camtest12")


def test_hash_password_round_trips_through_entry_password():
    """A stored hash must survive being fed back through entry_password() unchanged."""
    hashed = sc.GwellIPCamClient.hash_password("camtest12")
    assert sc.entry_password(hashed) == int(hashed)


def test_is_weak_password_int_detects_runs_and_repeats():
    assert sc._is_weak_password_int(123456)
    assert sc._is_weak_password_int(555555)
    assert not sc._is_weak_password_int(384756)


# -- discovery -----------------------------------------------------------------


def test_discover_parses_search_reply():
    reply = bytearray(96)
    struct.pack_into(">I", reply, 0, 2)
    struct.pack_into(">I", reply, 16, 1283250)
    fake = FakeSocket([bytes(reply)])
    with patch("custom_components.gwell_ipcam.api.socket.socket", return_value=fake):
        found = sc._discover(broadcast_ip="192.168.0.66", timeout=0.05)
    assert len(found) == 1
    assert found[0].contact_id == "1283250"
    assert found[0].host == "192.168.0.66"
    assert found[0].name == "IPCam-1283250"


def test_discover_ignores_non_reply_packets():
    junk = bytes(96)  # op=0, not SEARCH_REPLY(2)
    fake = FakeSocket([junk])
    with patch("custom_components.gwell_ipcam.api.socket.socket", return_value=fake):
        found = sc._discover(broadcast_ip="192.168.0.66", timeout=0.05)
    assert found == []


# -- settings read/write --------------------------------------------------------


def test_get_settings_parses_dump_and_filters_noise():
    payload = bytearray(4 + 3 * 8)
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
    assert capacity is not None
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
    assert info is not None
    assert info["device_version"] == "21.0.0.30"


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
    assert len(entries) == 1
    assert entries[0].timestamp == datetime(2026, 7, 13, 8, 15, 48)
    assert entries[0].tag == "M"
    assert entries[0].duration_s is None


def test_get_rec_files_parses_durations_when_flagged():
    entry = struct.pack("<H", 2026) + bytes([7, 13, 8, 15, 48]) + b"A"
    payload = bytes([4, 1, 0, 1]) + entry + struct.pack("<H", 120)
    resp = bytes([0x60]) + bytes(11) + payload
    client = make_client(responses=[resp])
    entries = client.get_rec_files(datetime(2026, 7, 1), datetime(2026, 7, 14), timeout=0.05)
    assert entries[0].duration_s == 120
    assert entries[0].tag == "A"


def test_get_rec_files_excludes_settings_dump_lookalikes():
    settings_lookalike = bytes([0x60]) + bytes(11) + bytes([0x02, 0x01]) + bytes(200)
    client = make_client(responses=[settings_lookalike])
    entries = client.get_rec_files(datetime(2026, 7, 1), datetime(2026, 7, 14), timeout=0.05)
    assert entries == []


# -- GwellIPCamClient: quick-record toggle state machine ------------------------


@pytest.mark.asyncio
async def test_toggle_quick_record_saves_mode_then_restores_it():
    client = sc.GwellIPCamClient(hass=object(), host="192.168.0.66", port=51880, password_hash="888888")  # ty: ignore[invalid-argument-type]
    with (
        patch.object(client, "async_get_settings", AsyncMock(return_value={sc.SETTING_RECORD_TYPE: 1})),
        patch.object(client, "async_set_setting", AsyncMock()) as set_setting,
        patch.object(client, "async_set_recording_state", AsyncMock()) as set_recording,
    ):
        started = await client.async_toggle_quick_record()
        assert started is True
        set_setting.assert_called_once_with(sc.SETTING_RECORD_TYPE, sc.RECORD_TYPE_MANUAL)
        set_recording.assert_called_once_with(enabled=True)

        set_setting.reset_mock()
        set_recording.reset_mock()
        stopped = await client.async_toggle_quick_record()
        assert stopped is False
        set_recording.assert_called_once_with(enabled=False)
        set_setting.assert_called_once_with(sc.SETTING_RECORD_TYPE, 1)


# -- StorageState / Recording mapping --------------------------------------------


def test_to_recording_maps_fields():
    entry = sc._RecFileEntry(timestamp=datetime(2026, 7, 13, 8, 15, 48), disc=0, tag="A", duration_s=120)
    recording = sc._to_recording(entry)
    assert recording.recording_id == "0-20260713081548"
    assert recording.motion_triggered is True
    assert recording.duration == timedelta(seconds=120)


def test_to_recording_missing_duration_defaults_to_zero():
    entry = sc._RecFileEntry(timestamp=datetime(2026, 7, 13, 8, 15, 48), disc=1, tag="M", duration_s=None)
    recording = sc._to_recording(entry)
    assert recording.duration == timedelta(0)
    assert recording.motion_triggered is False
