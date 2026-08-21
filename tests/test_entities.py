"""Tests for stable entity identities and capability behavior."""

from __future__ import annotations

from unittest.mock import MagicMock

from custom_components.ambientika_ventilation.fan import AmbientikaFan
from custom_components.ambientika_ventilation.models import (
    AmbientikaData,
    AmbientikaDevice,
    AmbientikaDeviceData,
    AmbientikaStatus,
)
from custom_components.ambientika_ventilation.select import SELECTS, AmbientikaSelect
from custom_components.ambientika_ventilation.sensor import SENSORS, AmbientikaSensor

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
                    turbo_available=True,
                ),
            )
        }
    )
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
