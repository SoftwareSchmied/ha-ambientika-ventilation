"""Data models and tolerant parsers for Ambientika API responses."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
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

DAYS_OF_WEEK = (
    "Sunday",
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
)
DEVICE_ROLES = (
    "Master",
    "SlaveEqualMaster",
    "SlaveOppositeMaster",
    "NotConfigured",
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
    zone_id: int | None = None
    zone_index: int | None = None
    installation: datetime | None = None
    house_id: int | None = None
    house_name: str | None = None
    room_id: int | None = None
    room_name: str | None = None
    zone_name: str | None = None
    radio_firmware: str | None = None
    micro_firmware: str | None = None
    radio_at_firmware: str | None = None
    house_address: str | None = None
    house_latitude: float | None = None
    house_longitude: float | None = None
    house_timezone: int | None = None
    house_iana_timezone: str | None = None
    house_current_time: str | None = None
    house_zones_count: int | None = None
    house_devices_count: int | None = None
    room_devices_count: int | None = None


@dataclass(frozen=True, slots=True)
class AmbientikaStatus:
    """Dynamic status reported by one Ambientika unit."""

    serial_number: str
    packet_type: str | None = None
    device_type: str | None = None
    device_subtype: str | None = None
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
class AmbientikaTimeSlot:
    """One weekly schedule time slot."""

    slot_id: int | None = None
    day_of_week: str | None = None
    start_time: str | None = None
    end_time: str | None = None
    operating_mode: str | None = None
    fan_speed: str | None = None
    humidity_level: str | None = None
    light_sensor_level: str | None = None
    schedule_id: int | None = None


@dataclass(frozen=True, slots=True)
class AmbientikaSchedule:
    """Read-only weekly schedule information for one device."""

    schedule_id: int | None = None
    zone_id: int | None = None
    house_id: int | None = None
    device_id: int | None = None
    time_slots: tuple[AmbientikaTimeSlot, ...] = ()


@dataclass(frozen=True, slots=True)
class AmbientikaDeviceData:
    """Combined static and dynamic state for an Ambientika unit."""

    device: AmbientikaDevice
    status: AmbientikaStatus | None = None
    schedule: AmbientikaSchedule | None = None


@dataclass(slots=True)
class AmbientikaData:
    """Coordinator data for all devices on one account."""

    devices: dict[str, AmbientikaDeviceData] = field(default_factory=dict)
    feature_flags: dict[str, bool] = field(default_factory=dict)
    failed_devices: frozenset[str] = field(default_factory=frozenset)


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


def _number(value: object) -> float | None:
    """Return a floating-point number without accepting booleans."""
    if isinstance(value, int | float) and not isinstance(value, bool):
        return float(value)
    return None


def _boolean(value: object) -> bool | None:
    """Return a boolean value or None."""
    return value if isinstance(value, bool) else None


def _datetime(value: object) -> datetime | None:
    """Parse an ISO timestamp without rejecting a missing timezone."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _enum(value: object, known_values: tuple[str, ...]) -> str | None:
    """Parse string or legacy numeric enums without rejecting new strings."""
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, int) and not isinstance(value, bool):
        return known_values[value] if 0 <= value < len(known_values) else None
    return None


def _device_role(value: object) -> str | None:
    """Normalize documented and legacy not-configured role representations."""
    if value in (-1, "NC"):
        return "NotConfigured"
    return _enum(value, DEVICE_ROLES)


def _day_of_week(value: object) -> str | None:
    """Normalize both app numeric and OpenAPI string weekday representations."""
    if isinstance(value, int) and not isinstance(value, bool):
        return DAYS_OF_WEEK[value] if 0 <= value < len(DAYS_OF_WEEK) else None
    text = _text(value)
    if text is None:
        return None
    if text.isdigit():
        index = int(text)
        return DAYS_OF_WEEK[index] if 0 <= index < len(DAYS_OF_WEEK) else None
    for day in DAYS_OF_WEEK:
        if day.casefold() == text.casefold():
            return day
    return text


def parse_houses(
    payload: object, house_metadata: object = None
) -> dict[str, AmbientikaDevice]:
    """Extract devices from current and legacy house payload shapes."""
    devices: dict[str, AmbientikaDevice] = {}
    metadata_by_id: dict[int, dict[str, Any]] = {}
    for metadata_value in _list(house_metadata):
        metadata = _mapping(metadata_value)
        metadata_id = _integer(metadata.get("id")) or _integer(metadata.get("houseId"))
        if metadata_id is not None:
            metadata_by_id[metadata_id] = metadata

    def add_device(
        raw_value: object,
        *,
        house_id: int | None,
        house_name: str | None,
        house_metadata_value: dict[str, Any],
        house_zones_count: int | None,
        house_devices_count: int | None,
        zone_id: int | None = None,
        room_name: str | None = None,
        room_id: int | None = None,
        room_devices_count: int | None = None,
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
            role=_device_role(raw.get("role")),
            zone_id=zone_id,
            zone_index=_integer(raw.get("zoneIndex")),
            installation=_datetime(raw.get("installation")),
            house_id=house_id,
            house_name=house_name,
            room_id=_integer(raw.get("roomId")) or room_id,
            room_name=room_name,
            zone_name=zone_name,
            radio_firmware=_text(raw.get("radioFwVersion")),
            micro_firmware=_text(raw.get("microFwVersion")),
            radio_at_firmware=_text(raw.get("radioAtCommandsFwVersion")),
            house_address=_text(house_metadata_value.get("address")),
            house_latitude=_number(house_metadata_value.get("latitude")),
            house_longitude=_number(house_metadata_value.get("longitude")),
            house_timezone=_integer(house_metadata_value.get("timezone")),
            house_iana_timezone=_text(house_metadata_value.get("ianaTimezone")),
            house_current_time=_text(house_metadata_value.get("currentHouseTime")),
            house_zones_count=house_zones_count,
            house_devices_count=house_devices_count,
            room_devices_count=room_devices_count,
        )

    def add_room(
        raw_value: object,
        *,
        house_id: int | None,
        house_name: str | None,
        house_metadata_value: dict[str, Any],
        house_zones_count: int | None,
        house_devices_count: int | None,
        zone_id: int | None = None,
        zone_name: str | None = None,
    ) -> None:
        raw = _mapping(raw_value)
        room_id = _integer(raw.get("id"))
        room_devices_count = _integer(raw.get("roomDevicesCount"))
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
                house_metadata_value=house_metadata_value,
                house_zones_count=house_zones_count,
                house_devices_count=house_devices_count,
                zone_id=zone_id,
                room_name=room_name,
                room_id=room_id,
                room_devices_count=room_devices_count,
                zone_name=zone_name,
            )

    for raw_house_value in _list(payload):
        raw_house = _mapping(raw_house_value)
        house_id = _integer(raw_house.get("houseId")) or _integer(raw_house.get("id"))
        house_name = _text(raw_house.get("houseName")) or _text(raw_house.get("name"))
        metadata = dict(raw_house)
        if house_id is not None:
            metadata.update(metadata_by_id.get(house_id, {}))
        house_zones_count = _integer(raw_house.get("houseZonesCount"))
        house_devices_count = _integer(raw_house.get("houseDevicesCount"))

        for key in ("nonGeminiDevices", "geminiDevices", "devices"):
            for raw_device in _list(raw_house.get(key)):
                add_device(
                    raw_device,
                    house_id=house_id,
                    house_name=house_name,
                    house_metadata_value=metadata,
                    house_zones_count=house_zones_count,
                    house_devices_count=house_devices_count,
                )

        for key in ("roomsWithGeminiDevices", "rooms"):
            for raw_room in _list(raw_house.get(key)):
                add_room(
                    raw_room,
                    house_id=house_id,
                    house_name=house_name,
                    house_metadata_value=metadata,
                    house_zones_count=house_zones_count,
                    house_devices_count=house_devices_count,
                )

        for raw_zone_value in _list(raw_house.get("nonGeminiZones")) + _list(
            raw_house.get("zones")
        ):
            raw_zone = _mapping(raw_zone_value)
            zone_id = _integer(raw_zone.get("id"))
            zone_name = _text(raw_zone.get("name"))
            for raw_room in _list(raw_zone.get("rooms")):
                add_room(
                    raw_room,
                    house_id=house_id,
                    house_name=house_name,
                    house_metadata_value=metadata,
                    house_zones_count=house_zones_count,
                    house_devices_count=house_devices_count,
                    zone_id=zone_id,
                    zone_name=zone_name,
                )

    return devices


def is_controllable_device(
    device: AmbientikaDevice, status: AmbientikaStatus | None = None
) -> bool:
    """Return whether the app treats a device as a directly controlled unit."""
    role = status.device_role if status and status.device_role else device.role
    return role not in ("SlaveEqualMaster", "SlaveOppositeMaster", "NotConfigured")


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

    signal_strength = _integer(raw.get("signalStrenght"))
    if signal_strength is None:
        signal_strength = _integer(raw.get("signalStrength"))

    return AmbientikaStatus(
        serial_number=serial,
        packet_type=_enum(
            raw.get("packetType"),
            (
                "Connection",
                "Status",
                "Command",
                "FwVersions",
                "OutsideWeatherRequest",
                "Unknown",
            ),
        ),
        device_type=_enum(
            raw.get("deviceType"), ("Ghost", "Diamond", "Icon", "Gemini")
        ),
        device_subtype=_enum(
            raw.get("deviceSubtype"),
            ("None", "Version100", "Version160", "Version200"),
        ),
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
        device_role=_device_role(raw.get("deviceRole")),
        last_operating_mode=_enum(raw.get("lastOperatingMode"), OPERATING_MODES),
        signal_strength=signal_strength,
        schedule_state=_enum(raw.get("isScheduled"), SCHEDULE_STATES),
        turbo_available=_boolean(raw.get("isTurboAvailable")) is True,
    )


def parse_house_statuses(payload: object) -> dict[str, AmbientikaStatus]:
    """Extract all device status packets from a house batch response."""
    raw = _mapping(payload)
    packets: list[object] = []
    packets.extend(
        _mapping(item).get("statusPacket") for item in _list(raw.get("zoneDevicesInfo"))
    )
    packets.extend(
        _mapping(item).get("statusPacket")
        for item in _list(raw.get("geminiDevicesInfo"))
    )
    packets.append(raw.get("uniqueZoneStatusPacket"))

    statuses: dict[str, AmbientikaStatus] = {}
    for packet in packets:
        packet_mapping = _mapping(packet)
        serial = _text(packet_mapping.get("deviceSerialNumber"))
        if serial is not None:
            statuses[serial] = parse_status(packet_mapping, serial)
    return statuses


def parse_schedule(payload: object) -> AmbientikaSchedule:
    """Parse a read-only device schedule and its weekly time slots."""
    raw = _mapping(payload)
    slots: list[AmbientikaTimeSlot] = []
    for value in _list(raw.get("timeSlots")):
        slot = _mapping(value)
        slots.append(
            AmbientikaTimeSlot(
                slot_id=_integer(slot.get("id")),
                day_of_week=_day_of_week(slot.get("dayOfWeek")),
                start_time=_text(slot.get("startTime")),
                end_time=_text(slot.get("endTime")),
                operating_mode=_enum(slot.get("operatingMode"), OPERATING_MODES),
                fan_speed=_enum(slot.get("fanSpeed"), FAN_SPEEDS),
                humidity_level=_enum(slot.get("humidityLevel"), HUMIDITY_LEVELS),
                light_sensor_level=_enum(
                    slot.get("lightSensorLevel"), LIGHT_SENSOR_LEVELS
                ),
                schedule_id=_integer(slot.get("scheduleId")),
            )
        )
    return AmbientikaSchedule(
        schedule_id=_integer(raw.get("id")),
        zone_id=_integer(raw.get("zoneId")),
        house_id=_integer(raw.get("houseId")),
        device_id=_integer(raw.get("deviceId")),
        time_slots=tuple(slots),
    )
