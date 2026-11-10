"""Tests for diagnostic redaction."""

from __future__ import annotations

from collections import deque
from datetime import timedelta
from types import SimpleNamespace

from custom_components.ambientika_ventilation.api import RequestMetrics
from custom_components.ambientika_ventilation.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.ambientika_ventilation.hub import AmbientikaHub
from custom_components.ambientika_ventilation.models import (
    AmbientikaData,
    AmbientikaDevice,
    AmbientikaDeviceData,
    AmbientikaStatus,
)


async def test_diagnostics_redact_credentials_and_identifiers(hass) -> None:
    """Diagnostics never include passwords, tokens, usernames, or full serials."""
    serial = "AABBCCDDEEFF"
    coordinator = SimpleNamespace(
        data=AmbientikaData(
            devices={
                serial: AmbientikaDeviceData(
                    device=AmbientikaDevice(
                        serial_number=serial,
                        name="Private room name",
                        device_type="Diamond",
                    ),
                    status=AmbientikaStatus(
                        serial_number=serial,
                        operating_mode="Auto",
                        fan_speed="Low",
                        humidity_level="Normal",
                        light_sensor_level="Low",
                    ),
                )
            }
        ),
        last_successful_update=None,
        last_update_duration_ms=10,
        last_partial_failures=0,
        update_interval=timedelta(seconds=60),
        base_interval=timedelta(seconds=60),
        consecutive_failures=0,
        cycle_durations_ms=deque([10, 12]),
        discarded_polls=0,
        device_changes=0,
    )
    entry = SimpleNamespace(
        runtime_data=SimpleNamespace(
            coordinator=coordinator,
            api=SimpleNamespace(
                metrics=RequestMetrics(), token_expires_at="2027-01-01T00:00:00+00:00"
            ),
            hub=AmbientikaHub(),
            local=SimpleNamespace(diagnostics=lambda: {"mode": "auto"}),
        ),
        version=1,
        minor_version=1,
        data={
            "username": "private@example.com",
            "password": "top-secret",
            "token": "jwt-secret",
            "base_url": "https://ambientika.test",
        },
    )

    result = await async_get_config_entry_diagnostics(hass, entry)
    output = repr(result)

    assert result["devices"][0]["redacted_id"] == "***EEFF"
    assert result["coordinator"]["last_failure_causes"] == []
    assert result["coordinator"]["discovery_failures"] == 0
    assert result["coordinator"]["next_discovery_attempt"] is None
    assert result["coordinator"]["offline_devices"] == []
    assert result["coordinator"]["offline_transitions"] == 0
    assert result["coordinator"]["poll_interval_s"] == 60
    assert result["coordinator"]["cycle_durations_ms"] == [10, 12]
    assert result["token"]["expires_at"] == "2027-01-01T00:00:00+00:00"

    coordinator.offline_devices = {serial}
    coordinator.offline_transitions = 2
    result = await async_get_config_entry_diagnostics(hass, entry)
    assert result["coordinator"]["offline_devices"] == ["***EEFF"]
    assert result["coordinator"]["offline_transitions"] == 2
    assert serial not in repr(result)
    assert result["request_metrics"]["error_kinds"] == {}
    assert result["request_metrics"]["last_error"] is None
    assert serial not in output
    assert "private@example.com" not in output
    assert "top-secret" not in output
    assert "jwt-secret" not in output
    assert "Private room name" not in output
