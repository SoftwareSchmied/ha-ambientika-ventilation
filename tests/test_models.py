"""Tests for tolerant Ambientika payload parsing."""

from __future__ import annotations

from custom_components.ambientika_ventilation.models import (
    parse_house_statuses,
    parse_houses,
    parse_schedule,
    parse_status,
)


def test_parse_houses_discovers_multiple_shapes(houses_payload) -> None:
    """Devices nested in zones and direct Gemini lists are both discovered."""
    devices = parse_houses(
        houses_payload,
        [
            {
                "id": 10,
                "address": "Test street 1",
                "latitude": 50.1,
                "longitude": 8.4,
                "timezone": 2,
                "ianaTimezone": "Europe/Berlin",
                "currentHouseTime": "2026-08-22T12:00:00+02:00",
            }
        ],
    )

    assert set(devices) == {"AABBCCDDEEFF", "112233445566"}
    assert devices["AABBCCDDEEFF"].room_name == "LivingRoom"
    assert devices["AABBCCDDEEFF"].zone_name == "Ground floor"
    assert devices["AABBCCDDEEFF"].zone_id == 301
    assert devices["AABBCCDDEEFF"].room_id == 201
    assert devices["AABBCCDDEEFF"].radio_at_firmware == "3.4.5"
    assert devices["AABBCCDDEEFF"].installation is not None
    assert devices["AABBCCDDEEFF"].house_address == "Test street 1"
    assert devices["AABBCCDDEEFF"].house_latitude == 50.1
    assert devices["AABBCCDDEEFF"].house_iana_timezone == "Europe/Berlin"
    assert devices["AABBCCDDEEFF"].house_current_time == "2026-08-22T12:00:00+02:00"
    assert devices["AABBCCDDEEFF"].house_devices_count == 2
    assert devices["AABBCCDDEEFF"].room_devices_count == 1
    assert devices["112233445566"].device_type == "Gemini"


def test_parse_houses_ignores_incomplete_values() -> None:
    """Missing collections and serial numbers do not break discovery."""
    assert parse_houses([{"houseId": 1, "nonGeminiDevices": [{"name": "Bad"}]}]) == {}
    assert parse_houses(None) == {}


def test_parse_status_accepts_legacy_numeric_enums(status_payload) -> None:
    """Numeric enum payloads from older deployments remain compatible."""
    status_payload.update(
        {
            "operatingMode": 2,
            "fanSpeed": 1,
            "humidityLevel": 1,
            "lightSensorLevel": 2,
            "deviceRole": -1,
        }
    )
    status = parse_status(status_payload, "fallback")

    assert status.operating_mode == "ManualHeatRecovery"
    assert status.fan_speed == "Medium"
    assert status.light_sensor_level == "Low"
    assert status.device_role == "NotConfigured"


def test_parse_status_accepts_corrected_signal_spelling(status_payload) -> None:
    """A future corrected API spelling remains compatible."""
    status_payload.pop("signalStrenght")
    status_payload["signalStrength"] = 0

    assert parse_status(status_payload, "fallback").signal_strength == 0


def test_parse_status_tolerates_unknown_and_unrealistic_values(status_payload) -> None:
    """Unknown enums survive while unsafe measurements become unavailable."""
    status_payload.update(
        {
            "operatingMode": "FutureMode",
            "temperature": 999,
            "humidity": -1,
            "airQuality": 999,
        }
    )
    status = parse_status(status_payload, "fallback")

    assert status.operating_mode == "FutureMode"
    assert status.temperature is None
    assert status.humidity is None
    assert status.air_quality is None


def test_parse_house_statuses_collects_all_batch_shapes(status_payload) -> None:
    """Zone, unique-zone, and Gemini status packets are all discovered."""
    payload = {
        "zoneDevicesInfo": [{"statusPacket": status_payload}],
        "uniqueZoneStatusPacket": {
            **status_payload,
            "deviceSerialNumber": "112233445566",
        },
        "geminiDevicesInfo": [
            {
                "statusPacket": {
                    **status_payload,
                    "deviceSerialNumber": "FFEEDDCCBBAA",
                }
            }
        ],
    }

    assert set(parse_house_statuses(payload)) == {
        "AABBCCDDEEFF",
        "112233445566",
        "FFEEDDCCBBAA",
    }


def test_parse_schedule_preserves_all_writable_fields() -> None:
    """Weekly schedule details remain available for diagnostic entities."""
    schedule = parse_schedule(
        {
            "id": 7,
            "zoneId": 3,
            "houseId": 4,
            "deviceId": 5,
            "timeSlots": [
                {
                    "id": 8,
                    "dayOfWeek": 1,
                    "startTime": "08:00:00",
                    "endTime": "10:00:00",
                    "operatingMode": "Auto",
                    "fanSpeed": "Night",
                    "humidityLevel": "Normal",
                    "lightSensorLevel": "Low",
                    "scheduleId": 7,
                }
            ],
        }
    )

    assert schedule.schedule_id == 7
    assert schedule.zone_id == 3
    assert schedule.house_id == 4
    assert schedule.device_id == 5
    assert schedule.time_slots[0].day_of_week == "Monday"
    assert schedule.time_slots[0].fan_speed == "Night"
    assert schedule.time_slots[0].schedule_id == 7


def test_parsers_tolerate_dates_and_weekday_variants() -> None:
    """Malformed dates and all observed weekday representations remain safe."""
    devices = parse_houses(
        [
            {
                "id": 1,
                "rooms": [
                    {
                        "devices": [
                            {
                                "serialNumber": "direct-room",
                                "installation": "not-a-date",
                            }
                        ]
                    }
                ],
            }
        ],
        [{}],
    )
    assert devices["direct-room"].installation is None

    schedule = parse_schedule(
        {
            "timeSlots": [
                {"dayOfWeek": "2"},
                {"dayOfWeek": "monday"},
                {"dayOfWeek": "Funday"},
                {"dayOfWeek": ""},
                {"dayOfWeek": 99},
            ]
        }
    )
    assert [slot.day_of_week for slot in schedule.time_slots] == [
        "Tuesday",
        "Monday",
        "Funday",
        None,
        None,
    ]
