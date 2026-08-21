"""Data models and tolerant parsers for Ambientika API responses."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .const import (
    AIR_QUALITY_LEVELS,
    FAN_SPEEDS,
    FILTER_STATUSES,
    HUMIDITY_LEVELS,
    LIGHT_SENSOR_LEVELS,
    OPERATING_MODES,
    SCHEDULE_STATES,
)


@dataclass(frozen=True, slots=True)
class AmbientikaDevice:
    """Static metadata describing one Ambientika ventilation unit."""

    serial_number: str
    name: str
    device_id: int | None = None
    device_type: str | None = None
    device_subtype: str | None = None
    role: str | None = None
    house_id: int | None = None
    house_name: str | None = None
    room_name: str | None = None
    zone_name: str | None = None
    radio_firmware: str | None = None
    micro_firmware: str | None = None


@dataclass(frozen=True, slots=True)
class AmbientikaStatus:
    """Dynamic status reported by one Ambientika unit."""

    serial_number: str
    operating_mode: str | None = None
    fan_speed: str | None = None
    humidity_level: str | None = None
    light_sensor_level: str | None = None
    temperature: int | None = None
    humidity: int | None = None
    air_quality: str | None = None
    humidity_alarm: bool | None = None
    filter_status: str | None = None
    night_alarm: bool | None = None
    device_role: str | None = None
    last_operating_mode: str | None = None
    signal_strength: int | None = None
    schedule_state: str | None = None
    turbo_available: bool = False


@dataclass(frozen=True, slots=True)
class AmbientikaDeviceData:
    """Combined static and dynamic state for an Ambientika unit."""

    device: AmbientikaDevice
    status: AmbientikaStatus | None = None


@dataclass(slots=True)
class AmbientikaData:
    """Coordinator data for all devices on one account."""

    devices: dict[str, AmbientikaDeviceData] = field(default_factory=dict)
    feature_flags: dict[str, bool] = field(default_factory=dict)


def _mapping(value: object) -> dict[str, Any]:
    """Return a mapping or an empty mapping for malformed payloads."""
    return value if isinstance(value, dict) else {}


def _list(value: object) -> list[object]:
    """Return a list or an empty list for malformed payloads."""
    return value if isinstance(value, list) else []


def _text(value: object) -> str | None:
    """Return a non-empty string representation when safe."""
    if isinstance(value, str):
        value = value.strip()
        return value or None
    return None


def _integer(value: object) -> int | None:
    """Return an integer without accepting booleans."""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _boolean(value: object) -> bool | None:
    """Return a boolean value or None."""
    return value if isinstance(value, bool) else None


def _enum(value: object, known_values: tuple[str, ...]) -> str | None:
    """Parse string or legacy numeric enums without rejecting new strings."""
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, int) and not isinstance(value, bool):
        return known_values[value] if 0 <= value < len(known_values) else None
    return None


def parse_houses(payload: object) -> dict[str, AmbientikaDevice]:
    """Extract devices from current and legacy house payload shapes."""
    devices: dict[str, AmbientikaDevice] = {}

    def add_device(
        raw_value: object,
        *,
        house_id: int | None,
        house_name: str | None,
        room_name: str | None = None,
        zone_name: str | None = None,
    ) -> None:
        raw = _mapping(raw_value)
        serial = _text(raw.get("serialNumber"))
        if serial is None:
            return
        devices[serial] = AmbientikaDevice(
            serial_number=serial,
            name=_text(raw.get("name")) or f"Ambientika {serial[-4:]}",
            device_id=_integer(raw.get("id")),
            device_type=_enum(
                raw.get("deviceType"), ("Ghost", "Diamond", "Icon", "Gemini")
            ),
            device_subtype=_enum(
                raw.get("deviceSubtype"),
                ("None", "Version100", "Version160", "Version200"),
            ),
            role=_enum(
                raw.get("role"),
                ("Master", "SlaveEqualMaster", "SlaveOppositeMaster", "NotConfigured"),
            ),
            house_id=house_id,
            house_name=house_name,
            room_name=room_name,
            zone_name=zone_name,
            radio_firmware=_text(raw.get("radioFwVersion")),
            micro_firmware=_text(raw.get("microFwVersion")),
        )

    def add_room(
        raw_value: object,
        *,
        house_id: int | None,
        house_name: str | None,
        zone_name: str | None = None,
    ) -> None:
        raw = _mapping(raw_value)
        room_name = _enum(
            raw.get("name"),
            (
                "Kitchen",
                "LivingRoom",
                "Bedroom",
                "Bathroom",
                "DinningRoom",
                "ChildrenRoom",
                "Bathroom2",
                "Bathroom3",
                "Bedroom2",
                "Bedroom3",
                "Bedroom4",
                "Study",
                "Laundry",
                "Garage",
                "Basement",
                "Attic",
                "GenericRoom1",
                "GenericRoom2",
            ),
        )
        for raw_device in _list(raw.get("devices")):
            add_device(
                raw_device,
                house_id=house_id,
                house_name=house_name,
                room_name=room_name,
                zone_name=zone_name,
            )

    for raw_house_value in _list(payload):
        raw_house = _mapping(raw_house_value)
        house_id = _integer(raw_house.get("houseId")) or _integer(raw_house.get("id"))
        house_name = _text(raw_house.get("houseName")) or _text(raw_house.get("name"))

        for key in ("nonGeminiDevices", "geminiDevices", "devices"):
            for raw_device in _list(raw_house.get(key)):
                add_device(raw_device, house_id=house_id, house_name=house_name)

        for key in ("roomsWithGeminiDevices", "rooms"):
            for raw_room in _list(raw_house.get(key)):
                add_room(raw_room, house_id=house_id, house_name=house_name)

        for raw_zone_value in _list(raw_house.get("nonGeminiZones")) + _list(
            raw_house.get("zones")
        ):
            raw_zone = _mapping(raw_zone_value)
            zone_name = _text(raw_zone.get("name"))
            for raw_room in _list(raw_zone.get("rooms")):
                add_room(
                    raw_room,
                    house_id=house_id,
                    house_name=house_name,
                    zone_name=zone_name,
                )

    return devices


def parse_status(payload: object, fallback_serial: str) -> AmbientikaStatus:
    """Parse a status payload while accepting missing and unknown values."""
    raw = _mapping(payload)
    serial = _text(raw.get("deviceSerialNumber")) or fallback_serial
    humidity = _integer(raw.get("humidity"))
    if humidity is not None and not 0 <= humidity <= 100:
        humidity = None

    temperature = _integer(raw.get("temperature"))
    if temperature is not None and not -60 <= temperature <= 100:
        temperature = None

    return AmbientikaStatus(
        serial_number=serial,
        operating_mode=_enum(raw.get("operatingMode"), OPERATING_MODES),
        fan_speed=_enum(raw.get("fanSpeed"), FAN_SPEEDS),
        humidity_level=_enum(raw.get("humidityLevel"), HUMIDITY_LEVELS),
        light_sensor_level=_enum(raw.get("lightSensorLevel"), LIGHT_SENSOR_LEVELS),
        temperature=temperature,
        humidity=humidity,
        air_quality=_enum(raw.get("airQuality"), AIR_QUALITY_LEVELS),
        humidity_alarm=_boolean(raw.get("humidityAlarm")),
        filter_status=_enum(raw.get("filtersStatus"), FILTER_STATUSES),
        night_alarm=_boolean(raw.get("nightAlarm")),
        device_role=_text(raw.get("deviceRole")),
        last_operating_mode=_enum(raw.get("lastOperatingMode"), OPERATING_MODES),
        signal_strength=_integer(raw.get("signalStrenght")),
        schedule_state=_enum(raw.get("isScheduled"), SCHEDULE_STATES),
        turbo_available=_boolean(raw.get("isTurboAvailable")) is True,
    )
