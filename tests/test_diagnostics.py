"""Tests for diagnostic redaction."""

from __future__ import annotations

from types import SimpleNamespace

from custom_components.ambientika.api import RequestMetrics
from custom_components.ambientika.diagnostics import async_get_config_entry_diagnostics
from custom_components.ambientika.models import (
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
    )
    entry = SimpleNamespace(
        runtime_data=SimpleNamespace(
            coordinator=coordinator,
            api=SimpleNamespace(metrics=RequestMetrics()),
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
    assert serial not in output
    assert "private@example.com" not in output
    assert "top-secret" not in output
    assert "jwt-secret" not in output
    assert "Private room name" not in output
