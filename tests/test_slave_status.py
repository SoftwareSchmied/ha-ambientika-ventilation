"""Regression tests for per-device read-only slave status polling."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call

import pytest
from custom_components.ambientika_ventilation.api import (
    AmbientikaApiError,
    AmbientikaAuthError,
    AmbientikaForbiddenError,
    AmbientikaNotFoundError,
    AmbientikaRateLimitError,
    AmbientikaResponseError,
    AmbientikaServerError,
)
from custom_components.ambientika_ventilation.binary_sensor import (
    async_setup_entry as setup_binary_sensors,
)
from custom_components.ambientika_ventilation.button import (
    async_setup_entry as setup_buttons,
)
from custom_components.ambientika_ventilation.const import DISCOVERY_INTERVAL, DOMAIN
from custom_components.ambientika_ventilation.coordinator import AmbientikaCoordinator
from custom_components.ambientika_ventilation.fan import async_setup_entry as setup_fans
from custom_components.ambientika_ventilation.models import parse_houses
from custom_components.ambientika_ventilation.select import (
    async_setup_entry as setup_selects,
)
from custom_components.ambientika_ventilation.sensor import AmbientikaSensor
from custom_components.ambientika_ventilation.sensor import (
    async_setup_entry as setup_sensors,
)
from custom_components.ambientika_ventilation.switch import (
    async_setup_entry as setup_switches,
)
from homeassistant.exceptions import ConfigEntryAuthFailed, ServiceValidationError
from pytest_homeassistant_custom_component.common import MockConfigEntry

MASTER = "AABBCCDDEEFF"
SLAVE = "FFEEDDCCBBAA"


@pytest.fixture
def slave_coordinator(hass):
    """Provide a house batch containing only the master and a readable slave."""
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    api = AsyncMock()
    api.async_houses_info.return_value = [
        {
            "houseId": 10,
            "devices": [
                {"id": 1, "serialNumber": MASTER, "name": "Master", "role": "Master"},
                {
                    "id": 2,
                    "serialNumber": SLAVE,
                    "name": "Slave",
                    "role": "SlaveEqualMaster",
                },
            ],
        }
    ]
    api.async_houses.return_value = []
    api.async_feature_flags.return_value = {"weeklyScheduler": False}
    api.async_house_devices_status.return_value = {
        "zoneDevicesInfo": [
            {"statusPacket": {"deviceSerialNumber": MASTER, "temperature": 21}}
        ]
    }
    api.async_device_status.return_value = {
        "deviceSerialNumber": SLAVE,
        "deviceRole": "SlaveEqualMaster",
        "temperature": 17,
        "humidity": 64,
        "nightAlarm": True,
    }
    coordinator = AmbientikaCoordinator(hass, entry, api)
    coordinator._known_devices = parse_houses(api.async_houses_info.return_value)
    coordinator._last_discovery = datetime.now(UTC)
    return coordinator, api


@pytest.mark.parametrize("role", ["SlaveEqualMaster", "SlaveOppositeMaster"])
@pytest.mark.parametrize("in_batch", [False, True])
async def test_slave_readings_are_per_device_and_batch_preferred(
    slave_coordinator, role, in_batch
) -> None:
    coordinator, api = slave_coordinator
    coordinator._known_devices[SLAVE] = replace(
        coordinator._known_devices[SLAVE], role=role
    )
    api.async_device_status.return_value["deviceRole"] = role
    if in_batch:
        api.async_house_devices_status.return_value["zoneDevicesInfo"].append(
            {"statusPacket": api.async_device_status.return_value}
        )

    await coordinator.async_refresh()

    assert coordinator.data.devices[SLAVE].status.temperature == 17
    assert coordinator.data.devices[SLAVE].status.humidity == 64
    assert coordinator.data.devices[MASTER].status.temperature == 21
    assert not coordinator.data.failed_devices
    api.async_house_devices_status.assert_awaited_once_with(10)
    if in_batch:
        api.async_device_status.assert_not_awaited()
    else:
        api.async_device_status.assert_awaited_once_with(SLAVE)


@pytest.mark.parametrize("error", [AmbientikaForbiddenError, AmbientikaNotFoundError])
async def test_unsupported_slave_is_retried_after_discovery(slave_coordinator, error):
    coordinator, api = slave_coordinator
    api.async_device_status.side_effect = error
    await coordinator.async_refresh()
    await coordinator.async_refresh()

    api.async_device_status.assert_awaited_once_with(SLAVE)
    assert coordinator.data.failed_devices == frozenset({SLAVE})
    assert coordinator.last_update_success
    assert coordinator.data.devices[MASTER].status.temperature == 21

    api.async_device_status.side_effect = None
    coordinator._last_discovery -= DISCOVERY_INTERVAL
    await coordinator.async_refresh()

    assert api.async_device_status.await_count == 2
    assert not coordinator.data.failed_devices
    assert coordinator.data.devices[SLAVE].status.temperature == 17
    assert not coordinator._unsupported_status


async def test_batch_can_recover_unsupported_slave_without_extra_request(
    slave_coordinator,
) -> None:
    coordinator, api = slave_coordinator
    api.async_device_status.side_effect = AmbientikaNotFoundError
    await coordinator.async_refresh()
    api.async_house_devices_status.return_value["zoneDevicesInfo"].append(
        {"statusPacket": api.async_device_status.return_value}
    )

    await coordinator.async_refresh()

    api.async_device_status.assert_awaited_once_with(SLAVE)
    assert not coordinator.data.failed_devices
    assert not coordinator._unsupported_status
    assert coordinator.data.devices[SLAVE].status.temperature == 17


@pytest.mark.parametrize(
    "error",
    [AmbientikaApiError, AmbientikaRateLimitError, AmbientikaServerError],
)
async def test_temporary_slave_failure_retains_data_and_recovers(
    slave_coordinator, error
) -> None:
    coordinator, api = slave_coordinator
    await coordinator.async_refresh()
    api.async_device_status.side_effect = error
    await coordinator.async_refresh()

    assert coordinator.last_update_success
    assert coordinator.data.failed_devices == frozenset({SLAVE})
    assert coordinator.data.devices[SLAVE].status.temperature == 17
    assert not coordinator._unsupported_status

    api.async_device_status.side_effect = None
    api.async_device_status.return_value["temperature"] = 19
    await coordinator.async_refresh()

    assert not coordinator.data.failed_devices
    assert coordinator.data.devices[SLAVE].status.temperature == 19
    assert api.async_device_status.await_count == 3


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"deviceSerialNumber": SLAVE, "deviceRole": "SlaveEqualMaster"},
        {"deviceSerialNumber": MASTER, "temperature": 21},
        {"deviceSerialNumber": SLAVE, "temperature": "bad", "humidity": 999},
    ],
)
async def test_missing_malformed_or_foreign_status_does_not_create_slave_readings(
    slave_coordinator, payload
) -> None:
    coordinator, api = slave_coordinator
    api.async_device_status.return_value = payload
    await coordinator.async_refresh()

    assert coordinator.last_update_success
    assert coordinator.data.devices[SLAVE].status is None
    assert coordinator.data.failed_devices == frozenset({SLAVE})
    assert coordinator.data.devices[MASTER].status.temperature == 21
    assert not coordinator._unsupported_status


@pytest.mark.parametrize("values", [{"humidity": 0}, {"nightAlarm": False}])
async def test_partial_slave_packet_accepts_zero_and_false(slave_coordinator, values):
    coordinator, api = slave_coordinator
    api.async_device_status.return_value = values
    await coordinator.async_refresh()

    status = coordinator.data.devices[SLAVE].status
    assert status.serial_number == SLAVE
    assert status.temperature is None
    assert status.humidity == values.get("humidity")
    assert status.night_alarm == values.get("nightAlarm")
    assert not coordinator.data.failed_devices


async def test_empty_slave_batch_packet_uses_individual_fallback(slave_coordinator):
    coordinator, api = slave_coordinator
    api.async_house_devices_status.return_value["zoneDevicesInfo"].append(
        {"statusPacket": {"deviceSerialNumber": SLAVE}}
    )
    await coordinator.async_refresh()

    api.async_device_status.assert_awaited_once_with(SLAVE)
    assert coordinator.data.devices[SLAVE].status.temperature == 17


async def test_slave_authentication_failure_requires_reauth(slave_coordinator):
    coordinator, api = slave_coordinator
    api.async_device_status.side_effect = AmbientikaAuthError
    with pytest.raises(ConfigEntryAuthFailed):
        await coordinator._async_update_data()


async def test_removed_unsupported_slave_does_not_poison_future_updates(
    slave_coordinator,
) -> None:
    coordinator, api = slave_coordinator
    api.async_device_status.side_effect = AmbientikaNotFoundError
    await coordinator.async_refresh()
    api.async_houses_info.return_value[0]["devices"].pop()
    coordinator._last_discovery -= DISCOVERY_INTERVAL
    await coordinator.async_refresh()

    assert coordinator.last_update_success
    assert set(coordinator.data.devices) == {MASTER}
    assert not coordinator.data.failed_devices
    api.async_device_status.assert_awaited_once_with(SLAVE)


async def test_not_configured_device_is_not_polled_or_given_stale_status(
    slave_coordinator,
) -> None:
    coordinator, api = slave_coordinator
    await coordinator.async_refresh()
    coordinator._known_devices[SLAVE] = replace(
        coordinator._known_devices[SLAVE], role="NotConfigured"
    )
    api.async_house_devices_status.return_value["zoneDevicesInfo"].append(
        {"statusPacket": api.async_device_status.return_value}
    )
    await coordinator.async_refresh()

    assert coordinator.data.devices[SLAVE].status is None
    assert not coordinator.data.failed_devices
    api.async_device_status.assert_awaited_once_with(SLAVE)


async def test_schedule_discovery_remains_master_only(slave_coordinator):
    coordinator, api = slave_coordinator
    api.async_feature_flags.return_value = {"weeklyScheduler": True}
    api.async_schedule.return_value = {"timeSlots": []}
    await coordinator._async_discover()

    api.async_schedule.assert_awaited_once_with(1)


@pytest.mark.parametrize("role", ["SlaveEqualMaster", "SlaveOppositeMaster"])
@pytest.mark.parametrize("status_role", [None, "Master", "SlaveEqualMaster"])
async def test_slave_platforms_remain_read_only_with_conflicting_roles(
    hass, slave_coordinator, role, status_role
) -> None:
    coordinator, api = slave_coordinator
    coordinator._known_devices[SLAVE] = replace(
        coordinator._known_devices[SLAVE], role=role
    )
    api.async_device_status.return_value.update(
        {
            "deviceRole": status_role,
            "operatingMode": "ManualHeatRecovery",
            "fanSpeed": "High",
            "humidityLevel": "Normal",
            "lightSensorLevel": "Low",
            "isScheduled": "Off",
            "filtersStatus": "Bad",
        }
    )
    await coordinator.async_refresh()
    # Exercise dynamic platform discovery without scheduling real polling timers.
    coordinator.async_add_listener = MagicMock(return_value=MagicMock())
    entry = SimpleNamespace(
        runtime_data=SimpleNamespace(coordinator=coordinator),
        async_on_unload=MagicMock(),
    )
    for setup in (setup_fans, setup_selects, setup_switches, setup_buttons):
        entities = []
        await setup(hass, entry, entities.extend)
        assert all(entity._serial != SLAVE for entity in entities)

    for setup in (setup_sensors, setup_binary_sensors):
        entities = []
        await setup(hass, entry, entities.extend)
        slave_entities = [entity for entity in entities if entity._serial == SLAVE]
        assert slave_entities
        assert all(entity.available for entity in slave_entities)
        assert all(
            entity.unique_id.startswith(f"{SLAVE}_") for entity in slave_entities
        )
        if setup is setup_sensors:
            temperature = next(
                entity
                for entity in slave_entities
                if entity.entity_description.key == "temperature"
            )
            assert isinstance(temperature, AmbientikaSensor)
            assert temperature.native_value == 17
        else:
            night = next(
                entity
                for entity in slave_entities
                if entity.entity_description.key == "night"
            )
            assert night.is_on is True

    for command in ({"fan_speed": "High"}, {"schedule_mode": True}):
        with pytest.raises(ServiceValidationError) as error:
            await coordinator.async_write_state(SLAVE, **command)
        assert error.value.translation_key == "device_not_controllable"
    with pytest.raises(ServiceValidationError) as error:
        await coordinator.async_reset_filter(SLAVE)
    assert error.value.translation_key == "device_not_controllable"
    api.async_change_mode.assert_not_awaited()
    api.async_reset_filter.assert_not_awaited()


async def test_house_outage_falls_back_to_master_and_slave(slave_coordinator):
    coordinator, api = slave_coordinator
    api.async_house_devices_status.side_effect = AmbientikaServerError
    api.async_device_status.side_effect = lambda serial: {
        "deviceSerialNumber": serial,
        "temperature": 21 if serial == MASTER else 17,
    }
    await coordinator.async_refresh()

    assert not coordinator.data.failed_devices
    assert coordinator.data.devices[MASTER].status.temperature == 21
    assert coordinator.data.devices[SLAVE].status.temperature == 17
    api.async_device_status.assert_has_awaits(
        [call(MASTER), call(SLAVE)], any_order=True
    )


async def test_late_slave_status_adds_entities_once_and_tracks_availability(
    hass, slave_coordinator
) -> None:
    coordinator, api = slave_coordinator
    api.async_device_status.side_effect = AmbientikaApiError
    await coordinator.async_refresh()
    coordinator.async_add_listener = MagicMock(return_value=MagicMock())
    entry = SimpleNamespace(
        runtime_data=SimpleNamespace(coordinator=coordinator),
        async_on_unload=MagicMock(),
    )
    entities = []
    callbacks = []
    for setup in (setup_sensors, setup_binary_sensors):
        await setup(hass, entry, entities.extend)
        callbacks.append(coordinator.async_add_listener.call_args.args[0])
    assert not any(entity.unique_id == f"{SLAVE}_temperature" for entity in entities)

    api.async_device_status.side_effect = None
    await coordinator.async_refresh()
    for callback in callbacks:
        callback()
        callback()
    ids = [entity.unique_id for entity in entities]
    assert len(ids) == len(set(ids))
    temperature = next(
        entity for entity in entities if entity.unique_id == f"{SLAVE}_temperature"
    )
    master_temperature = next(
        entity for entity in entities if entity.unique_id == f"{MASTER}_temperature"
    )
    assert temperature.native_value == 17
    assert temperature.available

    api.async_device_status.side_effect = AmbientikaApiError
    await coordinator.async_refresh()
    assert temperature.native_value == 17
    assert not temperature.available
    assert master_temperature.available

    api.async_device_status.side_effect = None
    api.async_device_status.return_value["temperature"] = 19
    await coordinator.async_refresh()
    for callback in callbacks:
        callback()
    assert temperature.available
    assert temperature.native_value == 19
    assert [entity.unique_id for entity in entities] == ids


async def test_failed_discovery_does_not_restart_unsupported_slave_probes(
    slave_coordinator,
) -> None:
    coordinator, api = slave_coordinator
    api.async_device_status.side_effect = AmbientikaForbiddenError
    await coordinator.async_refresh()
    api.async_houses_info.side_effect = AmbientikaApiError
    coordinator._last_discovery -= DISCOVERY_INTERVAL
    await coordinator.async_refresh()

    assert coordinator.last_update_success
    api.async_device_status.assert_awaited_once_with(SLAVE)
    assert coordinator.data.failed_devices == frozenset({SLAVE})


async def test_mismatched_packet_raises_response_error(slave_coordinator):
    coordinator, api = slave_coordinator
    api.async_device_status.return_value = {
        "deviceSerialNumber": MASTER,
        "temperature": 21,
    }
    with pytest.raises(AmbientikaResponseError):
        await coordinator._async_fetch_one(SLAVE)
