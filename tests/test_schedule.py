"""Tests for the pure schedule logic."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from custom_components.ambientika_ventilation.models import (
    AmbientikaDevice,
    AmbientikaTimeSlot,
)
from custom_components.ambientika_ventilation.schedule import (
    ScheduleValidationError,
    SlotSpec,
    event_description,
    event_summary,
    expand_slots,
    find_overlap,
    format_clock,
    parse_clock,
    parse_day,
    parse_event_settings,
    parse_weekly_rrule,
    schedule_timezone,
    slot_interval,
    slot_options,
    validate_slot,
    weekly_rrule,
)

BERLIN = ZoneInfo("Europe/Berlin")


def slot(
    slot_id: int | None = 1,
    day: str | None = "Monday",
    start: str | None = "07:00:00",
    end: str | None = "09:00:00",
    mode: str | None = "ManualHeatRecovery",
    fan: str | None = "Medium",
    humidity: str | None = "Normal",
    light: str | None = "Off",
) -> AmbientikaTimeSlot:
    return AmbientikaTimeSlot(
        slot_id=slot_id,
        day_of_week=day,
        start_time=start,
        end_time=end,
        operating_mode=mode,
        fan_speed=fan,
        humidity_level=humidity,
        light_sensor_level=light,
    )


def valid(**changes: object) -> SlotSpec:
    values: dict[str, object] = {
        "day_of_week": "monday",
        "start_time": "07:00",
        "end_time": "09:30:00",
        "operating_mode": "ManualHeatRecovery",
        "fan_speed": "Low",
        "humidity_level": "Normal",
        "light_sensor_level": "Off",
    }
    values.update(changes)
    return validate_slot(**values)  # type: ignore[arg-type]


def code_of(**changes: object) -> ScheduleValidationError:
    with pytest.raises(ScheduleValidationError) as error:
        valid(**changes)
    return error.value


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("07:30", time(7, 30)),
        ("07:30:15", time(7, 30, 15)),
        (" 7:5 ", time(7, 5)),
        (time(8, 0, tzinfo=UTC), time(8, 0)),
        ("24:00", None),
        ("07", None),
        ("07:30:00:00", None),
        ("aa:bb", None),
        ("-1:30", None),
        ("\u0660\u0667:\u0663\u0660", None),
        (None, None),
        (730, None),
        (datetime(2026, 1, 1, 7, 30), None),
    ],
)
def test_parse_clock(value: object, expected: time | None) -> None:
    assert parse_clock(value) == expected


def test_format_clock_always_writes_zero_seconds() -> None:
    assert format_clock(time(7, 5, 59)) == "07:05:00"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0, 0),
        (6, 6),
        ("3", 3),
        ("Sunday", 0),
        ("saturday", 6),
        (" monday ", 1),
        (7, None),
        (-1, None),
        ("9", None),
        ("funday", None),
        (True, None),
        (None, None),
        (1.5, None),
    ],
)
def test_parse_day(value: object, expected: int | None) -> None:
    assert parse_day(value) == expected


def test_valid_slot_is_normalised_and_builds_the_documented_payload() -> None:
    spec = valid()

    assert spec.day_of_week == 1
    assert spec.start == time(7, 0)
    assert spec.end == time(9, 30)
    assert spec.to_payload(0, 5) == {
        "id": 0,
        "dayOfWeek": 1,
        "startTime": "07:00:00",
        "endTime": "09:30:00",
        "operatingMode": "ManualHeatRecovery",
        "fanSpeed": "Low",
        "humidityLevel": "Normal",
        "lightSensorLevel": "Off",
        "scheduleId": 5,
    }
    assert spec.to_payload(9, None)["scheduleId"] == 0


@pytest.mark.parametrize("day", [7, -1, "someday", None, True])
def test_invalid_day_is_rejected(day: object) -> None:
    error = code_of(day_of_week=day)
    assert error.code == "schedule_invalid_day"
    assert error.placeholders == {"value": str(day)}


@pytest.mark.parametrize(
    ("changes", "field"),
    [
        ({"start_time": "07:00:30"}, "start"),
        ({"start_time": "bad"}, "start"),
        ({"start_time": None}, "start"),
        ({"end_time": "09:30:01"}, "end"),
        ({"end_time": time(9, 30, 0, 5)}, "end"),
        ({"end_time": 12}, "end"),
    ],
)
def test_invalid_times_are_rejected(changes: dict[str, object], field: str) -> None:
    error = code_of(**changes)
    assert error.code == "schedule_invalid_time"
    assert error.placeholders == {"field": field}


@pytest.mark.parametrize(
    ("start", "end"),
    [("09:00", "09:00"), ("10:00", "09:00"), ("23:00", "00:00")],
)
def test_start_must_be_before_end_and_not_cross_midnight(start: str, end: str) -> None:
    assert code_of(start_time=start, end_time=end).code == "schedule_time_order"


@pytest.mark.parametrize(
    ("changes", "field", "value"),
    [
        ({"operating_mode": "Turbo"}, "operating_mode", "Turbo"),
        ({"operating_mode": "smart"}, "operating_mode", "smart"),
        ({"operating_mode": None}, "operating_mode", "None"),
        ({"fan_speed": "Night"}, "fan_speed", "Night"),
        ({"fan_speed": "Turbo"}, "fan_speed", "Turbo"),
        ({"fan_speed": 3}, "fan_speed", "3"),
        ({"humidity_level": "Wet"}, "humidity_level", "Wet"),
        ({"light_sensor_level": "Max"}, "light_sensor_level", "Max"),
    ],
)
def test_unknown_values_are_rejected(
    changes: dict[str, object], field: str, value: str
) -> None:
    error = code_of(**changes)
    assert error.code == "schedule_invalid_value"
    assert error.placeholders == {"field": field, "value": value}


def test_turbo_and_mode_restrictions_are_explicit() -> None:
    assert valid(fan_speed="Turbo", allow_turbo=True).fan_speed == "Turbo"
    error = code_of(allowed_modes=("Smart",))
    assert error.code == "schedule_invalid_value"
    assert valid(light_sensor_level="NotAvailable").light_sensor_level == "NotAvailable"
    assert valid(operating_mode="Off").operating_mode == "Off"


def test_error_message_names_the_code() -> None:
    assert str(ScheduleValidationError("schedule_time_order")) == "schedule_time_order"
    assert "x" in str(ScheduleValidationError("c", field="x"))


def test_slot_interval_and_unreadable_slots() -> None:
    assert slot_interval(slot()) == (1, time(7), time(9))
    for broken in (
        slot(day=None),
        slot(day="Noday"),
        slot(start=None),
        slot(end="x"),
        slot(start="09:00:00", end="07:00:00"),
    ):
        assert slot_interval(broken) is None


def test_overlap_detection() -> None:
    existing = [slot(1, "Monday", "07:00:00", "09:00:00"), slot(2, "Tuesday")]

    def spec(start: str, end: str, day: str = "monday") -> SlotSpec:
        return valid(day_of_week=day, start_time=start, end_time=end)

    assert find_overlap(spec("08:00", "10:00"), existing) is existing[0]
    assert find_overlap(spec("06:00", "07:01"), existing) is existing[0]
    assert find_overlap(spec("07:30", "08:00"), existing) is existing[0]
    assert find_overlap(spec("06:00", "10:00"), existing) is existing[0]
    # Touching slots do not overlap.
    assert find_overlap(spec("09:00", "10:00"), existing) is None
    assert find_overlap(spec("06:00", "07:00"), existing) is None
    # Another day is free.
    assert find_overlap(spec("07:00", "09:00", "wednesday"), existing) is None
    # The slot being changed does not collide with itself.
    assert find_overlap(spec("08:00", "10:00"), existing, ignore_id=1) is None
    # Slots that cannot be read are ignored.
    assert find_overlap(spec("08:00", "10:00"), [slot(day=None)]) is None


def test_schedule_timezone_falls_back() -> None:
    fallback = ZoneInfo("America/New_York")

    def zone(name: str | None) -> object:
        return schedule_timezone(
            AmbientikaDevice(serial_number="S", name="n", house_iana_timezone=name),
            fallback,
        )

    assert zone("Europe/Berlin") == BERLIN
    assert zone(None) is fallback
    assert zone("Not/AZone") is fallback
    assert zone("../etc") is fallback


def occurrences(slots, start: datetime, end: datetime, zone=BERLIN):
    return [
        (item.slot_id, begin.isoformat(), finish.isoformat())
        for item, begin, finish in expand_slots(slots, zone, start, end)
    ]


def test_expansion_repeats_weekly_in_local_time() -> None:
    slots = [
        slot(1, "Monday", "07:00:00", "09:00:00"),
        slot(2, "Sunday", "22:00", "23:30"),
    ]
    start = datetime(2026, 6, 1, 0, 0, tzinfo=BERLIN)  # a Monday
    result = occurrences(slots, start, start + timedelta(days=14))

    assert result == [
        (1, "2026-06-01T07:00:00+02:00", "2026-06-01T09:00:00+02:00"),
        (2, "2026-06-07T22:00:00+02:00", "2026-06-07T23:30:00+02:00"),
        (1, "2026-06-08T07:00:00+02:00", "2026-06-08T09:00:00+02:00"),
        (2, "2026-06-14T22:00:00+02:00", "2026-06-14T23:30:00+02:00"),
    ]


def test_week_boundary_sunday_to_monday() -> None:
    """Sunday is day 0 of the API but the last day of the ISO week."""
    slots = [
        slot(1, "Sunday", "00:00:00", "01:00:00"),
        slot(2, "Saturday", "23:00", "23:59"),
    ]
    start = datetime(2026, 6, 6, 12, 0, tzinfo=BERLIN)  # Saturday noon
    result = occurrences(slots, start, datetime(2026, 6, 8, 12, 0, tzinfo=BERLIN))

    assert [item[0] for item in result] == [2, 1]
    assert result[1][1] == "2026-06-07T00:00:00+02:00"


def test_numeric_and_name_days_expand_alike() -> None:
    slots = [slot(1, "Monday"), slot(2, "1")]
    result = occurrences(
        slots,
        datetime(2026, 6, 1, tzinfo=BERLIN),
        datetime(2026, 6, 2, tzinfo=BERLIN),
    )
    assert [item[0] for item in result] == [1, 2]


def test_range_overlap_rules() -> None:
    slots = [slot(1, "Monday", "07:00:00", "09:00:00")]
    monday = datetime(2026, 6, 1, tzinfo=BERLIN)

    def at(hour: int, minute: int = 0) -> datetime:
        return monday.replace(hour=hour, minute=minute)

    # A range inside the slot, and ranges touching its boundaries.
    assert occurrences(slots, at(8), at(8, 30))
    assert occurrences(slots, at(6), at(7, 1))
    assert not occurrences(slots, at(5), at(7))
    assert not occurrences(slots, at(9), at(10))
    # A range given in another zone is read in the schedule zone.
    assert occurrences(slots, at(0).astimezone(UTC), at(23).astimezone(UTC))


def test_dst_spring_forward_keeps_wall_clock_times() -> None:
    """On 2026-03-29 Berlin skips 02:00-03:00."""
    sunday = [
        slot(1, "Sunday", "01:00:00", "04:00:00"),
        slot(2, "Sunday", "05:00", "06:00"),
    ]
    result = occurrences(
        sunday,
        datetime(2026, 3, 29, 0, 0, tzinfo=BERLIN),
        datetime(2026, 3, 30, 0, 0, tzinfo=BERLIN),
    )

    assert result == [
        (1, "2026-03-29T01:00:00+01:00", "2026-03-29T04:00:00+02:00"),
        (2, "2026-03-29T05:00:00+02:00", "2026-03-29T06:00:00+02:00"),
    ]
    # The first slot is two real hours long, the wall clock shows three.
    first = expand_slots(
        sunday,
        BERLIN,
        datetime(2026, 3, 29, tzinfo=BERLIN),
        datetime(2026, 3, 30, tzinfo=BERLIN),
    )[0]
    assert first[2].astimezone(UTC) - first[1].astimezone(UTC) == timedelta(hours=2)


def test_dst_gap_start_moves_forward_and_empty_slots_are_dropped() -> None:
    gap = [
        slot(1, "Sunday", "02:30:00", "03:30:00"),
        slot(2, "Sunday", "02:10", "02:50"),
    ]
    result = occurrences(
        gap,
        datetime(2026, 3, 29, 0, 0, tzinfo=BERLIN),
        datetime(2026, 3, 30, 0, 0, tzinfo=BERLIN),
    )

    # 02:30 does not exist and becomes 03:30; the slot would be empty.
    assert result == [(2, "2026-03-29T03:10:00+02:00", "2026-03-29T03:50:00+02:00")]


def test_dst_fall_back_uses_first_occurrence() -> None:
    """On 2026-10-25 Berlin repeats 02:00-03:00."""
    sunday = [slot(1, "Sunday", "01:00:00", "04:00:00")]
    result = occurrences(
        sunday,
        datetime(2026, 10, 25, 0, 0, tzinfo=BERLIN),
        datetime(2026, 10, 26, 0, 0, tzinfo=BERLIN),
    )

    assert result == [(1, "2026-10-25T01:00:00+02:00", "2026-10-25T04:00:00+01:00")]
    begin, finish = expand_slots(
        sunday,
        BERLIN,
        datetime(2026, 10, 25, tzinfo=BERLIN),
        datetime(2026, 10, 26, tzinfo=BERLIN),
    )[0][1:]
    assert finish.astimezone(UTC) - begin.astimezone(UTC) == timedelta(hours=4)


def test_expansion_before_and_after_dst_matches_utc_offset() -> None:
    slots = [slot(1, "Sunday", "08:00:00", "09:00:00")]
    winter = occurrences(
        slots,
        datetime(2026, 3, 22, tzinfo=BERLIN),
        datetime(2026, 3, 23, tzinfo=BERLIN),
    )
    summer = occurrences(
        slots, datetime(2026, 4, 5, tzinfo=BERLIN), datetime(2026, 4, 6, tzinfo=BERLIN)
    )
    assert winter[0][1] == "2026-03-22T08:00:00+01:00"
    assert summer[0][1] == "2026-04-05T08:00:00+02:00"


def test_expansion_ignores_unreadable_slots_and_sorts() -> None:
    slots = [
        slot(3, "Monday", "10:00", "11:00"),
        slot(None, "Monday", "08:00", "09:00"),
        slot(2, "Monday", "08:00", "09:00"),
        slot(9, None),
    ]
    result = occurrences(
        slots,
        datetime(2026, 6, 1, tzinfo=BERLIN),
        datetime(2026, 6, 2, tzinfo=BERLIN),
    )
    assert [item[0] for item in result] == [None, 2, 3]


def test_event_text_shows_mode_speed_and_humidity_only_where_they_apply() -> None:
    heat = slot(mode="ManualHeatRecovery", fan="High", humidity="Dry")
    auto = slot(mode="Auto", fan="High", humidity="Moist")
    night = slot(mode="Night", fan="Low")
    legacy = slot(mode="Turbo")
    empty = slot(mode=None, fan=None, humidity=None, light=None)

    assert event_summary(heat, "en") == "Heat recovery · High"
    assert event_summary(auto, "en") == "Automatic · Moist"
    assert event_summary(night, "en") == "Night"
    assert event_summary(heat, "de") == "Wärmerückgewinnung · Hoch"
    assert event_summary(heat, "fr") == "Heat recovery · High"
    assert event_summary(legacy, "en") == "Turbo"
    assert event_summary(empty, "en") == "?"
    assert event_description(auto) == (
        "operating_mode: auto\nfan_speed: high\nhumidity_level: moist\n"
        "light_sensor_level: off"
    )
    assert slot_options(slot(fan="Night"))["fan_speed"] == "night"


def test_event_settings_are_read_from_description_and_title() -> None:
    description = (
        "Operating_Mode: night\nfan_speed:HIGH\nnoise\nunknown: x\n"
        "light_sensor_level = low"
    )
    assert parse_event_settings("Anything", description) == {
        "operating_mode": "Night",
        "fan_speed": "High",
    }
    # The title names the mode when the description does not.
    assert parse_event_settings("Wärmerückgewinnung · Hoch", None) == {
        "operating_mode": "ManualHeatRecovery"
    }
    assert parse_event_settings("night", "") == {"operating_mode": "Night"}
    assert parse_event_settings("Automatic", None) == {"operating_mode": "Auto"}
    # Fan speed words are not operating modes.
    assert parse_event_settings("high", None) == {}
    assert parse_event_settings(None, None) == {}
    assert parse_event_settings("x", 5) == {}


def test_unknown_options_in_the_description_are_rejected() -> None:
    with pytest.raises(ScheduleValidationError) as error:
        parse_event_settings("x", "fan_speed: warp")
    assert error.value.code == "schedule_invalid_value"
    assert error.value.placeholders == {"field": "fan_speed", "value": "warp"}


@pytest.mark.parametrize(
    "rule",
    [
        "FREQ=WEEKLY;BYDAY=MO",
        "freq=weekly;byday=mo",
        "FREQ=WEEKLY",
        "RRULE:FREQ=WEEKLY;INTERVAL=1;BYDAY=MO",
    ],
)
def test_weekly_rrule_is_accepted_on_its_own_day(rule: str) -> None:
    parse_weekly_rrule(rule, 1)


@pytest.mark.parametrize(
    "rule",
    [
        None,
        "",
        "  ",
        5,
        "FREQ",
        "FREQ=DAILY",
        "FREQ=MONTHLY;BYDAY=MO",
        "FREQ=WEEKLY;BYDAY=TU",
        "FREQ=WEEKLY;BYDAY=MO,TU",
        "FREQ=WEEKLY;INTERVAL=2;BYDAY=MO",
        "FREQ=WEEKLY;COUNT=3;BYDAY=MO",
        "FREQ=WEEKLY;UNTIL=20271231T000000Z;BYDAY=MO",
    ],
)
def test_other_recurrences_are_rejected(rule: object) -> None:
    with pytest.raises(ScheduleValidationError) as error:
        parse_weekly_rrule(rule, 1)
    assert error.value.code == "calendar_event_not_weekly"


def test_weekly_rrule_names_the_weekday() -> None:
    assert [weekly_rrule(day).split("=")[-1] for day in range(7)] == [
        "SU",
        "MO",
        "TU",
        "WE",
        "TH",
        "FR",
        "SA",
    ]
    assert date(2026, 6, 7).weekday() == 6
