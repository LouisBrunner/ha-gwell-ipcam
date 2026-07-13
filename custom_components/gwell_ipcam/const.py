"""Constants for the Gwell IP Camera integration."""

from logging import Logger, getLogger

LOGGER: Logger = getLogger(__package__)

DOMAIN = "gwell_ipcam"

CONF_CONTACT_ID = "contact_id"
CONF_PASSWORD_HASH = "password_hash"  # noqa: S105 -- config key name, not a literal secret

DISCOVERY_TIMEOUT_S = 5
DISCOVERY_INTERVAL_S = 900
STATE_UPDATE_INTERVAL_S = 300
RECORDINGS_POLL_INTERVAL_S = 60
