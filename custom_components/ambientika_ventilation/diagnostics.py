"""Privacy-preserving diagnostics for Ambientika."""

from __future__ import annotations

import time
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntry

from . import AmbientikaConfigEntry
from .const import DOMAIN, INTEGRATION_VERSION
from .hub import AmbientikaHub, DeviceLiveness
from .models import is_controllable_device, turbo_usable


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
        controllable = is_controllable_device(device_data.device, status)
        devices.append(
            {
                "redacted_id": _redact_identifier(serial),
                "device_type": device_data.device.device_type,
                "device_subtype": device_data.device.device_subtype,
                "role": device_data.device.role,
                "has_status": status is not None,
                "metadata": {
                    "has_installation": device_data.device.installation is not None,
                    "has_room": device_data.device.room_name is not None,
                    "has_zone": device_data.device.zone_name is not None,
                    "firmware_fields": sum(
                        value is not None
                        for value in (
                            device_data.device.micro_firmware,
                            device_data.device.radio_firmware,
                            device_data.device.radio_at_firmware,
                        )
                    ),
                    "schedule_entries": (
                        len(device_data.schedule.time_slots)
                        if device_data.schedule is not None
                        else None
                    ),
                },
                "capabilities": {
                    "fan_control": (
                        controllable
                        and status is not None
                        and _has_writable_state(status)
                    ),
                    "directly_controllable": controllable,
                    "manual_controls_locked": (
                        status is not None
                        and (
                            status.filter_status == "Bad"
                            or status.schedule_state == "On"
                        )
                    ),
                    "light_sensor": (
                        status is not None
                        and status.light_sensor_level not in (None, "NotAvailable")
                    ),
                    "turbo": turbo_usable(status, coordinator.data.feature_flags),
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
            "houses",
            "house_devices_status",
            "device_status",
            "schedule",
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
            "last_failure_causes": list(
                getattr(coordinator, "last_failure_causes", ())
            ),
            "discovery_failures": getattr(coordinator, "discovery_failures", 0),
            "next_discovery_attempt": _isoformat(
                getattr(coordinator, "next_discovery_attempt", None)
            ),
            "offline_devices": sorted(
                _redact_identifier(serial)
                for serial in getattr(coordinator, "offline_devices", ())
            ),
            "offline_transitions": getattr(coordinator, "offline_transitions", 0),
            "poll_interval_s": (
                coordinator.update_interval.total_seconds()
                if coordinator.update_interval
                else None
            ),
            "configured_interval_s": coordinator.base_interval.total_seconds(),
            "consecutive_failures": coordinator.consecutive_failures,
            "cycle_durations_ms": list(coordinator.cycle_durations_ms),
            "discarded_polls": coordinator.discarded_polls,
            "device_changes": coordinator.device_changes,
        },
        "token": {
            "expires_at": runtime.api.token_expires_at,
            "refreshes": runtime.api.metrics.token_refreshes,
        },
        "request_metrics": runtime.api.metrics.as_dict(),
        "hub": _hub_diagnostics(runtime.hub),
        "local_udp": runtime.local.diagnostics(),
        "analytics": runtime.analytics.diagnostics(),
        "devices": devices,
    }


async def async_get_device_diagnostics(
    hass: HomeAssistant,
    entry: AmbientikaConfigEntry,
    device: DeviceEntry,
) -> dict[str, Any]:
    """Return the state of one unit; the serial is reduced to its last digits."""
    coordinator = entry.runtime_data.coordinator
    serial = next(
        (
            identifier
            for domain, identifier in device.identifiers
            if domain == DOMAIN and identifier in coordinator.data.devices
        ),
        None,
    )
    if serial is None:
        return {"known": False}
    device_data = coordinator.data.devices[serial]
    status = device_data.status
    metadata = device_data.device
    return {
        "known": True,
        "redacted_id": _redact_identifier(serial),
        "device_type": metadata.device_type,
        "device_subtype": metadata.device_subtype,
        "role": metadata.role,
        "firmware": {
            "micro": metadata.micro_firmware,
            "radio": metadata.radio_firmware,
            "radio_at": metadata.radio_at_firmware,
            "history": coordinator.firmware_history.get(serial, []),
        },
        "status": (
            {
                "operating_mode": status.operating_mode,
                "fan_speed": status.fan_speed,
                "humidity_level": status.humidity_level,
                "light_sensor_level": status.light_sensor_level,
                "air_quality": status.air_quality,
                "filter_status": status.filter_status,
                "schedule_state": status.schedule_state,
                "device_role": status.device_role,
                "last_operating_mode": status.last_operating_mode,
                "turbo_available": status.turbo_available,
                "has_signal": status.signal_strength is not None,
            }
            if status is not None
            else None
        ),
        "offline": serial in coordinator.offline_devices,
        "failed": serial in coordinator.data.failed_devices,
        "status_error_streak": coordinator._status_errors.get(serial, 0),
        "schedule_entries": (
            len(device_data.schedule.time_slots) if device_data.schedule else None
        ),
        "liveness": _liveness(entry.runtime_data.hub.liveness(serial)),
    }


def _liveness(liveness: DeviceLiveness | None) -> dict[str, Any] | None:
    """Return the data source state of one device without identifiers."""
    if liveness is None:
        return None
    return {
        "data_source": liveness.data_source,
        "local_freshness": liveness.local_freshness.value,
        "local_age_s": _rounded(liveness.local_age_s),
        "local_engaged": liveness.local_engaged,
        "cloud_reachable": liveness.cloud_reachable,
        "cloud_age_s": _rounded(liveness.cloud_age_s),
        "conflicts": liveness.conflicts,
        "in_conflict": liveness.in_conflict,
    }


def _rounded(value: float | None) -> float | None:
    return None if value is None else round(value, 1)


def _hub_diagnostics(hub: AmbientikaHub) -> dict[str, Any]:
    """Return the merged-data state: no house id, no addresses, masked serials."""
    health = hub.cloud_health
    now = time.monotonic()

    def since(at: float | None) -> float | None:
        return None if at is None else round(max(now - at, 0.0), 1)

    return {
        "cloud_reachable": hub.cloud_reachable(),
        "cloud_health": {
            "consecutive_failures": health.consecutive_failures,
            "last_failure_kind": health.last_failure_kind,
            "failures_by_kind": dict(health.failures_by_kind),
            "requests_total": health.requests_total,
            "failures_total": health.failures_total,
            "success_rate": _rounded(health.success_rate),
            "last_latency_s": _rounded(health.last_latency_s),
            "seconds_since_success": since(health.last_success_at),
            "seconds_since_failure": since(health.last_failure_at),
            "seconds_since_change": since(health.last_change_at),
        },
        "unmapped_local_zones": len(hub.unmapped_zones()),
        "devices": [
            {"redacted_id": _redact_identifier(serial), **(_liveness(live) or {})}
            for serial in sorted(hub.serials)
            for live in (hub.liveness(serial),)
        ],
    }


def _isoformat(value: Any) -> str | None:
    """Return an ISO timestamp for optional datetimes."""
    return value.isoformat() if value is not None else None


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
