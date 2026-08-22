"""Constants for the Ambientika integration."""

from __future__ import annotations

from datetime import timedelta
from typing import Final

from homeassistant.const import Platform

DOMAIN: Final = "ambientika_ventilation"
INTEGRATION_VERSION: Final = "0.9.0"
DEFAULT_BASE_URL: Final = "https://app.ambientika.eu:4521"
DEFAULT_SCAN_INTERVAL: Final = timedelta(seconds=60)
DISCOVERY_INTERVAL: Final = timedelta(hours=6)
REQUEST_TIMEOUT: Final = 20
MAX_CONCURRENT_REQUESTS: Final = 3

CONF_BASE_URL: Final = "base_url"
CONF_EXPIRES_AT: Final = "expires_at"
CONF_TOKEN: Final = "token"
CONF_USER_ID: Final = "user_id"

PLATFORMS: Final = (
    Platform.FAN,
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.SELECT,
    Platform.SWITCH,
    Platform.BUTTON,
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
