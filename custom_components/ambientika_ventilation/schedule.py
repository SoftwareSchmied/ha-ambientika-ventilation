"""Pure schedule logic: validation, overlap checks, expansion and event text.

Nothing here talks to the network or to Home Assistant. The documented time
slot shape is ``{id, dayOfWeek (0 = Sunday ... 6), startTime "HH:mm:ss",
endTime "HH:mm:ss", operatingMode, fanSpeed, humidityLevel, lightSensorLevel,
scheduleId}``. A slot never crosses midnight and is repeated every week.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .const import (
    API_TO_HA,
    HUMIDITY_LEVELS,
    LIGHT_SENSOR_LEVELS,
    OPERATING_MODES,
    WRITABLE_FAN_SPEEDS,
    mode_allows_setting,
)
from .models import DAYS_OF_WEEK, AmbientikaDevice, AmbientikaTimeSlot

# Defaults for values a caller leaves out when a slot is created.
DEFAULT_FAN_SPEED = "Low"
DEFAULT_HUMIDITY_LEVEL = "Normal"
DEFAULT_LIGHT_SENSOR_LEVEL = "Off"

SLOT_FIELDS = ("operating_mode", "fan_speed", "humidity_level", "light_sensor_level")
HA_TO_API = {value: key for key, value in API_TO_HA.items()}
MODE_OPTIONS = frozenset(API_TO_HA[mode] for mode in OPERATING_MODES)
HA_DAYS = tuple(day.casefold() for day in DAYS_OF_WEEK)

# Event text. The machine readable description lists ``field: option`` lines
# using the option names of the Home Assistant actions.
LABELS: dict[str, dict[str, str]] = {
    "en": {
        "smart": "Smart",
        "auto": "Automatic",
        "heat_recovery": "Heat recovery",
        "night": "Night",
        "away": "Away",
        "surveillance": "Surveillance",
        "timed_extraction": "Timed extraction",
        "extraction": "Extraction",
        "intake": "Intake",
        "master_slave_flow": "Master to slave flow",
        "slave_master_flow": "Slave to master flow",
        "off": "Off",
        "low": "Low",
        "medium": "Medium",
        "high": "High",
        "turbo": "Turbo",
        "dry": "Dry",
        "normal": "Normal",
        "moist": "Moist",
        "not_available": "Not available",
    },
    "de": {
        "smart": "Smart",
        "auto": "Automatik",
        "heat_recovery": "Wärmerückgewinnung",
        "night": "Nacht",
        "away": "Abwesend",
        "surveillance": "Überwachung",
        "timed_extraction": "Zeitgesteuerte Abluft",
        "extraction": "Abluft",
        "intake": "Zuluft",
        "master_slave_flow": "Master zu Slave",
        "slave_master_flow": "Slave zu Master",
        "off": "Aus",
        "low": "Niedrig",
        "medium": "Mittel",
        "high": "Hoch",
        "turbo": "Turbo",
        "dry": "Trocken",
        "normal": "Normal",
        "moist": "Feucht",
        "not_available": "Nicht verfügbar",
    },
}
SUMMARY_SEPARATOR = " · "


class ScheduleValidationError(ValueError):
    """A time slot value was rejected before anything was sent.

    ``code`` is the translation key of the matching Home Assistant error and
    ``placeholders`` the values for its message.
    """

    def __init__(self, code: str, **placeholders: str) -> None:
        """Store the translation key and message placeholders."""
        super().__init__(f"{code}: {placeholders}" if placeholders else code)
        self.code = code
        self.placeholders = placeholders


@dataclass(frozen=True, slots=True)
class SlotSpec:
    """A validated time slot, ready to be sent."""

    day_of_week: int
    start: time
    end: time
    operating_mode: str
    fan_speed: str
    humidity_level: str
    light_sensor_level: str

    def to_payload(self, slot_id: int, schedule_id: int | None) -> dict[str, Any]:
        """Return the request body (``id`` 0 creates a new slot)."""
        return {
            "id": slot_id,
            "dayOfWeek": self.day_of_week,
            "startTime": format_clock(self.start),
            "endTime": format_clock(self.end),
            "operatingMode": self.operating_mode,
            "fanSpeed": self.fan_speed,
            "humidityLevel": self.humidity_level,
            "lightSensorLevel": self.light_sensor_level,
            "scheduleId": schedule_id or 0,
        }


def parse_clock(value: object) -> time | None:
    """Parse ``HH:mm`` or ``HH:mm:ss`` text or a time into a ``time``."""
    if isinstance(value, time):
        return value.replace(tzinfo=None)
    if not isinstance(value, str):
        return None
    parts = value.strip().split(":")
    if len(parts) not in (2, 3) or not all(
        part.isascii() and part.isdigit() for part in parts
    ):
        return None
    hour, minute, second = (int(part) for part in (*parts, "0")[:3])
    try:
        return time(hour, minute, second)
    except ValueError:
        return None


def format_clock(value: time) -> str:
    """Format a time the way the API expects it (seconds are always ``00``)."""
    return f"{value.hour:02d}:{value.minute:02d}:00"


def parse_day(value: object) -> int | None:
    """Return the API weekday (Sunday is 0) for a number or a day name."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if 0 <= value <= 6 else None
    if isinstance(value, str):
        text = value.strip().casefold()
        if text.isascii() and text.isdigit():
            return parse_day(int(text))
        if text in HA_DAYS:
            return HA_DAYS.index(text)
    return None


def _strict_clock(name: str, value: object) -> time:
    """Parse a boundary of a slot, which the API stores with zero seconds."""
    parsed = parse_clock(value)
    if parsed is None or parsed.second or parsed.microsecond:
        raise ScheduleValidationError("schedule_invalid_time", field=name)
    return parsed


def _choice(
    field: str, value: object, choices: tuple[str, ...], *, allowed: bool = True
) -> str:
    """Return ``value`` if it is one of ``choices``."""
    if not isinstance(value, str) or value not in choices or not allowed:
        raise ScheduleValidationError(
            "schedule_invalid_value", field=field, value=str(value)
        )
    return value


def validate_slot(
    *,
    day_of_week: object,
    start_time: object,
    end_time: object,
    operating_mode: object,
    fan_speed: object,
    humidity_level: object,
    light_sensor_level: object,
    allowed_modes: tuple[str, ...] = OPERATING_MODES,
    allow_turbo: bool = False,
) -> SlotSpec:
    """Validate a slot the way the official app builds it.

    Never accepts a value the cloud is not known to take: ``Night`` is not a
    fan speed, Turbo needs the explicit permission of the caller, and the
    operating mode must be one of ``allowed_modes``.
    """
    day = parse_day(day_of_week)
    if day is None:
        raise ScheduleValidationError("schedule_invalid_day", value=str(day_of_week))
    start = _strict_clock("start", start_time)
    end = _strict_clock("end", end_time)
    if start >= end:
        raise ScheduleValidationError("schedule_time_order")
    return SlotSpec(
        day_of_week=day,
        start=start,
        end=end,
        operating_mode=_choice("operating_mode", operating_mode, allowed_modes),
        fan_speed=_choice(
            "fan_speed",
            fan_speed,
            WRITABLE_FAN_SPEEDS,
            allowed=allow_turbo or fan_speed != "Turbo",
        ),
        humidity_level=_choice("humidity_level", humidity_level, HUMIDITY_LEVELS),
        light_sensor_level=_choice(
            "light_sensor_level", light_sensor_level, LIGHT_SENSOR_LEVELS
        ),
    )


def slot_interval(slot: AmbientikaTimeSlot) -> tuple[int, time, time] | None:
    """Return ``(weekday, start, end)`` of a slot, or ``None`` if unreadable."""
    day = parse_day(slot.day_of_week)
    start = parse_clock(slot.start_time)
    end = parse_clock(slot.end_time)
    if day is None or start is None or end is None or start >= end:
        return None
    return day, start, end


def find_overlap(
    spec: SlotSpec,
    slots: Iterable[AmbientikaTimeSlot],
    *,
    ignore_id: int | None = None,
) -> AmbientikaTimeSlot | None:
    """Return a slot on the same day that shares time with ``spec``.

    Slots that merely touch (one ends when the next starts) do not overlap.
    """
    for slot in slots:
        if ignore_id is not None and slot.slot_id == ignore_id:
            continue
        interval = slot_interval(slot)
        if interval is None or interval[0] != spec.day_of_week:
            continue
        if spec.start < interval[2] and interval[1] < spec.end:
            return slot
    return None


def schedule_timezone(device: AmbientikaDevice, default: tzinfo) -> tzinfo:
    """Return the zone slots are read in: the house zone, else ``default``."""
    name = device.house_iana_timezone
    if name:
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError, OSError):
            pass
    return default


def _wall_clock(day: date, clock: time, zone: tzinfo) -> datetime:
    """Return a local wall-clock time as an existing instant in ``zone``.

    A time skipped by the start of daylight saving time moves forward by the
    length of the gap; an ambiguous time takes its first occurrence.
    """
    value = datetime.combine(day, clock, tzinfo=zone)
    return value.astimezone(UTC).astimezone(zone)


def expand_slots(
    slots: Iterable[AmbientikaTimeSlot],
    zone: tzinfo,
    start: datetime,
    end: datetime,
) -> list[tuple[AmbientikaTimeSlot, datetime, datetime]]:
    """Return every occurrence of the weekly slots that touches the range.

    Occurrences are built from the local calendar date and wall-clock times
    of every day in the range, so they keep their local time across daylight
    saving changes and week boundaries.
    """
    first = start.astimezone(zone).date()
    last = end.astimezone(zone).date()
    by_day: dict[int, list[tuple[AmbientikaTimeSlot, time, time]]] = {}
    for slot in slots:
        interval = slot_interval(slot)
        if interval is not None:
            by_day.setdefault(interval[0], []).append((slot, interval[1], interval[2]))

    result: list[tuple[AmbientikaTimeSlot, datetime, datetime]] = []
    day = first
    while day <= last:
        api_day = (day.weekday() + 1) % 7
        for slot, clock_start, clock_end in by_day.get(api_day, ()):
            begin = _wall_clock(day, clock_start, zone)
            finish = _wall_clock(day, clock_end, zone)
            # Compare instants: datetimes sharing a zone compare by wall clock.
            if (
                begin.astimezone(UTC) < finish.astimezone(UTC)
                and begin.astimezone(UTC) < end
                and finish.astimezone(UTC) > start
            ):
                result.append((slot, begin, finish))
        day += timedelta(days=1)
    result.sort(key=lambda item: (item[1].astimezone(UTC), item[0].slot_id or 0))
    return result


def _label(language: str, option: str | None) -> str | None:
    """Return the display text of a Home Assistant option."""
    if option is None:
        return None
    labels = LABELS.get(language, LABELS["en"])
    return labels.get(option, LABELS["en"].get(option, option))


def slot_options(slot: AmbientikaTimeSlot) -> dict[str, str]:
    """Return the fields of a slot as Home Assistant option names."""
    values = {
        "operating_mode": slot.operating_mode,
        "fan_speed": slot.fan_speed,
        "humidity_level": slot.humidity_level,
        "light_sensor_level": slot.light_sensor_level,
    }
    return {
        field: API_TO_HA.get(value, value.casefold())
        for field, value in values.items()
        if value is not None
    }


def event_summary(slot: AmbientikaTimeSlot, language: str) -> str:
    """Return a short title: mode, fan speed and humidity target.

    Only settings the mode takes into account are shown.
    """
    options = slot_options(slot)
    mode = slot.operating_mode
    parts = [_label(language, options.get("operating_mode")) or "?"]
    for field in ("fan_speed", "humidity_level"):
        if field in options and mode_allows_setting(mode, field):
            parts.append(_label(language, options[field]) or "")
    return SUMMARY_SEPARATOR.join(parts)


def event_description(slot: AmbientikaTimeSlot) -> str:
    """Return all fields of a slot as ``field: option`` lines."""
    return "\n".join(f"{field}: {value}" for field, value in slot_options(slot).items())


def parse_event_settings(summary: object, description: object) -> dict[str, str]:
    """Read slot fields (as API values) from an event title and description.

    ``field: option`` lines of the description take precedence; otherwise the
    first part of the title may name the operating mode. Unknown fields are
    ignored, unknown options are rejected.
    """
    found: dict[str, str] = {}
    if isinstance(description, str):
        for line in description.splitlines():
            key, separator, option = line.partition(":")
            key = key.strip().casefold()
            if separator and key in SLOT_FIELDS:
                found[key] = _api_value(key, option.strip().casefold())
    if "operating_mode" not in found and isinstance(summary, str):
        head = summary.split(SUMMARY_SEPARATOR.strip())[0].strip().casefold()
        for labels in LABELS.values():
            for option, label in labels.items():
                if option in MODE_OPTIONS and head in (option, label.casefold()):
                    found["operating_mode"] = _api_value("operating_mode", option)
    return found


def _api_value(field: str, option: str) -> str:
    """Map a Home Assistant option to its API value."""
    value = HA_TO_API.get(option)
    if value is None:
        raise ScheduleValidationError(
            "schedule_invalid_value", field=field, value=option
        )
    return value


def parse_weekly_rrule(rrule: object, api_day: int) -> None:
    """Accept only ``FREQ=WEEKLY`` on the weekday of ``api_day``.

    Anything else (other frequencies, intervals, counts, end dates, several
    days) cannot be stored as a weekly time slot.
    """
    if not isinstance(rrule, str) or not rrule.strip():
        raise ScheduleValidationError("calendar_event_not_weekly")
    rule: dict[str, str] = {}
    for part in rrule.strip().removeprefix("RRULE:").split(";"):
        key, separator, value = part.partition("=")
        if not separator:
            raise ScheduleValidationError("calendar_event_not_weekly")
        rule[key.strip().upper()] = value.strip().upper()
    weekday = ("SU", "MO", "TU", "WE", "TH", "FR", "SA")[api_day]
    if (
        rule.get("FREQ") != "WEEKLY"
        or rule.get("INTERVAL", "1") != "1"
        or set(rule) - {"FREQ", "INTERVAL", "BYDAY"}
        or rule.get("BYDAY", weekday) != weekday
    ):
        raise ScheduleValidationError("calendar_event_not_weekly")


def weekly_rrule(api_day: int) -> str:
    """Return the recurrence rule of a slot on ``api_day``."""
    weekday = ("SU", "MO", "TU", "WE", "TH", "FR", "SA")[api_day]
    return f"FREQ=WEEKLY;BYDAY={weekday}"
