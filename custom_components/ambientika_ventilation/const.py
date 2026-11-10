"""Constants for the Ambientika integration."""

from __future__ import annotations

from datetime import timedelta
from typing import Final

from homeassistant.const import Platform

DOMAIN: Final = "ambientika_ventilation"
INTEGRATION_VERSION: Final = "1.0.0-beta.1"
DEFAULT_BASE_URL: Final = "https://app.ambientika.eu:4521"
DEFAULT_SCAN_INTERVAL: Final = timedelta(seconds=60)
DISCOVERY_INTERVAL: Final = timedelta(hours=6)
# Devices missing from this many consecutive successful discoveries (every six
# hours) and for at least the minimum age are removed from the registry.
STALE_DEVICE_DISCOVERIES: Final = 4
STALE_DEVICE_MIN_AGE: Final = timedelta(hours=24)
REQUEST_TIMEOUT: Final = 10
CONNECT_TIMEOUT: Final = 5
# Idle connections are kept slightly shorter than the cloud's ~130 s so that
# the next poll reuses the TLS connection instead of opening a new one.
KEEPALIVE_TIMEOUT: Final = 120
MAX_CONCURRENT_REQUESTS: Final = 3
MAX_CONCURRENT_COMMANDS: Final = 2

CONF_SCAN_INTERVAL: Final = "scan_interval"
MIN_SCAN_INTERVAL: Final = 30
MAX_SCAN_INTERVAL: Final = 600
# After a command or a change the cloud is polled at the minimum interval for a
# while, because the device uploads a status packet about every 30 s.
FAST_POLL_DURATION: Final = timedelta(minutes=3)
# Consecutive failed polls stretch the interval up to this limit.
MAX_BACKOFF_INTERVAL: Final = timedelta(minutes=10)
TOKEN_RENEW_BEFORE: Final = timedelta(days=30)
TOKEN_CHECK_INTERVAL: Final = timedelta(days=1)
EVENT_DEVICE_CHANGED: Final = f"{DOMAIN}_device_changed"

CONF_BASE_URL: Final = "base_url"
CONF_EXPIRES_AT: Final = "expires_at"
CONF_TOKEN: Final = "token"
CONF_TOKEN_ONLY: Final = "token_only"
CONF_USER_ID: Final = "user_id"

PLATFORMS: Final = (
    Platform.FAN,
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.SELECT,
    Platform.SWITCH,
    Platform.BUTTON,
    Platform.CALENDAR,
)

OPERATING_MODES: Final = (
    "Smart",
    "Auto",
    "ManualHeatRecovery",
    "Night",
    "AwayHome",
    "Surveillance",
    "TimedExpulsion",
    "Expulsion",
    "Intake",
    "MasterSlaveFlow",
    "SlaveMasterFlow",
    "Off",
)
NON_GEMINI_OPERATING_MODES: Final = OPERATING_MODES
GEMINI_OPERATING_MODES: Final = (
    "Smart",
    "Auto",
    "ManualHeatRecovery",
    "Night",
    "Surveillance",
    "TimedExpulsion",
    "Expulsion",
    "Intake",
    "Off",
)
# Legacy firmware may still report the retired "Turbo" mode (numeric 12). It is
# shown read-only and never written.
REPORTED_OPERATING_MODES: Final = (*OPERATING_MODES, "Turbo")
USER_OPERATING_MODES: Final = OPERATING_MODES[:-1]
FAN_SPEEDS: Final = ("Low", "Medium", "High", "Night", "Turbo")
WRITABLE_FAN_SPEEDS: Final = ("Low", "Medium", "High", "Turbo")
HUMIDITY_LEVELS: Final = ("Dry", "Normal", "Moist")
LIGHT_SENSOR_LEVELS: Final = ("NotAvailable", "Off", "Low", "Medium")
AIR_QUALITY_LEVELS: Final = ("VeryGood", "Good", "Medium", "Poor", "Bad")
FILTER_STATUSES: Final = ("Good", "Medium", "Bad")
SCHEDULE_STATES: Final = ("NotAvailable", "Off", "On")

MODE_WRITABLE_FIELDS: Final = {
    "Smart": frozenset({"light_sensor_level"}),
    "Auto": frozenset({"humidity_level", "light_sensor_level"}),
    "ManualHeatRecovery": frozenset({"fan_speed"}),
    "Night": frozenset(),
    "AwayHome": frozenset(),
    "Surveillance": frozenset({"humidity_level"}),
    "TimedExpulsion": frozenset(),
    "Expulsion": frozenset({"fan_speed"}),
    "Intake": frozenset({"fan_speed"}),
    "MasterSlaveFlow": frozenset({"fan_speed"}),
    "SlaveMasterFlow": frozenset({"fan_speed"}),
    "Off": frozenset(),
}

API_TO_HA: Final = {
    "Smart": "smart",
    "Auto": "auto",
    "ManualHeatRecovery": "heat_recovery",
    "Night": "night",
    "AwayHome": "away",
    "Surveillance": "surveillance",
    "TimedExpulsion": "timed_extraction",
    "Expulsion": "extraction",
    "Intake": "intake",
    "MasterSlaveFlow": "master_slave_flow",
    "SlaveMasterFlow": "slave_master_flow",
    "Off": "off",
    "Low": "low",
    "Medium": "medium",
    "High": "high",
    "Turbo": "turbo",
    "Dry": "dry",
    "Normal": "normal",
    "Moist": "moist",
    "NotAvailable": "not_available",
    "VeryGood": "very_good",
    "Good": "good",
    "Poor": "poor",
    "Bad": "bad",
    "On": "on",
    "Master": "master",
    "SlaveEqualMaster": "slave_equal_master",
    "SlaveOppositeMaster": "slave_opposite_master",
    "NotConfigured": "not_configured",
}


def operating_modes_for_device(device_type: str | None) -> tuple[str, ...]:
    """Return operating modes exposed by the official app for a device type."""
    if device_type == "Gemini":
        return GEMINI_OPERATING_MODES
    return NON_GEMINI_OPERATING_MODES


def mode_allows_setting(mode: str | None, field: str) -> bool:
    """Return whether the official app enables a setting in a mode."""
    return field in MODE_WRITABLE_FIELDS.get(mode or "", frozenset())


# --- Hybrid cloud + local data model ---------------------------------------

CONF_LOCAL_UDP: Final = "local_udp"
CONF_RAW_VARIANTS: Final = "raw_variants"
LOCAL_UDP_AUTO: Final = "auto"
LOCAL_UDP_ON: Final = "on"
LOCAL_UDP_OFF: Final = "off"
LOCAL_UDP_MODES: Final = (LOCAL_UDP_AUTO, LOCAL_UDP_ON, LOCAL_UDP_OFF)
DEFAULT_LOCAL_UDP: Final = LOCAL_UDP_AUTO
DEFAULT_RAW_VARIANTS: Final = False

# The cloud value stays usable for 2.5 scan intervals, at least 150 s.
CLOUD_TTL_FACTOR: Final = 2.5
MIN_CLOUD_TTL: Final = 150.0
# The auto mode listens this long for the account's own broadcasts.
PROBE_DURATION: Final = 60.0
# A listener that hears nothing for this long raises a repair issue ("on").
NO_FRAMES_AFTER: Final = 300.0
# A UDP-driven change asks the cloud for confirmation at most this often.
UDP_REFRESH_MIN_INTERVAL: Final = 30.0
# Volatile diagnostics (heartbeat age, last seen) are written at most this often.
THROTTLE_INTERVAL: Final = timedelta(seconds=60)

STORE_VERSION: Final = 1
STORE_KEY_PREFIX: Final = f"{DOMAIN}"
ISSUE_LOCAL_UDP_BIND: Final = "local_udp_bind_failed"
ISSUE_LOCAL_UDP_NO_FRAMES: Final = "local_udp_no_frames"

# Identifier prefix of the account-level cloud device (not a unit serial).
CLOUD_DEVICE_PREFIX: Final = "cloud_"
