"""Constants for the Ambientika integration."""

from __future__ import annotations

from datetime import timedelta
from typing import Final

from homeassistant.const import Platform

DOMAIN: Final = "ambientika"
INTEGRATION_VERSION: Final = "0.1.0"
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
USER_OPERATING_MODES: Final = OPERATING_MODES[:9]
FAN_SPEEDS: Final = ("Low", "Medium", "High", "Night", "Turbo")
HUMIDITY_LEVELS: Final = ("Dry", "Normal", "Moist")
LIGHT_SENSOR_LEVELS: Final = ("NotAvailable", "Off", "Low", "Medium")
AIR_QUALITY_LEVELS: Final = ("VeryGood", "Good", "Medium", "Poor", "Bad")
FILTER_STATUSES: Final = ("Good", "Medium", "Bad")
SCHEDULE_STATES: Final = ("NotAvailable", "Off", "On")

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
}
