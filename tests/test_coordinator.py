"""Tests for coordinator discovery, polling, and writes."""

from __future__ import annotations

from unittest.mock import AsyncMock

from custom_components.ambientika_ventilation.const import DOMAIN
from custom_components.ambientika_ventilation.coordinator import AmbientikaCoordinator
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


async def test_write_preserves_active_schedule(
    hass, houses_payload, status_payload
) -> None:
    """Normal controls do not silently disable an active weekly schedule."""
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    api = AsyncMock()
    api.async_houses_info.return_value = houses_payload
    api.async_feature_flags.return_value = {"weeklyScheduler": False}
    api.async_device_status.return_value = {**status_payload, "isScheduled": "On"}
    coordinator = AmbientikaCoordinator(hass, entry, api)
    coordinator.data = await coordinator._async_update_data()

    await coordinator.async_write_state("AABBCCDDEEFF", humidity_level="Moist")

    assert api.async_change_mode.await_args.kwargs["schedule_mode"] is True
