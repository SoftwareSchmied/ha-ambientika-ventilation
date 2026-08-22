"""Tests for stable entity identities and capability behavior."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from custom_components.ambientika_ventilation.binary_sensor import (
    BINARY_SENSORS,
    AmbientikaBinarySensor,
)
from custom_components.ambientika_ventilation.binary_sensor import (
    async_setup_entry as async_setup_binary_sensors,
)
from custom_components.ambientika_ventilation.button import (
    AmbientikaResetFilterButton,
)
from custom_components.ambientika_ventilation.button import (
    async_setup_entry as async_setup_buttons,
)
from custom_components.ambientika_ventilation.errors import validation_error
from custom_components.ambientika_ventilation.fan import AmbientikaFan
from custom_components.ambientika_ventilation.fan import (
    async_setup_entry as async_setup_fans,
)
from custom_components.ambientika_ventilation.models import (
    AmbientikaData,
    AmbientikaDevice,
    AmbientikaDeviceData,
    AmbientikaStatus,
)
from custom_components.ambientika_ventilation.select import SELECTS, AmbientikaSelect
from custom_components.ambientika_ventilation.select import (
    async_setup_entry as async_setup_selects,
)
from custom_components.ambientika_ventilation.sensor import (
    DEVICE_SENSORS,
    SENSORS,
    AmbientikaDeviceSensor,
    AmbientikaScheduleSensor,
    AmbientikaSensor,
)
from custom_components.ambientika_ventilation.sensor import (
    async_setup_entry as async_setup_sensors,
)
from custom_components.ambientika_ventilation.switch import AmbientikaScheduleSwitch
from custom_components.ambientika_ventilation.switch import (
    async_setup_entry as async_setup_switches,
)
from homeassistant.components.fan import FanEntityFeature
from homeassistant.exceptions import ServiceValidationError

SERIAL = "AABBCCDDEEFF"


def coordinator_with_status():
    """Return a coordinator-shaped object with one complete device."""
    coordinator = MagicMock()
    coordinator.last_update_success = True
    coordinator.data = AmbientikaData(
        devices={
            SERIAL: AmbientikaDeviceData(
                device=AmbientikaDevice(
                    serial_number=SERIAL,
                    name="Living room",
                    device_type="Diamond",
                    role="Master",
                ),
                status=AmbientikaStatus(
                    serial_number=SERIAL,
                    operating_mode="ManualHeatRecovery",
                    fan_speed="Medium",
                    humidity_level="Normal",
                    light_sensor_level="Low",
                    temperature=21,
                    humidity=48,
                    air_quality="Good",
                    filter_status="Medium",
                    humidity_alarm=False,
                    night_alarm=False,
                    schedule_state="Off",
                    turbo_available=True,
                ),
            )
        }
    )
    coordinator.async_add_listener = MagicMock(return_value=MagicMock())
    coordinator.async_write_state = AsyncMock()
    coordinator.async_reset_filter = AsyncMock()
    return coordinator


def test_entity_unique_ids_are_serial_based() -> None:
    """Renaming a device cannot change published unique IDs."""
    coordinator = coordinator_with_status()
    fan = AmbientikaFan(coordinator, SERIAL)
    temperature = AmbientikaSensor(coordinator, SERIAL, SENSORS[0])

    assert fan.unique_id == f"{SERIAL}_ventilation"
    assert temperature.unique_id == f"{SERIAL}_temperature"
    assert ("ambientika_ventilation", SERIAL) in fan.device_info["identifiers"]


def test_enum_entities_normalize_api_values() -> None:
    """Translated HA options remain separate from API wire values."""
    coordinator = coordinator_with_status()
    operating_mode = AmbientikaSelect(coordinator, SERIAL, SELECTS[0])
    air_quality = AmbientikaSensor(coordinator, SERIAL, SENSORS[2])

    assert operating_mode.current_option == "heat_recovery"
    assert "smart" in operating_mode.options
    assert air_quality.native_value == "good"


def test_fan_maps_turbo_percentage() -> None:
    """Turbo capability dynamically adds the fourth fan step."""
    fan = AmbientikaFan(coordinator_with_status(), SERIAL)

    assert fan.speed_count == 4
    assert fan.percentage == 50
    assert fan._speed_for_percentage(100) == "Turbo"


async def test_fan_represents_night_as_preset() -> None:
    """Night changes only the mode and preserves the reported fan speed."""
    coordinator = coordinator_with_status()
    coordinator.async_write_state = AsyncMock()
    coordinator.data.devices[SERIAL] = replace(
        coordinator.data.devices[SERIAL],
        status=AmbientikaStatus(
            serial_number=SERIAL,
            operating_mode="Night",
            fan_speed="High",
            humidity_level="Normal",
            light_sensor_level="Low",
        ),
    )
    fan = AmbientikaFan(coordinator, SERIAL)

    assert fan.preset_mode == "night"
    assert fan.percentage is None
    assert not fan.supported_features & FanEntityFeature.SET_SPEED
    await fan.async_set_preset_mode("night")
    coordinator.async_write_state.assert_awaited_once_with(
        SERIAL,
        operating_mode="Night",
    )


def test_operating_modes_follow_device_type() -> None:
    """Ghost-style devices expose flow modes while Gemini excludes them and Away."""
    coordinator = coordinator_with_status()
    diamond = AmbientikaSelect(coordinator, SERIAL, SELECTS[0])

    assert "master_slave_flow" in diamond.options
    assert "slave_master_flow" in diamond.options

    coordinator.data.devices[SERIAL] = replace(
        coordinator.data.devices[SERIAL],
        device=replace(
            coordinator.data.devices[SERIAL].device,
            device_type="Gemini",
        ),
    )
    gemini = AmbientikaSelect(coordinator, SERIAL, SELECTS[0])

    assert "away" not in gemini.options
    assert "master_slave_flow" not in gemini.options


async def test_schedule_switch_controls_schedule_mode() -> None:
    """Schedule control uses the dedicated change-mode flag."""
    coordinator = coordinator_with_status()
    coordinator.async_write_state = AsyncMock()
    coordinator.data.devices[SERIAL] = replace(
        coordinator.data.devices[SERIAL],
        status=AmbientikaStatus(
            serial_number=SERIAL,
            operating_mode="Auto",
            fan_speed="Low",
            humidity_level="Normal",
            light_sensor_level="Low",
            schedule_state="On",
        ),
    )
    entity = AmbientikaScheduleSwitch(coordinator, SERIAL)

    assert entity.is_on is True
    await entity.async_turn_off()
    coordinator.async_write_state.assert_awaited_once_with(SERIAL, schedule_mode=False)


def test_device_metadata_sensor_normalizes_role() -> None:
    """Optional static metadata can be exposed as diagnostic entities."""
    coordinator = coordinator_with_status()
    role_description = next(
        item for item in DEVICE_SENSORS if item.key == "device_role"
    )
    entity = AmbientikaDeviceSensor(coordinator, SERIAL, role_description)

    assert entity.native_value == "master"


def test_house_sensor_exposes_complete_topology_metadata() -> None:
    """House details are grouped as disabled diagnostic attributes."""
    coordinator = coordinator_with_status()
    coordinator.data.devices[SERIAL] = replace(
        coordinator.data.devices[SERIAL],
        device=replace(
            coordinator.data.devices[SERIAL].device,
            house_id=10,
            house_name="Home",
            house_address="Test street 1",
            house_latitude=50.1,
            house_longitude=8.4,
            house_timezone=2,
            house_iana_timezone="Europe/Berlin",
            house_devices_count=2,
        ),
    )
    house_description = next(item for item in DEVICE_SENSORS if item.key == "house")
    entity = AmbientikaDeviceSensor(coordinator, SERIAL, house_description)

    assert entity.native_value == "Home"
    assert entity.extra_state_attributes == {
        "house_id": 10,
        "address": "Test street 1",
        "latitude": 50.1,
        "longitude": 8.4,
        "timezone": 2,
        "iana_timezone": "Europe/Berlin",
        "devices_count": 2,
    }


@pytest.mark.parametrize(
    ("description_index", "expected"),
    [(0, False), (1, True), (2, False), (3, False)],
)
def test_binary_sensor_values(description_index: int, expected: bool) -> None:
    """Binary sensor descriptions map status fields consistently."""
    entity = AmbientikaBinarySensor(
        coordinator_with_status(), SERIAL, BINARY_SENSORS[description_index]
    )

    assert entity.is_on is expected


def test_entity_availability_tracks_device_failures_and_removal() -> None:
    """A failed or removed device becomes unavailable without losing identity."""
    coordinator = coordinator_with_status()
    entity = AmbientikaFan(coordinator, SERIAL)

    assert entity.available is True
    coordinator.data.failed_devices = frozenset({SERIAL})
    assert entity.available is False
    coordinator.data.failed_devices = frozenset()
    coordinator.data.devices.clear()
    assert entity.available is False
    assert entity.device_info["name"] == "Living room"


def test_fan_state_and_feature_variants() -> None:
    """Fan state and capabilities follow mode, filter, and schedule state."""
    coordinator = coordinator_with_status()
    fan = AmbientikaFan(coordinator, SERIAL)

    assert fan.is_on is True
    assert fan.supported_features & FanEntityFeature.SET_SPEED

    coordinator.data.devices[SERIAL] = replace(
        coordinator.data.devices[SERIAL],
        status=replace(fan.status, operating_mode="Off", turbo_available=False),
    )
    assert fan.is_on is False
    assert fan.speed_count == 3

    coordinator.data.devices[SERIAL] = replace(
        coordinator.data.devices[SERIAL],
        status=replace(fan.status, filter_status="Bad"),
    )
    assert fan.supported_features == FanEntityFeature(0)


async def test_fan_actions_cover_restore_percentage_and_validation() -> None:
    """Fan actions select safe modes and validate external inputs."""
    coordinator = coordinator_with_status()
    fan = AmbientikaFan(coordinator, SERIAL)

    await fan.async_turn_on(percentage=100)
    coordinator.async_write_state.assert_awaited_with(
        SERIAL, operating_mode="ManualHeatRecovery", fan_speed="Turbo"
    )
    await fan.async_turn_off()
    coordinator.async_write_state.assert_awaited_with(SERIAL, operating_mode="Off")
    await fan.async_set_percentage(0)
    coordinator.async_write_state.assert_awaited_with(SERIAL, operating_mode="Off")
    await fan.async_set_percentage(1)
    coordinator.async_write_state.assert_awaited_with(SERIAL, fan_speed="Low")

    with pytest.raises(ServiceValidationError) as preset_error:
        await fan.async_set_preset_mode("unsupported")
    assert preset_error.value.translation_key == "unsupported_preset_mode"
    with pytest.raises(ServiceValidationError) as percentage_error:
        fan._speed_for_percentage(101)
    assert percentage_error.value.translation_key == "invalid_fan_percentage"


async def test_fan_turn_on_restores_last_supported_mode() -> None:
    """Turning on restores the last supported mode or uses heat recovery."""
    coordinator = coordinator_with_status()
    status = replace(
        coordinator.data.devices[SERIAL].status,
        operating_mode="Off",
        last_operating_mode="Auto",
    )
    coordinator.data.devices[SERIAL] = replace(
        coordinator.data.devices[SERIAL], status=status
    )
    fan = AmbientikaFan(coordinator, SERIAL)

    await fan.async_turn_on()
    coordinator.async_write_state.assert_awaited_with(
        SERIAL, operating_mode="Auto", fan_speed=None
    )

    coordinator.data.devices[SERIAL] = replace(
        coordinator.data.devices[SERIAL],
        status=replace(status, last_operating_mode="AwayHome"),
        device=replace(coordinator.data.devices[SERIAL].device, device_type="Gemini"),
    )
    await fan.async_turn_on()
    coordinator.async_write_state.assert_awaited_with(
        SERIAL, operating_mode="ManualHeatRecovery", fan_speed=None
    )


async def test_select_action_and_unknown_values() -> None:
    """Select entities translate known values and reject foreign options."""
    coordinator = coordinator_with_status()
    entity = AmbientikaSelect(coordinator, SERIAL, SELECTS[0])

    await entity.async_select_option("auto")
    coordinator.async_write_state.assert_awaited_once_with(
        SERIAL, operating_mode="Auto"
    )
    with pytest.raises(ServiceValidationError) as error:
        await entity.async_select_option("future")
    assert error.value.translation_key == "unsupported_select_option"

    coordinator.data.devices[SERIAL] = replace(
        coordinator.data.devices[SERIAL],
        status=replace(coordinator.data.devices[SERIAL].status, operating_mode=None),
    )
    assert entity.current_option is None


async def test_button_and_schedule_switch_actions() -> None:
    """Filter reset and schedule actions delegate to the coordinator."""
    coordinator = coordinator_with_status()
    button = AmbientikaResetFilterButton(coordinator, SERIAL)
    switch = AmbientikaScheduleSwitch(coordinator, SERIAL)

    assert button.available is False
    coordinator.data.devices[SERIAL] = replace(
        coordinator.data.devices[SERIAL],
        status=replace(coordinator.data.devices[SERIAL].status, filter_status="Bad"),
    )
    assert button.available is True
    await button.async_press()
    coordinator.async_reset_filter.assert_awaited_once_with(SERIAL)

    assert switch.is_on is False
    await switch.async_turn_on()
    coordinator.async_write_state.assert_awaited_with(SERIAL, schedule_mode=True)
    await switch.async_turn_off()
    coordinator.async_write_state.assert_awaited_with(SERIAL, schedule_mode=False)


def test_schedule_sensor_and_missing_sensor_values() -> None:
    """Schedule details and absent live readings use valid HA states."""
    from custom_components.ambientika_ventilation.models import (
        AmbientikaSchedule,
        AmbientikaTimeSlot,
    )

    coordinator = coordinator_with_status()
    coordinator.data.devices[SERIAL] = replace(
        coordinator.data.devices[SERIAL],
        status=None,
        schedule=AmbientikaSchedule(
            schedule_id=7,
            time_slots=(AmbientikaTimeSlot(slot_id=8, day_of_week="Monday"),),
        ),
    )
    live_sensor = AmbientikaSensor(coordinator, SERIAL, SENSORS[0])
    schedule_sensor = AmbientikaScheduleSensor(coordinator, SERIAL)

    assert live_sensor.native_value is None
    assert schedule_sensor.native_value == 1
    assert schedule_sensor.extra_state_attributes["time_slots"][0]["id"] == 8


async def test_all_platform_setup_callbacks(hass) -> None:
    """Every platform creates only the entities supported by current data."""
    coordinator = coordinator_with_status()
    entry = SimpleNamespace(
        runtime_data=SimpleNamespace(coordinator=coordinator),
        async_on_unload=MagicMock(),
    )
    setup_functions = (
        async_setup_binary_sensors,
        async_setup_buttons,
        async_setup_fans,
        async_setup_selects,
        async_setup_sensors,
        async_setup_switches,
    )

    counts: list[int] = []
    for setup in setup_functions:
        added: list[object] = []
        await setup(hass, entry, added.extend)
        counts.append(len(added))

    assert counts == [4, 1, 1, 3, 9, 1]
    assert entry.async_on_unload.call_count == len(setup_functions)


def test_error_helpers_attach_translation_metadata() -> None:
    """Action validation errors are ready for frontend translation."""
    error = validation_error("setting_unavailable", placeholders={"mode": "Auto"})

    assert error.translation_domain == "ambientika_ventilation"
    assert error.translation_key == "setting_unavailable"
    assert error.translation_placeholders == {"mode": "Auto"}


def test_entities_handle_missing_and_unknown_live_values() -> None:
    """Entities return unavailable values for incomplete future payloads."""
    coordinator = coordinator_with_status()
    fan = AmbientikaFan(coordinator, SERIAL)
    binary_sensor = AmbientikaBinarySensor(coordinator, SERIAL, BINARY_SENSORS[0])
    switch = AmbientikaScheduleSwitch(coordinator, SERIAL)
    schedule = AmbientikaScheduleSensor(coordinator, SERIAL)

    coordinator.data.devices[SERIAL] = replace(
        coordinator.data.devices[SERIAL],
        status=replace(
            coordinator.data.devices[SERIAL].status,
            operating_mode="FutureMode",
            fan_speed="FutureSpeed",
        ),
    )
    assert fan.percentage is None
    assert fan.preset_mode is None

    coordinator.data.devices[SERIAL] = replace(
        coordinator.data.devices[SERIAL], status=None
    )
    assert fan.is_on is None
    assert fan.percentage is None
    assert fan.preset_mode is None
    assert binary_sensor.is_on is None
    assert switch.is_on is None
    assert schedule.native_value is None
    assert schedule.extra_state_attributes == {}


async def test_fan_turn_on_delegates_preset_and_handles_absent_status() -> None:
    """Turn-on handles preset, automatic-mode speed, and missing status paths."""
    coordinator = coordinator_with_status()
    fan = AmbientikaFan(coordinator, SERIAL)

    await fan.async_turn_on(preset_mode="night")
    coordinator.async_write_state.assert_awaited_with(SERIAL, operating_mode="Night")

    coordinator.async_write_state.reset_mock()
    coordinator.data.devices[SERIAL] = replace(
        coordinator.data.devices[SERIAL],
        status=replace(
            coordinator.data.devices[SERIAL].status,
            operating_mode="Auto",
        ),
    )
    await fan.async_turn_on(percentage=50)
    coordinator.async_write_state.assert_awaited_with(
        SERIAL, operating_mode="ManualHeatRecovery", fan_speed="Medium"
    )

    coordinator.async_write_state.reset_mock()
    coordinator.data.devices[SERIAL] = replace(
        coordinator.data.devices[SERIAL], status=None
    )
    await fan.async_turn_on()
    coordinator.async_write_state.assert_not_awaited()


def test_device_info_and_metadata_optional_branches() -> None:
    """Device registry and diagnostics handle optional metadata correctly."""
    coordinator = coordinator_with_status()
    coordinator.data.devices[SERIAL] = replace(
        coordinator.data.devices[SERIAL],
        device=replace(
            coordinator.data.devices[SERIAL].device,
            device_subtype="Version160",
            micro_firmware="1.0",
        ),
    )
    fan = AmbientikaFan(coordinator, SERIAL)
    assert fan.device_info["model"] == "Diamond Version160"
    assert fan.device_info["hw_version"] == "Version160"
    assert fan.device_info["sw_version"] == "1.0"

    subtype = next(item for item in DEVICE_SENSORS if item.key == "device_subtype")
    entity = AmbientikaDeviceSensor(coordinator, SERIAL, subtype)
    assert entity.available is True
    assert entity.extra_state_attributes is None

    coordinator.data.devices[SERIAL] = replace(
        coordinator.data.devices[SERIAL],
        device=replace(coordinator.data.devices[SERIAL].device, device_subtype=None),
    )
    assert entity.available is False
