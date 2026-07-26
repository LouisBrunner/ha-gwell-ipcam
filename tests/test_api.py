"""Wire-format regression tests for custom_components/gwell_ipcam/api.py; plain pytest, no HA test harness needed."""

from __future__ import annotations

import asyncio
import struct
from datetime import datetime, timedelta
from datetime import time as dtime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.gwell_ipcam import api as sc


class FakeSocket:
    """Minimal socket stand-in for `_discover` (still a plain blocking function)."""

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
        return self.responses.pop(0), ("192.0.2.10", 51880)

    def close(self) -> None:
        return


class FakeWireSession(sc._WireSession):
    """`send()` immediately replays queued replies through `datagram_received`, as if the camera answered instantly."""

    def __init__(self, replies: list[bytes] | None = None) -> None:
        super().__init__("192.0.2.10")
        self.sent: list[bytes] = []
        self._pending_replies = list(replies or [])

    def send(self, packet: bytes, uid: str) -> None:  # noqa: ARG002
        self.sent.append(packet)
        for reply in self._pending_replies:
            self.datagram_received(reply, ("192.0.2.10", 51880))
        self._pending_replies = []


def _reply(payload: bytes) -> bytes:
    """Build a well-formed `0x60` reply: 12-byte header with a correctly declared length, then `payload`."""
    return bytes([0x60]) + bytes(7) + struct.pack("<I", len(payload)) + payload


def make_wire(replies: list[bytes] | None = None, dst_id: int = 0x42, password_int: int = 888888) -> sc._Wire:
    return sc._Wire(FakeWireSession(replies), dst_id, password_int)


def sent(wire: sc._Wire) -> list[bytes]:
    return wire._Wire__session.sent  # ty: ignore[unresolved-attribute]


def make_wire_mock(**method_returns: object) -> MagicMock:
    """Build a mock `_Wire` whose named async methods return the given values; use with `_get_wire`."""
    wire = MagicMock()
    for name, value in method_returns.items():
        setattr(wire, name, AsyncMock(return_value=value))
    return wire


def make_client(hass: object = None) -> sc.GwellIPCamClient:
    return sc.GwellIPCamClient(  # ty: ignore[invalid-argument-type]
        hass=hass or object(), host="192.0.2.10", port=51880, password_hash="888888", entry_id="e"
    )


class _FakeHass:
    async def async_add_executor_job(self, fn):
        return fn()


# -- entry_password / weak-password reroll ------


@pytest.mark.parametrize(
    ("password", "expected"),
    [
        ("888888", 888888),  # short numeric PIN: used as-is, no hashing
        ("012345", 493320785),  # leading zero disqualifies it as a PIN -> hashed instead
        ("camtest12", 636734832),  # non-numeric password -> hashed
    ],
)
def test_entry_password(password, expected):
    assert sc.entry_password(password) == expected


@pytest.mark.parametrize(
    ("flipped", "direction", "expected"),
    [
        (0, "up", "up"),
        (0, "down", "down"),
        (0, "left", "left"),
        (0, "right", "right"),
        (1, "up", "up"),
        (1, "down", "down"),
        (1, "left", "right"),
        (1, "right", "left"),
    ],
)
def test_map_ptz_direction(flipped, direction, expected):
    settings = {sc.SETTING_IMAGE_FLIP: flipped}
    assert sc.map_ptz_direction(direction, settings) == expected


def test_map_ptz_direction_defaults_to_not_flipped_when_setting_missing():
    assert sc.map_ptz_direction("left", {}) == "left"


def test_hash_password_round_trips_through_entry_password():
    """A stored hash must survive being fed back through entry_password() unchanged."""
    hashed = sc.GwellIPCamClient.hash_password("camtest12")
    assert hashed == "636734832"
    assert sc.entry_password(hashed) == 636734832


def test_resolve_ipv4_passes_through_a_dotted_quad():
    assert sc._resolve_ipv4("192.0.2.10") == "192.0.2.10"


def test_resolve_ipv4_resolves_a_hostname_via_gethostbyname():
    with patch("custom_components.gwell_ipcam.api.socket.gethostbyname", return_value="192.0.2.20") as gethostbyname:
        assert sc._resolve_ipv4("camera.local") == "192.0.2.20"
    gethostbyname.assert_called_once_with("camera.local")


def test_client_coerces_float_port_to_int():
    client = sc.GwellIPCamClient(  # ty: ignore[invalid-argument-type]
        hass=object(), host="192.0.2.10", port=51880.0, password_hash="888888", entry_id="e"
    )
    assert client._GwellIPCamClient__port == 51880
    assert isinstance(client._GwellIPCamClient__port, int)


# -- record_plan_time (settingType 5) encoding ------


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


# -- _WireSession: packet integrity + msgid dispatch -----------------------------


def test_packet_is_intact_accepts_a_matching_declared_length():
    payload = bytes(20)
    packet = bytes(8) + struct.pack("<I", 20) + payload  # header bytes[8:12] declare 20, and it is
    assert sc._packet_is_intact(packet) is True


def test_packet_is_intact_rejects_a_length_mismatch():
    payload = bytes(20)
    packet = bytes(8) + struct.pack("<I", 30) + payload  # declares 30 but only 20 bytes follow
    assert sc._packet_is_intact(packet) is False


def test_packet_is_intact_accepts_the_short_ack_format():
    assert sc._packet_is_intact(bytes([0x61, 0x0B, 0x6D, 0x42]) + struct.pack("<H", 1234)) is True


def test_packet_is_intact_accepts_a_zero_padded_short_ack():
    """The camera pads short acks past 12 bytes with zeros; the length field only applies to full replies."""
    ack = bytes([0x61, 0x0B, 0x6D, 0x42]) + struct.pack("<H", 1234)
    padded = ack + bytes(20)
    assert sc._packet_is_intact(padded) is True


def test_packet_is_intact_rejects_a_truncated_non_short_ack_packet():
    """Only the true short-ack shape (0x61) is exempt from the length check -- a short 0x60 packet is corrupt."""
    assert sc._packet_is_intact(bytes([0x60, 0, 0, 0, 0])) is False


def test_packet_is_intact_rejects_empty_data():
    assert sc._packet_is_intact(b"") is False


def test_log_hex_redact_password_hides_the_password_block_but_not_the_rest():
    """The DES key is fixed and public, so the password block must never appear in debug logs."""
    header = bytes(range(12))
    password_block = bytes([0xAA]) * 8
    tail = bytes([0xBB]) * 4
    packet = header + password_block + tail
    logged = sc._log_hex_redact_password(packet)
    assert "aa" * 8 not in logged
    assert header.hex() in logged
    assert tail.hex() in logged


def test_log_hex_redact_password_falls_back_for_a_packet_too_short_to_have_one():
    short_packet = bytes(range(10))
    assert sc._log_hex_redact_password(short_packet) == sc._log_hex(short_packet)


@pytest.mark.asyncio
async def test_wire_session_drops_a_corrupted_packet_before_publishing():
    """A shape-matching reply that fails the length check must not update the cache -- it's not trustworthy."""
    session = sc._WireSession("192.0.2.10")
    payload = bytearray(4 + 8)
    payload[0:2] = bytes([0x02, 0x01])
    struct.pack_into("<H", payload, 2, 1)
    struct.pack_into("<II", payload, 4, 0, 1)
    padded_payload = bytes(payload) + bytes(201 - 12 - len(payload))

    header = bytes([0x60]) + bytes(7)
    corrupted = header + struct.pack("<I", 999) + padded_payload  # declares 999, wrong for this payload length
    session.datagram_received(corrupted, ("192.0.2.10", 51880))
    assert session.settings.seq == 0

    valid = _reply(padded_payload)
    session.datagram_received(valid, ("192.0.2.10", 51880))
    assert session.settings.seq == 1
    assert session.settings.latest is not None
    assert session.settings.latest.values == {0: 1}


@pytest.mark.asyncio
async def test_wire_session_short_ack_shape_never_falls_through_to_broadcast_dispatch():
    """A short-ack-shaped packet with no live waiter for its msgid must be dropped, not tested as a broadcast."""
    session = sc._WireSession("192.0.2.10")
    ack = bytes([0x61, 0x03, 0x6D, 0x42]) + struct.pack("<H", 12345) + bytes(20)
    session.datagram_received(ack, ("192.0.2.10", 51880))
    assert session.settings.seq == 0
    assert session.device_info.seq == 0


@pytest.mark.asyncio
async def test_wire_session_ignores_an_ack_with_a_mismatched_subcmd():
    """An ack whose subcmd doesn't match what we sent for this msgid must not resolve the waiter."""
    session = sc._WireSession("192.0.2.10")
    fut = session.begin_msgid(12345, subcmd=0x0B)
    wrong_subcmd_ack = bytes([0x61, 0x03, 0x6D, 0x42]) + struct.pack("<H", 12345)
    session.datagram_received(wrong_subcmd_ack, ("192.0.2.10", 51880))
    assert not fut.done()

    right_subcmd_ack = bytes([0x61, 0x0B, 0x6D, 0x42]) + struct.pack("<H", 12345)
    session.datagram_received(right_subcmd_ack, ("192.0.2.10", 51880))
    assert fut.done()


@pytest.mark.asyncio
async def test_wire_session_a_decode_exception_does_not_prevent_other_shapes_from_being_dispatched():
    """A malformed-but-intact packet that crashes one shape's decoder must not poison unrelated broadcast slots."""
    session = sc._WireSession("192.0.2.10")
    bad_payload = bytearray(4 + 8)
    bad_payload[0:2] = bytes([0x02, 0x01])
    struct.pack_into("<H", bad_payload, 2, 999)  # claims 999 entries, but the payload is nowhere near that long
    padded_bad_payload = bytes(bad_payload) + bytes(201 - 12 - len(bad_payload))
    session.datagram_received(_reply(padded_bad_payload), ("192.0.2.10", 51880))
    assert session.settings.seq == 0

    good = bytes([0xF1, 0, 3, 0, 0, 0])
    session.datagram_received(_reply(good), ("192.0.2.10", 51880))
    assert session.record_quality.seq == 1
    assert session.record_quality.latest == 3


@pytest.mark.asyncio
async def test_wire_session_a_value_error_from_an_invalid_date_does_not_poison_other_shapes():
    """An out-of-range month/day (ValueError, not struct.error) must be caught the same way as a bad struct decode."""
    session = sc._WireSession("192.0.2.10")
    invalid_month = bytes([0x0C, 0, 0, 0]) + struct.pack("<H", 2026) + bytes([0, 15, 10, 30])
    session.datagram_received(_reply(invalid_month), ("192.0.2.10", 51880))
    assert session.device_time.seq == 0

    good = bytes([0xF1, 0, 3, 0, 0, 0])
    session.datagram_received(_reply(good), ("192.0.2.10", 51880))
    assert session.record_quality.seq == 1
    assert session.record_quality.latest == 3


def test_alloc_msgid_skips_a_msgid_still_in_use():
    session = sc._WireSession("192.0.2.10")
    session._WireSession__next_msgid = sc._MSGID_MIN
    session._WireSession__by_msgid[sc._MSGID_MIN] = (0x0B, MagicMock())
    assert session.alloc_msgid() == sc._MSGID_MIN + 1


@pytest.mark.asyncio
async def test_broadcast_slot_after_seq_ignores_a_stale_publish_from_before_the_since_seq():
    slot = sc._BroadcastSlot()
    slot.publish("stale")
    since_seq = slot.seq
    result = await slot.wait_after(since_seq, timeout_s=0.01)
    assert result is None


@pytest.mark.asyncio
async def test_broadcast_slot_after_seq_returns_a_publish_newer_than_since_seq():
    slot = sc._BroadcastSlot()
    slot.publish("stale")
    since_seq = slot.seq
    slot.publish("fresh")
    result = await slot.wait_after(since_seq, timeout_s=0.01)
    assert result == (2, "fresh")


# -- discovery -----------------------------------------------------------------


def test_discover_parses_search_reply():
    reply = bytearray(96)
    struct.pack_into(">I", reply, 0, 2)
    struct.pack_into(">I", reply, 16, 9999999)
    fake = FakeSocket([bytes(reply)])
    with patch("custom_components.gwell_ipcam.api.socket.socket", return_value=fake):
        found = sc._discover(broadcast_ip="192.0.2.10", timeout=0.05)
    assert found == [sc.DiscoveredCamera(host="192.0.2.10", port=51880, contact_id="9999999", name="IPCam-9999999")]


def test_discover_ignores_non_reply_packets():
    junk = bytes(96)  # op=0, not SEARCH_REPLY(2)
    fake = FakeSocket([junk])
    with patch("custom_components.gwell_ipcam.api.socket.socket", return_value=fake):
        found = sc._discover(broadcast_ip="192.0.2.10", timeout=0.05)
    assert found == []


def test_discover_warns_on_malformed_reply_sharing_our_marker():
    malformed = bytearray(50)
    struct.pack_into(">I", malformed, 0, 2)
    fake = FakeSocket([bytes(malformed)])
    with (
        patch("custom_components.gwell_ipcam.api.socket.socket", return_value=fake),
        patch("custom_components.gwell_ipcam.api.LOGGER") as logger,
    ):
        found = sc._discover(broadcast_ip="192.0.2.10", timeout=0.05)
    assert found == []
    assert logger.warning.called


# -- settings read/write --------------------------------------------------------


@pytest.mark.asyncio
async def test_get_settings_parses_dump_and_filters_noise():
    payload = bytearray(4 + 3 * 8)
    payload[0:2] = bytes([0x02, 0x01])  # settings-dump response tag
    struct.pack_into("<H", payload, 2, 3)
    struct.pack_into("<II", payload, 4, 0, 1)  # remote_defence=1
    struct.pack_into("<II", payload, 12, 4, 1)  # remote_record=1
    struct.pack_into("<II", payload, 20, 10, 999)  # noise ID, must be filtered
    padded_payload = bytes(payload) + bytes(201 - 12 - len(payload))  # pad past the "len(data) > 200" gate
    wire = make_wire([_reply(padded_payload)])
    dump = await wire.get_settings("u")
    assert dump is not None
    assert dump.clean_values() == {0: 1, 4: 1}


@pytest.mark.asyncio
async def test_get_settings_ignores_other_large_0x60_responses():
    """A same-shape response with a different tag (e.g. recorded-file listing) must not be misread as a dump."""
    other_response_payload = bytes([0x04, 0x01]) + bytes(200)
    wire = make_wire([_reply(other_response_payload)])
    assert await wire.get_settings("u", timeout_s=0.01) is None


def _settings_dump_reply(setting_type: int, value: int) -> bytes:
    payload = bytearray(4 + 8)
    payload[0:2] = bytes([0x02, 0x01])
    struct.pack_into("<H", payload, 2, 1)
    struct.pack_into("<II", payload, 4, setting_type, value)
    return _reply(bytes(payload) + bytes(201 - 12 - len(payload)))


@pytest.mark.asyncio
async def test_get_settings_matching_ignores_a_dump_cached_before_the_write():
    """A dump already matching, but cached before this call was even made, must not be mistaken for confirmation."""
    session = FakeWireSession()
    session.datagram_received(
        _settings_dump_reply(sc.SETTING_REMOTE_RECORD, 1), ("192.0.2.10", 51880)
    )  # already satisfies the predicate below, but predates the write -- must not resolve the call
    wire = sc._Wire(session, dst_id=0x42, password_int=888888)

    dump = await wire.get_settings_matching("u", lambda d: d.values.get(sc.SETTING_REMOTE_RECORD) == 1, timeout_s=0.01)
    assert dump is None


@pytest.mark.asyncio
async def test_get_settings_matching_accepts_a_dump_published_after_the_write():
    session = FakeWireSession()
    session.datagram_received(_settings_dump_reply(sc.SETTING_REMOTE_RECORD, 1), ("192.0.2.10", 51880))
    session._pending_replies = [_settings_dump_reply(sc.SETTING_REMOTE_RECORD, 1)]
    wire = sc._Wire(session, dst_id=0x42, password_int=888888)

    dump = await wire.get_settings_matching("u", lambda d: d.values.get(sc.SETTING_REMOTE_RECORD) == 1, timeout_s=1.0)
    assert dump is not None
    assert dump.values[sc.SETTING_REMOTE_RECORD] == 1
    assert session.settings.seq == 2  # the pre-cached dump plus the one sent by this call, not just one


@pytest.mark.asyncio
async def test_set_setting_wire_format():
    wire = make_wire()
    await wire.set_setting(4, 1, "u")
    sent_bytes = sent(wire)[0]
    assert sent_bytes[0] == 0x60
    assert sent_bytes[1] == 0x0B
    payload = sent_bytes[12:]
    assert payload[8:12] == bytes.fromhex("01000100")
    setting_type, value = struct.unpack_from("<II", payload, 12)
    assert (setting_type, value) == (4, 1)


@pytest.mark.asyncio
async def test_set_setting_returns_without_waiting_for_ack():
    """No reply is queued for this wire, so a set_setting that waited for an ack would hang/timeout instead."""
    wire = make_wire()
    await asyncio.wait_for(wire.set_setting(4, 1, "u"), timeout=0.1)
    assert len(sent(wire)) == 1


# -- record quality: read (0xF0) and write (0xEF) are DIFFERENT commands --------


@pytest.mark.asyncio
async def test_get_record_quality_uses_read_tag_0xf0():
    wire = make_wire()
    await wire.get_record_quality("u", timeout_s=0.01)
    assert sent(wire)[0][12 + 8] == 0xF0


@pytest.mark.asyncio
async def test_get_record_quality_parses_response():
    resp = _reply(bytes([0xF1, 0, 3, 0, 0, 0]))
    wire = make_wire([resp])
    assert await wire.get_record_quality("u") == 3


@pytest.mark.asyncio
async def test_set_record_quality_uses_write_tag_0xef_not_0xf0():
    wire = make_wire()
    await wire.set_record_quality(3, "u")
    sent_bytes = sent(wire)[0]
    assert sent_bytes[12 + 8] == 0xEF
    assert sent_bytes[12 + 8 + 2] == 3


# -- SD card capacity: exact offsets, including SDcardID at offset 4 -----------


@pytest.mark.asyncio
async def test_get_sd_card_capacity_offsets():
    payload = bytearray(33 - 12)
    payload[0] = 0x50
    payload[4] = 0x10
    struct.pack_into("<I", payload, 8, 3691)
    struct.pack_into("<I", payload, 16, 2894)
    resp = _reply(bytes(payload))
    wire = make_wire([resp])
    capacity = await wire.get_sd_card_capacity("u")
    assert capacity == (3691 * 16, 2894 * 16, 0x10)


# -- Format SD card: wire format + result-code decoding -------------------------


@pytest.mark.asyncio
async def test_format_sd_card_wire_format():
    wire = make_wire()
    await wire.format_sd_card(0x10, "u", timeout_s=0.01)
    body = sent(wire)[0][12 + 8 :]
    assert body[0] == 0x51
    assert body[1:4] == bytes(3)
    assert body[4] == 0x10


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("code", "expected"), [(80, "success"), (81, "fail"), (82, "no_sd"), (103, "must_stop_record")]
)
async def test_format_sd_card_decodes_result_code(code, expected):
    resp = _reply(bytes([0x51, code, 0x00, 0x00, 0x10]))
    wire = make_wire([resp])
    assert await wire.format_sd_card(0x10, "u") == expected


@pytest.mark.asyncio
async def test_format_sd_card_no_response():
    wire = make_wire()
    assert await wire.format_sd_card(0x10, "u", timeout_s=0.01) == "no_response"


@pytest.mark.asyncio
async def test_send_and_wait_format_serializes_concurrent_calls():
    """A second concurrent format request must not even send until the first one has resolved -- no cross-delivery."""
    session = sc._WireSession("192.0.2.10")
    sends: list[int] = []

    task1 = asyncio.ensure_future(session.send_and_wait_format(lambda: sends.append(1), timeout_s=1.0))
    await asyncio.sleep(0.01)
    task2 = asyncio.ensure_future(session.send_and_wait_format(lambda: sends.append(2), timeout_s=1.0))
    await asyncio.sleep(0.01)
    assert sends == [1]  # task2 is blocked on the lock, hasn't sent yet

    assert session._WireSession__format_fut is not None
    session._WireSession__format_fut.set_result(_reply(bytes([0x51, 80, 0x00, 0x00, 0x10])))
    await task1
    await asyncio.sleep(0.01)
    assert sends == [1, 2]

    assert session._WireSession__format_fut is not None
    session._WireSession__format_fut.set_result(_reply(bytes([0x51, 81, 0x00, 0x00, 0x20])))
    await task2


# -- device time -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_device_time_request_tag():
    wire = make_wire()
    await wire.get_device_time("u", timeout_s=0.01)
    assert sent(wire)[0][12 + 8] == 0x0A


@pytest.mark.asyncio
async def test_get_device_time_parses_response():
    payload = bytes([0x0C, 0, 0, 0]) + struct.pack("<H", 2026) + bytes([7, 13, 8, 15])
    resp = _reply(payload)
    wire = make_wire([resp])
    dt = await wire.get_device_time("u")
    assert dt == datetime(2026, 7, 13, 8, 15)


@pytest.mark.asyncio
async def test_set_device_time_wire_format():
    wire = make_wire()
    await wire.set_device_time(datetime(2026, 7, 13, 8, 15), "u", timeout_s=0.01)
    body = sent(wire)[0][12 + 8 :]
    assert body[0] == 0x0B
    year = struct.unpack_from("<H", body, 4)[0]
    assert (year, body[6], body[7], body[8], body[9]) == (2026, 7, 13, 8, 15)


# -- device info / firmware update check -----------------------------------------


@pytest.mark.asyncio
async def test_get_device_info_request_tag():
    wire = make_wire()
    await wire.get_device_info("u", timeout_s=0.01)
    assert sent(wire)[0][12 + 8] == 0x27


@pytest.mark.asyncio
async def test_get_device_info_reverse_byte_version():
    payload = bytes([0x28, 0, 0, 0]) + bytes([30, 0, 0, 21]) + bytes(12)
    resp = _reply(payload + bytes(48 - 12 - len(payload)))
    wire = make_wire([resp])
    info = await wire.get_device_info("u")
    assert info == {"device_version": "21.0.0.30"}


@pytest.mark.asyncio
async def test_get_device_update_check_reverse_byte_versions():
    payload = bytes([0x1E, 1, 0, 0]) + bytes([30, 0, 0, 21]) + bytes([31, 0, 0, 21])
    resp = _reply(payload)
    wire = make_wire([resp])
    info = await wire.get_device_update_check("u")
    assert info == {"result": 1, "cur_version": "21.0.0.30", "upg_version": "21.0.0.31"}


# -- recorded file listing -------------------------------------------------------


@pytest.mark.asyncio
async def test_get_rec_files_parses_entries_without_duration():
    entry = struct.pack("<H", 2026) + bytes([7, 13, 8, 15, 48]) + b"M"
    payload = bytes([4, 0, 0, 1]) + entry
    resp = _reply(payload)
    wire = make_wire([resp])
    entries = await wire.get_rec_files(datetime(2026, 7, 1), datetime(2026, 7, 14), "u")
    assert entries == [sc._RecFileEntry(timestamp=datetime(2026, 7, 13, 8, 15, 48), disc=0, tag="M", duration_s=None)]


@pytest.mark.asyncio
async def test_get_rec_files_parses_durations_when_flagged():
    entry = struct.pack("<H", 2026) + bytes([7, 13, 8, 15, 48]) + b"A"
    payload = bytes([4, 1, 0, 1]) + entry + struct.pack("<H", 120)
    resp = _reply(payload)
    wire = make_wire([resp])
    entries = await wire.get_rec_files(datetime(2026, 7, 1), datetime(2026, 7, 14), "u")
    assert entries == [sc._RecFileEntry(timestamp=datetime(2026, 7, 13, 8, 15, 48), disc=0, tag="A", duration_s=120)]


@pytest.mark.asyncio
async def test_get_rec_files_excludes_settings_dump_lookalikes():
    """With no other packet arriving, filtering the lookalike out leaves no real reply -- a bad read, not 0."""
    settings_lookalike = _reply(bytes([0x02, 0x01]) + bytes(200))
    wire = make_wire([settings_lookalike])
    with pytest.raises(OSError, match="no complete reply"):
        await wire.get_rec_files(datetime(2026, 7, 1), datetime(2026, 7, 14), "u", timeout_s=0.01)


@pytest.mark.asyncio
async def test_get_rec_files_raises_on_truncated_reply():
    """Count says 2 entries but the payload is only long enough for 1 -- must not silently under-return."""
    entry = struct.pack("<H", 2026) + bytes([7, 13, 8, 15, 48]) + b"M"
    payload = bytes([4, 0, 0, 2]) + entry
    resp = _reply(payload)
    wire = make_wire([resp])
    with pytest.raises(OSError, match="no complete reply"):
        await wire.get_rec_files(datetime(2026, 7, 1), datetime(2026, 7, 14), "u", timeout_s=0.01)


@pytest.mark.asyncio
async def test_get_rec_files_raises_on_no_response():
    wire = make_wire()
    with pytest.raises(OSError, match="no complete reply"):
        await wire.get_rec_files(datetime(2026, 7, 1), datetime(2026, 7, 14), "u", timeout_s=0.01)


@pytest.mark.asyncio
async def test_get_rec_files_raises_api_error_on_an_entry_with_an_invalid_date():
    """A structurally complete reply with an out-of-range month must surface as APIError, not a bare ValueError."""
    invalid_month_entry = struct.pack("<H", 2026) + bytes([0x10, 0, 0, 0, 0]) + b"M"
    payload = bytes([4, 0, 0, 1]) + invalid_month_entry
    wire = make_wire([_reply(payload)])
    with pytest.raises(sc.APIError, match="malformed reply"):
        await wire.get_rec_files(datetime(2026, 7, 1), datetime(2026, 7, 14), "u")


@pytest.mark.asyncio
async def test_get_rec_files_skips_a_truncated_reply_ahead_of_a_complete_one():
    """Not msgid-correlated -- the predicate itself must skip an incomplete match rather than accepting it."""
    truncated = _reply(bytes([4, 0, 0, 2]) + (struct.pack("<H", 2026) + bytes([7, 1, 0, 0, 0, 77])))
    entry = struct.pack("<H", 2026) + bytes([7, 2, 0, 0, 0]) + b"M"
    complete = _reply(bytes([4, 0, 0, 1]) + entry)
    wire = make_wire([truncated, complete])
    entries = await wire.get_rec_files(datetime(2026, 7, 1), datetime(2026, 7, 14), "u")
    assert entries[0].timestamp.day == 2


@pytest.mark.asyncio
async def test_send_and_wait_rec_files_serializes_concurrent_calls():
    """Two concurrent get_rec_files calls for different date ranges must queue, not cross-deliver."""
    session = sc._WireSession("192.0.2.10")
    sends: list[int] = []

    task1 = asyncio.ensure_future(session.send_and_wait_rec_files(lambda: sends.append(1), timeout_s=1.0))
    await asyncio.sleep(0.01)
    task2 = asyncio.ensure_future(session.send_and_wait_rec_files(lambda: sends.append(2), timeout_s=1.0))
    await asyncio.sleep(0.01)
    assert sends == [1]  # task2 is blocked on the lock, hasn't sent yet

    first_entry = struct.pack("<H", 2026) + bytes([7, 1, 0, 0, 0]) + b"M"
    assert session._WireSession__rec_files_fut is not None
    session._WireSession__rec_files_fut.set_result(_reply(bytes([4, 0, 0, 1]) + first_entry))
    first_data = await task1
    await asyncio.sleep(0.01)
    assert sends == [1, 2]

    second_entry = struct.pack("<H", 2026) + bytes([7, 2, 0, 0, 0]) + b"A"
    assert session._WireSession__rec_files_fut is not None
    session._WireSession__rec_files_fut.set_result(_reply(bytes([4, 0, 0, 1]) + second_entry))
    second_data = await task2

    assert first_data is not None
    assert second_data is not None
    assert sc._decode_rec_files(first_data)[0].timestamp.day == 1
    assert sc._decode_rec_files(second_data)[0].timestamp.day == 2


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


class _FakeQuickRecordStore:
    """Stands in for the persisted `Store`, so tests don't need real HA storage plumbing."""

    def __init__(self) -> None:
        self.saved: dict[str, int | None] | None = None

    async def async_load(self) -> dict[str, int | None] | None:
        return self.saved

    async def async_save(self, data: dict[str, int | None]) -> None:
        self.saved = data


@pytest.mark.asyncio
async def test_toggle_quick_record_starts_then_stops_and_restores_prior_mode():
    """Also covers that the *original* record mode (not a hardcoded one) is what gets restored."""
    client = make_client()
    fake_store = _FakeQuickRecordStore()
    fresh_settings = {sc.SETTING_RECORD_TYPE: 0, sc.SETTING_REMOTE_RECORD: 1}
    with (
        patch.object(client, "_GwellIPCamClient__get_quick_record_store", return_value=fake_store),
        patch.object(client, "async_get_settings", AsyncMock(return_value={sc.SETTING_RECORD_TYPE: 1})),
        patch.object(client, "async_set_setting", AsyncMock(return_value=fresh_settings)) as set_setting,
        patch.object(client, "async_set_recording_state", AsyncMock(return_value=fresh_settings)) as set_recording,
    ):
        assert client.quick_record_active is False
        started, fresh = await client.async_toggle_quick_record()
        assert started is True
        assert fresh == fresh_settings
        assert client.quick_record_active is True
        set_setting.assert_called_once_with(sc.SETTING_RECORD_TYPE, sc.RECORD_TYPE_MANUAL, uid=None)
        set_recording.assert_called_once_with(enabled=True, uid=None)

        set_setting.reset_mock()
        set_recording.reset_mock()
        stopped, fresh = await client.async_toggle_quick_record()
        assert stopped is False
        assert fresh == fresh_settings
        assert client.quick_record_active is False
        set_recording.assert_called_once_with(enabled=False, uid=None)
        set_setting.assert_called_once_with(sc.SETTING_RECORD_TYPE, 1, uid=None)  # the mode saved before starting


@pytest.mark.asyncio
async def test_quick_record_state_survives_a_reload_via_the_store():
    """The whole point of persisting to a Store: a fresh client instance picks up the in-progress state."""
    fake_store = _FakeQuickRecordStore()
    fake_store.saved = {"saved_record_type": 2}
    client = make_client()
    with patch.object(client, "_GwellIPCamClient__get_quick_record_store", return_value=fake_store):
        assert client.quick_record_active is False
        await client.async_load_quick_record_state()
        assert client.quick_record_active is True


# -- GwellIPCamClient: thin async wrappers around _get_wire ---------------------


@pytest.mark.asyncio
async def test_async_get_camera_time_localizes_naive_result():
    client = make_client()
    wire = make_wire_mock(get_device_time=datetime(2026, 7, 13, 8, 15))
    with patch.object(client, "_GwellIPCamClient__get_wire", AsyncMock(return_value=wire)):
        result = await client.async_get_camera_time()
    assert result == datetime(2026, 7, 13, 8, 15, tzinfo=sc.dt_util.DEFAULT_TIME_ZONE)


@pytest.mark.asyncio
async def test_async_get_camera_time_raises_on_no_response():
    client = make_client()
    wire = make_wire_mock(get_device_time=None)
    with (
        patch.object(client, "_GwellIPCamClient__get_wire", AsyncMock(return_value=wire)),
        pytest.raises(sc.APIConnectionError),
    ):
        await client.async_get_camera_time()


@pytest.mark.asyncio
async def test_async_sync_time_pushes_current_local_time():
    client = make_client()
    drifted = datetime(2000, 1, 1, tzinfo=sc.dt_util.DEFAULT_TIME_ZONE)
    fresh = sc.dt_util.now()
    wire = make_wire_mock(set_device_time=True)
    with (
        patch.object(client, "async_get_camera_time", AsyncMock(side_effect=[drifted, fresh])),
        patch.object(client, "_GwellIPCamClient__get_wire", AsyncMock(return_value=wire)),
    ):
        result = await client.async_sync_time()
    wire.set_device_time.assert_called_once()
    assert result == fresh


@pytest.mark.asyncio
async def test_async_sync_time_raises_when_clock_did_not_change():
    """The write's own ack can't be trusted -- only a read-back showing reduced drift counts."""
    client = make_client()
    drifted = datetime(2000, 1, 1, tzinfo=sc.dt_util.DEFAULT_TIME_ZONE)
    wire = make_wire_mock(set_device_time=True)
    with (
        patch.object(client, "async_get_camera_time", AsyncMock(side_effect=[drifted, drifted])),
        patch.object(client, "_GwellIPCamClient__get_wire", AsyncMock(return_value=wire)),
        pytest.raises(sc.APIError, match="did not change"),
    ):
        await client.async_sync_time()


@pytest.mark.asyncio
async def test_async_get_storage_state_computes_used_from_total_minus_free():
    client = make_client()
    wire = make_wire_mock(get_sd_card_capacity=(3691 * 16, 2894 * 16, 0x10))
    with patch.object(client, "_GwellIPCamClient__get_wire", AsyncMock(return_value=wire)):
        result = await client.async_get_storage_state()
    assert result == sc.StorageState(used_mb=(3691 - 2894) * 16, total_mb=3691 * 16)


@pytest.mark.asyncio
async def test_async_get_storage_state_raises_on_no_response():
    client = make_client()
    wire = make_wire_mock(get_sd_card_capacity=None)
    with (
        patch.object(client, "_GwellIPCamClient__get_wire", AsyncMock(return_value=wire)),
        pytest.raises(sc.APIConnectionError),
    ):
        await client.async_get_storage_state()


@pytest.mark.asyncio
async def test_async_get_storage_state_raises_on_implausible_capacity():
    """Free > total can only come from a garbled/truncated reply -- must not report negative usage."""
    client = make_client()
    wire = make_wire_mock(get_sd_card_capacity=(100, 200, 0x10))
    with (
        patch.object(client, "_GwellIPCamClient__get_wire", AsyncMock(return_value=wire)),
        pytest.raises(sc.APIError, match="implausible"),
    ):
        await client.async_get_storage_state()


@pytest.mark.asyncio
async def test_async_get_settings_filters_noise_via_clean_values():
    client = make_client()
    dump = sc._SettingsDump(values={0: 1, 10: 999})  # 10 is a noise ID
    wire = make_wire_mock(get_settings=dump)
    with patch.object(client, "_GwellIPCamClient__get_wire", AsyncMock(return_value=wire)):
        result = await client.async_get_settings()
    assert result == {0: 1}


@pytest.mark.asyncio
async def test_async_get_settings_raises_on_no_response():
    client = make_client()
    wire = make_wire_mock(get_settings=None)
    with (
        patch.object(client, "_GwellIPCamClient__get_wire", AsyncMock(return_value=wire)),
        pytest.raises(sc.APIConnectionError),
    ):
        await client.async_get_settings()


@pytest.mark.asyncio
async def test_async_set_setting_succeeds_once_readback_confirms_the_value():
    client = make_client()
    dump = sc._SettingsDump(values={sc.SETTING_REMOTE_RECORD: 1})
    wire = make_wire_mock(set_setting=True, get_settings_matching=dump)
    with patch.object(client, "_GwellIPCamClient__get_wire", AsyncMock(return_value=wire)):
        result = await client.async_set_setting(sc.SETTING_REMOTE_RECORD, 1)
    assert result == {sc.SETTING_REMOTE_RECORD: 1}


@pytest.mark.asyncio
async def test_async_set_setting_raises_when_readback_never_confirms():
    """A write ack alone is never trusted -- only a read-back match counts, and it can't be faked."""
    client = make_client()
    wire = make_wire_mock(set_setting=True, get_settings_matching=None)
    with (
        patch.object(client, "_GwellIPCamClient__get_wire", AsyncMock(return_value=wire)),
        pytest.raises(sc.APIError, match="did not change"),
    ):
        await client.async_set_setting(sc.SETTING_REMOTE_RECORD, 1)


@pytest.mark.asyncio
async def test_async_set_recording_state_writes_remote_record_setting():
    client = make_client()
    with patch.object(client, "async_set_setting", AsyncMock()) as set_setting:
        await client.async_set_recording_state(enabled=True)
    set_setting.assert_called_once_with(sc.SETTING_REMOTE_RECORD, 1, uid=None)


@pytest.mark.asyncio
async def test_async_get_record_quality_delegates_to_wire():
    client = make_client()
    wire = make_wire_mock(get_record_quality=3)
    with patch.object(client, "_GwellIPCamClient__get_wire", AsyncMock(return_value=wire)):
        assert await client.async_get_record_quality() == 3


@pytest.mark.asyncio
async def test_async_set_record_quality_succeeds_once_readback_confirms_the_value():
    client = make_client()
    wire = make_wire_mock(set_record_quality=None, get_record_quality_matching=3)
    with patch.object(client, "_GwellIPCamClient__get_wire", AsyncMock(return_value=wire)):
        result = await client.async_set_record_quality(3)
    assert result == 3


@pytest.mark.asyncio
async def test_async_set_record_quality_raises_when_readback_never_confirms():
    client = make_client()
    wire = make_wire_mock(set_record_quality=None, get_record_quality_matching=None)
    with (
        patch.object(client, "_GwellIPCamClient__get_wire", AsyncMock(return_value=wire)),
        pytest.raises(sc.APIError, match="did not change"),
    ):
        await client.async_set_record_quality(3)


@pytest.mark.asyncio
async def test_async_get_record_plan_decodes_from_settings():
    client = make_client()
    value = sc.encode_record_plan_time(dtime(8, 0), dtime(18, 30))
    with patch.object(client, "async_get_settings", AsyncMock(return_value={sc.SETTING_RECORD_PLAN_TIME: value})):
        assert await client.async_get_record_plan() == (dtime(8, 0), dtime(18, 30))


@pytest.mark.asyncio
async def test_async_get_record_plan_returns_none_when_setting_missing():
    client = make_client()
    with patch.object(client, "async_get_settings", AsyncMock(return_value={})):
        assert await client.async_get_record_plan() is None


@pytest.mark.asyncio
async def test_async_set_record_plan_writes_encoded_setting():
    client = make_client()
    with patch.object(client, "async_set_setting", AsyncMock()) as set_setting:
        await client.async_set_record_plan(dtime(8, 0), dtime(18, 30))
    set_setting.assert_called_once_with(
        sc.SETTING_RECORD_PLAN_TIME, sc.encode_record_plan_time(dtime(8, 0), dtime(18, 30)), uid=None
    )


@pytest.mark.asyncio
async def test_async_format_sd_card_raises_on_failure_result():
    client = make_client()
    wire = make_wire_mock(get_sd_card_capacity=(100, 50, 0x10), format_sd_card="fail")
    with (
        patch.object(client, "_GwellIPCamClient__get_wire", AsyncMock(return_value=wire)),
        pytest.raises(sc.APIError, match="fail"),
    ):
        await client.async_format_sd_card()


@pytest.mark.asyncio
async def test_async_format_sd_card_succeeds_silently():
    client = make_client()
    wire = make_wire_mock(get_sd_card_capacity=(100, 50, 0x10), format_sd_card="success")
    with patch.object(client, "_GwellIPCamClient__get_wire", AsyncMock(return_value=wire)):
        await client.async_format_sd_card()
    wire.get_sd_card_capacity.assert_called_once()
    assert wire.format_sd_card.call_args.args[0] == 0x10


@pytest.mark.asyncio
async def test_async_get_recordings_maps_entries():
    client = make_client()
    entry = sc._RecFileEntry(timestamp=datetime(2026, 7, 13, 8, 15, 48), disc=0, tag="A", duration_s=120)
    wire = make_wire_mock(get_rec_files=[entry])
    with patch.object(client, "_GwellIPCamClient__get_wire", AsyncMock(return_value=wire)):
        result = await client.async_get_recordings()
    assert result == [sc._to_recording(entry)]


@pytest.mark.asyncio
async def test_async_get_live_stream_url_uses_local_proxy_port():
    client = make_client()
    with patch.object(type(client._GwellIPCamClient__rtsp_proxy), "port", new=40000):
        url = await client.async_get_live_stream_url()
    assert url == f"rtsp://127.0.0.1:40000{sc.RTSP_PATH}"


@pytest.mark.asyncio
async def test_async_get_firmware_info_reports_update_available():
    client = make_client()
    info = {"result": 1, "cur_version": "21.0.0.30", "upg_version": "21.0.0.31"}
    wire = make_wire_mock(get_device_update_check=info)
    with patch.object(client, "_GwellIPCamClient__get_wire", AsyncMock(return_value=wire)):
        result = await client.async_get_firmware_info()
    assert result == sc.FirmwareInfo(latest_version="21.0.0.31", release_summary=None, release_url=None)


@pytest.mark.asyncio
async def test_async_get_firmware_info_reports_no_update():
    client = make_client()
    info = {"result": 53, "cur_version": "21.0.0.30", "upg_version": "0.0.0.0"}  # noqa: S104 -- a version string
    wire = make_wire_mock(get_device_update_check=info)
    with patch.object(client, "_GwellIPCamClient__get_wire", AsyncMock(return_value=wire)):
        result = await client.async_get_firmware_info()
    assert result == sc.FirmwareInfo(latest_version="21.0.0.30", release_summary=None, release_url=None)


@pytest.mark.asyncio
async def test_async_get_firmware_info_raises_on_no_response():
    client = make_client()
    wire = make_wire_mock(get_device_update_check=None)
    with (
        patch.object(client, "_GwellIPCamClient__get_wire", AsyncMock(return_value=wire)),
        pytest.raises(sc.APIConnectionError),
    ):
        await client.async_get_firmware_info()


@pytest.mark.asyncio
async def test_async_install_firmware_update_is_not_supported():
    client = make_client()
    with pytest.raises(sc.APIError, match="not supported"):
        await client.async_install_firmware_update()


@pytest.mark.asyncio
async def test_async_ptz_delegates_to_rtsp_session():
    client = make_client()
    with patch.object(client.rtsp_session, "ptz", AsyncMock()) as ptz:
        await client.async_ptz("up", steps=3, step_delay_ms=100)
    ptz.assert_called_once_with("up", steps=3, step_delay_ms=100)


@pytest.mark.asyncio
async def test_async_start_stop_streaming_delegates_to_session_and_proxy():
    client = make_client()
    with (
        patch.object(client.rtsp_session, "start", AsyncMock()) as session_start,
        patch.object(client.rtsp_session, "stop", AsyncMock()) as session_stop,
        patch.object(client._GwellIPCamClient__rtsp_proxy, "start", AsyncMock()) as proxy_start,
        patch.object(client._GwellIPCamClient__rtsp_proxy, "stop", AsyncMock()) as proxy_stop,
    ):
        await client.async_start_streaming()
        await client.async_stop_streaming()
    session_start.assert_called_once()
    proxy_start.assert_called_once()
    proxy_stop.assert_called_once()
    session_stop.assert_called_once()


@pytest.mark.asyncio
async def test_async_close_wire_closes_and_clears_an_open_session():
    client = make_client()
    session = MagicMock()
    client._GwellIPCamClient__wire = session
    await client.async_close_wire()
    session.close.assert_called_once()
    assert client._GwellIPCamClient__wire is None


@pytest.mark.asyncio
async def test_async_close_wire_is_a_no_op_when_never_connected():
    client = make_client()
    await client.async_close_wire()  # must not raise


@pytest.mark.asyncio
async def test_get_wire_closes_the_socket_when_binding_fails():
    """A failed bind/connect must not leak the raw socket; it should be closed before APIConnectionError propagates."""

    class _FakeHassResolvingHost:
        async def async_add_executor_job(self, fn, *args: str):
            return fn(*args)

    client = sc.GwellIPCamClient(  # ty: ignore[invalid-argument-type]
        hass=_FakeHassResolvingHost(), host="192.0.2.10", port=51880, password_hash="888888", entry_id="e"
    )
    fake_sock = MagicMock()
    fake_sock.bind.side_effect = OSError("address already in use")
    with (
        patch("custom_components.gwell_ipcam.api.socket.socket", return_value=fake_sock),
        pytest.raises(sc.APIConnectionError),
    ):
        await client._GwellIPCamClient__get_wire()
    assert fake_sock.close.called


@pytest.mark.asyncio
async def test_async_discover_delegates_to_discover_function():
    with patch("custom_components.gwell_ipcam.api._discover", return_value=[]) as discover:
        result = await sc.GwellIPCamClient.async_discover(_FakeHass(), timeout_s=1.0)  # ty: ignore[invalid-argument-type]
    assert result == []
    discover.assert_called_once_with(timeout=1.0)


@pytest.mark.asyncio
async def test_async_discover_one_returns_first_match_or_none():
    camera = sc.DiscoveredCamera(host="192.0.2.10", port=51880, contact_id="9999999", name="IPCam-9999999")
    with patch("custom_components.gwell_ipcam.api._discover", return_value=[camera]):
        assert await sc.GwellIPCamClient.async_discover_one(_FakeHass(), "192.0.2.10") == camera  # ty: ignore[invalid-argument-type]
    with patch("custom_components.gwell_ipcam.api._discover", return_value=[]):
        assert await sc.GwellIPCamClient.async_discover_one(_FakeHass(), "192.0.2.10") is None  # ty: ignore[invalid-argument-type]


@pytest.mark.asyncio
async def test_async_get_identity_raises_when_not_discoverable():
    client = sc.GwellIPCamClient(  # ty: ignore[invalid-argument-type]
        hass=_FakeHass(), host="192.0.2.10", port=51880, password_hash="888888", entry_id="e"
    )
    with (
        patch.object(sc.GwellIPCamClient, "async_discover_one", AsyncMock(return_value=None)),
        pytest.raises(sc.APIConnectionError),
    ):
        await client.async_get_identity()


@pytest.mark.asyncio
async def test_async_get_identity_raises_auth_error_when_unauthenticated():
    client = sc.GwellIPCamClient(  # ty: ignore[invalid-argument-type]
        hass=_FakeHass(), host="192.0.2.10", port=51880, password_hash="888888", entry_id="e"
    )
    camera = sc.DiscoveredCamera(host="192.0.2.10", port=51880, contact_id="9999999", name="IPCam-9999999")
    wire = make_wire_mock(get_device_info=None)
    with (
        patch.object(sc.GwellIPCamClient, "async_discover_one", AsyncMock(return_value=camera)),
        patch.object(client, "_GwellIPCamClient__get_wire", AsyncMock(return_value=wire)),
        pytest.raises(sc.APIAuthError),
    ):
        await client.async_get_identity()


@pytest.mark.asyncio
async def test_async_get_identity_builds_camera_identity():
    client = sc.GwellIPCamClient(  # ty: ignore[invalid-argument-type]
        hass=_FakeHass(), host="192.0.2.10", port=51880, password_hash="888888", entry_id="e"
    )
    camera = sc.DiscoveredCamera(host="192.0.2.10", port=51880, contact_id="9999999", name="IPCam-9999999")
    wire = make_wire_mock(get_device_info={"device_version": "21.0.0.30"})
    with (
        patch.object(sc.GwellIPCamClient, "async_discover_one", AsyncMock(return_value=camera)),
        patch.object(client, "_GwellIPCamClient__get_wire", AsyncMock(return_value=wire)),
    ):
        identity = await client.async_get_identity()
    assert identity == sc.CameraIdentity(
        contact_id="9999999", name="IPCam-9999999", model=sc._DEFAULT_MODEL_NAME, firmware_version="21.0.0.30"
    )


@pytest.mark.asyncio
async def test_async_talk_opens_and_closes_a_talk_session():
    sent_pcm = b"\x00\x01" * 160
    with patch("custom_components.gwell_ipcam.api.TalkSession") as talk_session_cls:
        session = talk_session_cls.return_value
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)
        session.send_pcm16 = AsyncMock()
        client = make_client()
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
