"""Constants for the Gwell IP Camera integration."""

from logging import Logger, getLogger

LOGGER: Logger = getLogger(__package__)
# Raw UDP/RTSP frame dumps only -- set to WARNING to silence wire-level spam, keeping LOGGER's summaries at debug.
WIRE_LOGGER: Logger = getLogger(f"{__package__}.wire")

DOMAIN = "gwell_ipcam"

CONF_CONTACT_ID = "contact_id"
CONF_PASSWORD_HASH = "password_hash"  # noqa: S105 -- config key name, not a literal secret

DEFAULT_PORT = 51880

DISCOVERY_TIMEOUT_S = 5
DISCOVERY_INTERVAL_S = 900
STATE_UPDATE_INTERVAL_S = 300
RECORDINGS_POLL_INTERVAL_S = 60
FIRMWARE_CHECK_INTERVAL_S = 86400
# Above the camera's minute-resolution clock, so rounding alone doesn't trigger a resync.
CLOCK_DRIFT_THRESHOLD_S = 90

RTSP_PORT = 554
RTSP_PATH = "/onvif1"

TALK_SAMPLE_RATE_HZ = 8000
SATELLITE_SAMPLE_RATE_HZ = 16000
