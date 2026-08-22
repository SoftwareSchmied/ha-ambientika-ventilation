"""Tests for coordinator discovery, polling, and writes."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from custom_components.ambientika_ventilation.api import (
    AmbientikaApiError,
    AmbientikaAuthError,
    AmbientikaForbiddenError,
    AmbientikaNotFoundError,
)
from custom_components.ambientika_ventilation.const import DOMAIN
from custom_components.ambientika_ventilation.coordinator import AmbientikaCoordinator
from custom_components.ambientika_ventilation.models import (
    AmbientikaData,
    AmbientikaDevice,
    AmbientikaDeviceData,
    AmbientikaSchedule,
    AmbientikaStatus,
    parse_houses,
)
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    HomeAssistantError,
    ServiceValidationError,
)
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import MockConfigEntry


async def test_coordinator_discovers_multiple_devices(
    hass, houses_payload, status_payload
) -> None:
    """One coordinator handles a changing number of devices."""
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    api = AsyncMock()
    api.async_houses_info.return_value = houses_payload
    api.async_feature_flags.return_value = {"turboMode": True}
    api.async_device_status.side_effect = lambda serial: {
        **status_payload,
        "deviceSerialNumber": serial,
    }
    coordinator = AmbientikaCoordinator(hass, entry, api)

    data = await coordinator._async_update_data()

    assert set(data.devices) == {"AABBCCDDEEFF", "112233445566"}
    assert data.devices["112233445566"].status.temperature == 21
    assert data.feature_flags == {"turboMode": True}


async def test_write_sends_complete_state_and_reads_back(
    hass, houses_payload, status_payload
) -> None:
    """A partial HA command sends a complete API state and verifies it."""
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    api = AsyncMock()
    api.async_houses_info.return_value = houses_payload[:1]
    api.async_feature_flags.return_value = {}
    api.async_device_status.return_value = status_payload
    coordinator = AmbientikaCoordinator(hass, entry, api)
    coordinator.data = await coordinator._async_update_data()

    await coordinator.async_write_state("AABBCCDDEEFF", fan_speed="High")

    api.async_change_mode.assert_awaited_once_with(
        "AABBCCDDEEFF",
        operating_mode="ManualHeatRecovery",
        fan_speed="High",
        humidity_level="Normal",
        light_sensor_level="Low",
        schedule_mode=False,
    )
    assert api.async_device_status.await_count >= 3


async def test_batch_status_avoids_individual_requests(
    hass, houses_payload, status_payload
) -> None:
    """A complete house batch response replaces per-device status calls."""
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    api = AsyncMock()
    api.async_houses_info.return_value = houses_payload
    api.async_feature_flags.return_value = {"weeklyScheduler": False}
    api.async_house_devices_status.return_value = {
        "zoneDevicesInfo": [{"statusPacket": status_payload}],
        "geminiDevicesInfo": [
            {
                "statusPacket": {
                    **status_payload,
                    "deviceSerialNumber": "112233445566",
                }
            }
        ],
    }
    coordinator = AmbientikaCoordinator(hass, entry, api)

    data = await coordinator._async_update_data()

    assert len(data.devices) == 2
    api.async_house_devices_status.assert_awaited_once_with(10)
    api.async_device_status.assert_not_awaited()


async def test_slave_devices_remain_diagnostic_only(
    hass, houses_payload, status_payload
) -> None:
    """A non-Gemini slave is discovered but not polled as a zone controller."""
    slave = {
        **houses_payload[0]["nonGeminiZones"][0]["rooms"][0]["devices"][0],
        "id": 103,
        "serialNumber": "FFEEDDCCBBAA",
        "name": "Living room slave",
        "role": "SlaveEqualMaster",
    }
    houses_payload[0]["nonGeminiZones"][0]["rooms"][0]["devices"].append(slave)
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    api = AsyncMock()
    api.async_houses_info.return_value = houses_payload
    api.async_feature_flags.return_value = {"weeklyScheduler": False}
    api.async_house_devices_status.return_value = {
        "zoneDevicesInfo": [{"statusPacket": status_payload}],
        "geminiDevicesInfo": [
            {
                "statusPacket": {
                    **status_payload,
                    "deviceSerialNumber": "112233445566",
                }
            }
        ],
    }
    coordinator = AmbientikaCoordinator(hass, entry, api)

    data = await coordinator._async_update_data()

    assert data.devices["FFEEDDCCBBAA"].status is None
    api.async_device_status.assert_not_awaited()


async def test_write_preserves_active_schedule(
    hass, houses_payload, status_payload
) -> None:
    """Manual controls are rejected while the official schedule mode is active."""
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    api = AsyncMock()
    api.async_houses_info.return_value = houses_payload
    api.async_feature_flags.return_value = {"weeklyScheduler": False}
    api.async_device_status.return_value = {**status_payload, "isScheduled": "On"}
    coordinator = AmbientikaCoordinator(hass, entry, api)
    coordinator.data = await coordinator._async_update_data()

    with pytest.raises(ServiceValidationError) as error:
        await coordinator.async_write_state("AABBCCDDEEFF", humidity_level="Moist")

    assert error.value.translation_key == "controls_locked_schedule"
    api.async_change_mode.assert_not_awaited()


async def test_write_rejects_setting_disabled_for_mode(
    hass, houses_payload, status_payload
) -> None:
    """The app's per-mode control matrix is enforced centrally."""
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    api = AsyncMock()
    api.async_houses_info.return_value = houses_payload
    api.async_feature_flags.return_value = {"weeklyScheduler": False}
    api.async_device_status.return_value = {**status_payload, "operatingMode": "Smart"}
    coordinator = AmbientikaCoordinator(hass, entry, api)
    coordinator.data = await coordinator._async_update_data()

    with pytest.raises(ServiceValidationError) as error:
        await coordinator.async_write_state("AABBCCDDEEFF", fan_speed="High")

    assert error.value.translation_key == "setting_unavailable"


async def test_write_rejects_controls_with_bad_filter(
    hass, houses_payload, status_payload
) -> None:
    """A bad filter locks manual controls just like the official app."""
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    api = AsyncMock()
    api.async_houses_info.return_value = houses_payload
    api.async_feature_flags.return_value = {"weeklyScheduler": False}
    api.async_device_status.return_value = {**status_payload, "filtersStatus": "Bad"}
    coordinator = AmbientikaCoordinator(hass, entry, api)
    coordinator.data = await coordinator._async_update_data()

    with pytest.raises(ServiceValidationError) as error:
        await coordinator.async_write_state("AABBCCDDEEFF", operating_mode="Night")

    assert error.value.translation_key == "controls_locked_filter"


async def test_away_mode_uses_app_humidity_default(
    hass, houses_payload, status_payload
) -> None:
    """Selecting Away sends the Normal humidity value chosen by the app."""
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    api = AsyncMock()
    api.async_houses_info.return_value = houses_payload
    api.async_feature_flags.return_value = {"weeklyScheduler": False}
    api.async_device_status.return_value = {**status_payload, "humidityLevel": "Dry"}
    coordinator = AmbientikaCoordinator(hass, entry, api)
    coordinator.data = await coordinator._async_update_data()

    await coordinator.async_write_state("AABBCCDDEEFF", operating_mode="AwayHome")

    assert api.async_change_mode.await_args.kwargs["humidity_level"] == "Normal"


async def test_optional_discovery_resources_do_not_block_updates(
    hass, houses_payload, status_payload
) -> None:
    """Unsupported metadata, flags, and schedules remain optional."""
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    api = AsyncMock()
    api.async_houses_info.return_value = houses_payload
    api.async_houses.side_effect = AmbientikaForbiddenError
    api.async_feature_flags.side_effect = AmbientikaNotFoundError
    api.async_schedule.side_effect = AmbientikaNotFoundError
    api.async_device_status.side_effect = lambda serial: {
        **status_payload,
        "deviceSerialNumber": serial,
    }
    coordinator = AmbientikaCoordinator(hass, entry, api)

    data = await coordinator._async_update_data()

    assert len(data.devices) == 2
    assert data.feature_flags == {}
    assert all(item.schedule is None for item in data.devices.values())


async def test_schedule_discovery_keeps_success_and_previous_value(
    hass, houses_payload, status_payload
) -> None:
    """One optional schedule failure does not discard another schedule."""
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    api = AsyncMock()
    api.async_houses_info.return_value = houses_payload
    api.async_houses.return_value = []
    api.async_feature_flags.return_value = {"weeklyScheduler": True}

    async def schedule_for_device(device_id: int) -> dict[str, object]:
        if device_id == 101:
            return {"id": 7, "timeSlots": []}
        raise AmbientikaApiError("temporary")

    api.async_schedule.side_effect = schedule_for_device
    api.async_device_status.side_effect = lambda serial: {
        **status_payload,
        "deviceSerialNumber": serial,
    }
    coordinator = AmbientikaCoordinator(hass, entry, api)
    coordinator._schedules["112233445566"] = AmbientikaSchedule(schedule_id=6)

    data = await coordinator._async_update_data()

    assert data.devices["AABBCCDDEEFF"].schedule.schedule_id == 7
    assert data.devices["112233445566"].schedule.schedule_id == 6


async def test_partial_device_failure_marks_only_that_device_unavailable(
    hass, houses_payload, status_payload, caplog
) -> None:
    """Last values are retained while availability tracks a partial outage."""
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    api = AsyncMock()
    coordinator = AmbientikaCoordinator(hass, entry, api)
    coordinator._known_devices = parse_houses(houses_payload)
    coordinator._last_discovery = datetime.now(UTC)
    coordinator.data = AmbientikaData(
        devices={
            serial: AmbientikaDeviceData(
                device=device,
                status=AmbientikaStatus(
                    serial_number=serial,
                    operating_mode="Auto",
                    fan_speed="Low",
                    humidity_level="Normal",
                    light_sensor_level="Low",
                ),
            )
            for serial, device in coordinator._known_devices.items()
        }
    )
    api.async_house_devices_status.return_value = {
        "zoneDevicesInfo": [{"statusPacket": status_payload}]
    }
    api.async_device_status.side_effect = AmbientikaApiError("offline")

    data = await coordinator._async_update_data()

    assert data.failed_devices == frozenset({"112233445566"})
    assert data.devices["112233445566"].status is not None
    assert coordinator.last_partial_failures == 1
    assert "temporarily unavailable" in caplog.text

    api.async_house_devices_status.return_value = {
        "zoneDevicesInfo": [{"statusPacket": status_payload}],
        "geminiDevicesInfo": [
            {
                "statusPacket": {
                    **status_payload,
                    "deviceSerialNumber": "112233445566",
                }
            }
        ],
    }
    recovered = await coordinator._async_update_data()
    assert recovered.failed_devices == frozenset()
    assert "Recovered updates" in caplog.text


async def test_total_status_outage_raises_update_failed(
    hass, houses_payload, status_payload
) -> None:
    """A cycle with no fresh device status marks the coordinator unavailable."""
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    api = AsyncMock()
    coordinator = AmbientikaCoordinator(hass, entry, api)
    coordinator._known_devices = parse_houses(houses_payload)
    coordinator._last_discovery = datetime.now(UTC)
    coordinator.data = AmbientikaData(
        devices={
            serial: AmbientikaDeviceData(
                device=device,
                status=AmbientikaStatus(serial_number=serial),
            )
            for serial, device in coordinator._known_devices.items()
        }
    )
    api.async_house_devices_status.side_effect = AmbientikaApiError("offline")
    api.async_device_status.side_effect = AmbientikaApiError("offline")

    with pytest.raises(UpdateFailed, match="No Ambientika device status"):
        await coordinator._async_update_data()


async def test_initial_discovery_errors_are_classified(hass) -> None:
    """Authentication starts reauth while a network failure delays setup."""
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    api = AsyncMock()
    coordinator = AmbientikaCoordinator(hass, entry, api)

    api.async_houses_info.side_effect = AmbientikaAuthError
    with pytest.raises(ConfigEntryAuthFailed):
        await coordinator._async_update_data()

    api.async_houses_info.side_effect = AmbientikaApiError("offline")
    with pytest.raises(UpdateFailed, match="offline"):
        await coordinator._async_update_data()


def coordinator_for_write(
    hass, status: AmbientikaStatus
) -> tuple[AmbientikaCoordinator, AsyncMock]:
    """Return a coordinator with one writable test device."""
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    api = AsyncMock()
    coordinator = AmbientikaCoordinator(hass, entry, api)
    coordinator.data = AmbientikaData(
        devices={
            "serial": AmbientikaDeviceData(
                device=AmbientikaDevice(
                    serial_number="serial", name="Unit", role="Master"
                ),
                status=status,
            )
        }
    )
    return coordinator, api


@pytest.mark.parametrize(
    ("status", "kwargs", "key"),
    [
        (None, {}, "device_unavailable"),
        (
            AmbientikaStatus(serial_number="serial", device_role="SlaveEqualMaster"),
            {},
            "device_not_controllable",
        ),
        (
            AmbientikaStatus(
                serial_number="serial",
                device_type="Gemini",
                operating_mode="Auto",
                fan_speed="Low",
                humidity_level="Normal",
                light_sensor_level="Low",
            ),
            {"operating_mode": "AwayHome"},
            "unsupported_operating_mode",
        ),
        (
            AmbientikaStatus(
                serial_number="serial",
                operating_mode="Auto",
                fan_speed="Low",
                humidity_level="Normal",
                light_sensor_level="Low",
                schedule_state="NotAvailable",
            ),
            {"schedule_mode": True},
            "schedule_unsupported",
        ),
        (
            AmbientikaStatus(serial_number="serial", operating_mode="Auto"),
            {},
            "incomplete_writable_state",
        ),
    ],
)
async def test_write_validation_errors(hass, status, kwargs, key) -> None:
    """Invalid write states use stable translated validation keys."""
    if status is None:
        coordinator, api = coordinator_for_write(
            hass, AmbientikaStatus(serial_number="serial")
        )
        coordinator.data.devices["serial"] = replace(
            coordinator.data.devices["serial"], status=None
        )
    else:
        coordinator, api = coordinator_for_write(hass, status)

    with pytest.raises(ServiceValidationError) as error:
        await coordinator.async_write_state("serial", **kwargs)

    assert error.value.translation_key == key
    api.async_change_mode.assert_not_awaited()


async def test_write_and_filter_reset_failures_are_translated(hass) -> None:
    """Cloud write failures become translatable Home Assistant errors."""
    status = AmbientikaStatus(
        serial_number="serial",
        operating_mode="Auto",
        fan_speed="Low",
        humidity_level="Normal",
        light_sensor_level="Low",
        filter_status="Bad",
    )
    coordinator, api = coordinator_for_write(hass, status)

    api.async_reset_filter.side_effect = AmbientikaApiError("offline")
    with pytest.raises(HomeAssistantError) as reset_error:
        await coordinator.async_reset_filter("serial")
    assert reset_error.value.translation_key == "filter_reset_failed"

    coordinator.data.devices["serial"] = replace(
        coordinator.data.devices["serial"],
        status=replace(status, filter_status="Good"),
    )
    api.async_change_mode.side_effect = AmbientikaApiError("offline")
    with pytest.raises(HomeAssistantError) as write_error:
        await coordinator.async_write_state("serial", operating_mode="Night")
    assert write_error.value.translation_key == "cloud_command_failed"


async def test_filter_reset_updates_data_and_auth_failure_reauthenticates(hass) -> None:
    """A filter reset reads state back and propagates authentication expiry."""
    status = AmbientikaStatus(serial_number="serial", filter_status="Bad")
    coordinator, api = coordinator_for_write(hass, status)
    api.async_device_status.return_value = {
        "deviceSerialNumber": "serial",
        "filtersStatus": "Good",
    }

    await coordinator.async_reset_filter("serial")
    assert coordinator.data.devices["serial"].status.filter_status == "Good"

    api.async_reset_filter.side_effect = AmbientikaAuthError
    with pytest.raises(ConfigEntryAuthFailed):
        await coordinator.async_reset_filter("serial")
