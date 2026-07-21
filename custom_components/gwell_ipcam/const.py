"""Constants for the Gwell IP Camera integration."""

from logging import Logger, getLogger

LOGGER: Logger = getLogger(__package__)

DOMAIN = "gwell_ipcam"

CONF_CONTACT_ID = "contact_id"
CONF_PASSWORD_HASH = "password_hash"  # noqa: S105 -- config key name, not a literal secret

DEFAULT_PORT = 51880

DISCOVERY_TIMEOUT_S = 5
DISCOVERY_INTERVAL_S = 900
STATE_UPDATE_INTERVAL_S = 300
RECORDINGS_POLL_INTERVAL_S = 60
FIRMWARE_CHECK_INTERVAL_S = 86400
# The camera's clock has minute (not second) resolution, so give drift-correction enough
# margin to not trigger on rounding alone.
CLOCK_DRIFT_THRESHOLD_S = 90

RTSP_PORT = 554
RTSP_PATH = "/onvif1"

TALK_SAMPLE_RATE_HZ = 8000
SATELLITE_SAMPLE_RATE_HZ = 16000
