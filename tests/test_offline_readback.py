"""Tests for offline devices and delayed command read-back."""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, patch

import pytest
from custom_components.ambientika_ventilation import coordinator as coordinator_module
from custom_components.ambientika_ventilation.api import (
    AmbientikaApiError,
    AmbientikaDeviceOfflineError,
    AmbientikaNotFoundError,
    AmbientikaStatusUnavailableError,
)
from custom_components.ambientika_ventilation.coordinator import mask_serial
from custom_components.ambientika_ventilation.models import AmbientikaStatus

from .test_api import FakeResponse, make_client
from .test_resilience import GEMINI, MASTER, polling_coordinator, write_coordinator

OFF_STATUS = AmbientikaStatus(
    serial_number=MASTER,
    operating_mode="Off",
    fan_speed="Low",
    humidity_level="Normal",
    light_sensor_level="Low",
    temperature=16,
    schedule_state="Off",
)


def packet(**values: object) -> dict[str, object]:
    """Return a device status payload for the master."""
    return {
        "deviceSerialNumber": MASTER,
        "operatingMode": "Off",
        "fanSpeed": "Low",
        "humidityLevel": "Normal",
        "lightSensorLevel": "Low",
        "temperature": 16,
        "isScheduled": "Off",
        **values,
    }


async def test_missing_status_packet_means_device_offline() -> None:
    """HTTP 404 "Status packet not found!" is an offline device, not a gap."""
    client = make_client([FakeResponse(404, "Status packet not found!")])

    with pytest.raises(AmbientikaStatusUnavailableError) as error:
        await client.async_device_status(MASTER)

    assert isinstance(error.value, AmbientikaDeviceOfflineError)
    assert not isinstance(error.value, AmbientikaNotFoundError)
    assert error.value.kind == "device_offline"

    other = make_client([FakeResponse(404, "Status packet not found!")])
    with pytest.raises(AmbientikaNotFoundError):
        await other.async_schedule(101)


async def test_offline_device_keeps_being_polled_and_recovers(
    hass, houses_payload, status_payload, caplog
) -> None:
    """An offline unit is unavailable, polled every cycle, and logged once."""
    caplog.set_level(logging.INFO)
    coordinator, api = polling_coordinator(hass, houses_payload)
    api.async_house_devices_status.return_value = {
        "zoneDevicesInfo": [{"statusPacket": status_payload}],
        "geminiDevicesInfo": [
            {"statusPacket": {**status_payload, "deviceSerialNumber": GEMINI}}
        ],
    }
    coordinator.data = await coordinator._async_update_data()
    api.async_house_devices_status.return_value = {
        "zoneDevicesInfo": [{"statusPacket": status_payload}]
    }
    api.async_device_status.side_effect = AmbientikaStatusUnavailableError

    for _ in range(2):
        coordinator.data = await coordinator._async_update_data()

    assert coordinator.data.failed_devices == frozenset({GEMINI})
    assert coordinator.data.devices[GEMINI].status.temperature == 21
    assert GEMINI not in coordinator._unsupported_status
    assert api.async_device_status.await_count == 2
    assert coordinator.offline_devices == {GEMINI}
    assert coordinator.offline_transitions == 1
    offline_logs = [
        record
        for record in caplog.records
        if record.levelno == logging.WARNING and "offline" in record.getMessage()
    ]
    assert len(offline_logs) == 1
    assert mask_serial(GEMINI) in offline_logs[0].getMessage()
    assert "Unable to update" not in caplog.text

    api.async_device_status.side_effect = lambda serial: {
        **status_payload,
        "deviceSerialNumber": serial,
        "temperature": 19,
    }
    coordinator.data = await coordinator._async_update_data()

    assert not coordinator.data.failed_devices
    assert coordinator.data.devices[GEMINI].status.temperature == 19
    assert not coordinator.offline_devices
    assert "back online" in caplog.text
    assert "Recovered updates" not in caplog.text
    assert GEMINI not in caplog.text


async def test_offline_device_failing_otherwise_is_not_reported_recovered(
    hass, houses_payload, status_payload, caplog
) -> None:
    """A device stays offline until a status is actually read again."""
    caplog.set_level(logging.INFO)
    coordinator, api = polling_coordinator(hass, houses_payload)
    api.async_house_devices_status.return_value = {
        "zoneDevicesInfo": [{"statusPacket": status_payload}]
    }
    api.async_device_status.side_effect = AmbientikaStatusUnavailableError
    coordinator.data = await coordinator._async_update_data()
    api.async_device_status.side_effect = AmbientikaApiError("timeout")

    coordinator.data = await coordinator._async_update_data()

    assert coordinator.offline_devices == {GEMINI}
    assert "back online" not in caplog.text


async def test_read_back_uses_first_matching_status(hass) -> None:
    """A command reflected within the first delay is read back once."""
    coordinator, api = write_coordinator(hass, OFF_STATUS)
    api.async_device_status.return_value = packet(
        operatingMode="ManualHeatRecovery", fanSpeed="High", temperature=17
    )

    await coordinator.async_write_state(
        MASTER, operating_mode="ManualHeatRecovery", fan_speed="High"
    )

    assert api.async_device_status.await_count == 1
    status = coordinator.data.devices[MASTER].status
    assert status.operating_mode == "ManualHeatRecovery"
    assert status.temperature == 17


async def test_read_back_retries_once_for_a_late_cloud_update(hass) -> None:
    """A stale first read-back is followed by one more read."""
    coordinator, api = write_coordinator(hass, OFF_STATUS)
    api.async_device_status.side_effect = [
        packet(),
        packet(operatingMode="Auto", temperature=18),
    ]

    await coordinator.async_write_state(MASTER, operating_mode="Auto")

    assert api.async_device_status.await_count == 2
    status = coordinator.data.devices[MASTER].status
    assert status.operating_mode == "Auto"
    assert status.temperature == 18


async def test_unreflected_command_is_shown_optimistically(hass, monkeypatch) -> None:
    """Without a matching read-back the written values are shown until polling."""
    monkeypatch.setattr(coordinator_module, "READBACK_DELAY", 3.0)
    coordinator, api = write_coordinator(hass, OFF_STATUS)
    api.async_device_status.return_value = packet(temperature=15)

    with patch(
        "custom_components.ambientika_ventilation.coordinator.asyncio.sleep",
        new=AsyncMock(),
    ) as sleep:
        await coordinator.async_write_state(MASTER, operating_mode="Auto")

    assert [call.args for call in sleep.await_args_list] == [(3.0,), (3.0,)]
    assert api.async_device_status.await_count == 2
    status = coordinator.data.devices[MASTER].status
    assert status.operating_mode == "Auto"
    assert status.fan_speed == "Low"
    assert status.temperature == 15


async def test_failed_read_back_still_reports_success(hass) -> None:
    """An accepted command is not reported as failed when reading back fails."""
    coordinator, api = write_coordinator(
        hass,
        AmbientikaStatus(
            serial_number=MASTER,
            operating_mode="Auto",
            fan_speed="Low",
            humidity_level="Normal",
            light_sensor_level="Low",
            schedule_state="On",
        ),
    )
    api.async_device_status.side_effect = AmbientikaApiError("timeout")

    await coordinator.async_write_state(MASTER, schedule_mode=False)

    status = coordinator.data.devices[MASTER].status
    assert status.schedule_state == "Off"
    assert status.operating_mode == "Auto"


async def test_read_back_auth_failure_starts_reauth(hass) -> None:
    """Rejected credentials during read-back start reauthentication."""
    from custom_components.ambientika_ventilation.api import AmbientikaAuthError

    coordinator, api = write_coordinator(hass, OFF_STATUS)
    api.async_device_status.side_effect = AmbientikaAuthError

    with patch.object(coordinator.config_entry, "async_start_reauth") as reauth:
        await coordinator.async_write_state(MASTER, operating_mode="Auto")

    reauth.assert_called_once_with(hass)
    assert api.async_device_status.await_count == 1
    assert coordinator.data.devices[MASTER].status.operating_mode == "Auto"


@pytest.mark.parametrize(
    ("read_back", "expected"),
    [
        ([packet(filtersStatus="Good", temperature=20)], 20),
        ([packet(filtersStatus="Bad"), packet(filtersStatus="Bad")], 16),
    ],
)
async def test_filter_reset_read_back(hass, read_back, expected) -> None:
    """A filter reset shows Good even when the cloud has not caught up yet."""
    coordinator, api = write_coordinator(
        hass,
        AmbientikaStatus(
            serial_number=MASTER,
            operating_mode="Auto",
            fan_speed="Low",
            humidity_level="Normal",
            light_sensor_level="Low",
            filter_status="Bad",
        ),
    )
    api.async_device_status.side_effect = read_back

    await coordinator.async_reset_filter(MASTER)

    status = coordinator.data.devices[MASTER].status
    assert status.filter_status == "Good"
    assert status.temperature == expected
