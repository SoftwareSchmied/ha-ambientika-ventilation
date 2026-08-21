"""Privacy-preserving diagnostics for Ambientika."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant

from . import AmbientikaConfigEntry
from .const import INTEGRATION_VERSION


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant,
    entry: AmbientikaConfigEntry,
) -> dict[str, Any]:
    """Return useful counters without credentials or stable full identifiers."""
    runtime = entry.runtime_data
    coordinator = runtime.coordinator
    devices: list[dict[str, Any]] = []
    for serial, device_data in coordinator.data.devices.items():
        status = device_data.status
        devices.append(
            {
                "redacted_id": _redact_identifier(serial),
                "device_type": device_data.device.device_type,
                "device_subtype": device_data.device.device_subtype,
                "role": device_data.device.role,
                "has_status": status is not None,
                "capabilities": {
                    "fan_control": status is not None and _has_writable_state(status),
                    "light_sensor": (
                        status is not None
                        and status.light_sensor_level not in (None, "NotAvailable")
                    ),
                    "turbo": status is not None and status.turbo_available,
                    "schedule": (
                        status is not None
                        and status.schedule_state not in (None, "NotAvailable")
                    ),
                },
                "active_conditions": {
                    "humidity_alarm": status.humidity_alarm if status else None,
                    "filter_problem": (
                        status.filter_status != "Good"
                        if status and status.filter_status
                        else None
                    ),
                    "night": status.night_alarm if status else None,
                },
            }
        )

    return {
        "integration_version": INTEGRATION_VERSION,
        "entry": {
            "version": entry.version,
            "minor_version": entry.minor_version,
            "device_count": len(devices),
            "configured_base_url": bool(entry.data.get("base_url")),
        },
        "supported_resources": [
            "houses_info",
            "device_status",
            "change_mode",
            "reset_filter",
            "feature_flags",
            "token_refresh",
        ],
        "feature_flags": coordinator.data.feature_flags,
        "coordinator": {
            "last_successful_update": (
                coordinator.last_successful_update.isoformat()
                if coordinator.last_successful_update
                else None
            ),
            "last_update_duration_ms": coordinator.last_update_duration_ms,
            "partial_failures": coordinator.last_partial_failures,
        },
        "request_metrics": runtime.api.metrics.as_dict(),
        "devices": devices,
    }


def _redact_identifier(value: str) -> str:
    """Expose only a short suffix useful for matching a physical device."""
    return f"***{value[-4:]}" if len(value) >= 4 else "***"


def _has_writable_state(status: Any) -> bool:
    """Return whether all fields required by change-mode are known."""
    return all(
        isinstance(value, str)
        for value in (
            status.operating_mode,
            status.fan_speed,
            status.humidity_level,
            status.light_sensor_level,
        )
    )
