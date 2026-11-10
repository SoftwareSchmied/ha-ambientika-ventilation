"""Tests for the schedule calendar entity and its actions."""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest
import voluptuous as vol
from custom_components.ambientika_ventilation.calendar import (
    CLEAR_SCHEDULE_SCHEMA,
    DELETE_TIME_SLOT_SCHEMA,
    SET_TIME_SLOT_SCHEMA,
    AmbientikaScheduleCalendar,
    _slot_id,
    async_setup_entry,
)
from custom_components.ambientika_ventilation.models import (
    AmbientikaData,
    AmbientikaDevice,
    AmbientikaDeviceData,
    AmbientikaSchedule,
    AmbientikaStatus,
    AmbientikaTimeSlot,
)
from homeassistant.components.calendar.const import CalendarEntityFeature
from homeassistant.core import SupportsResponse
from homeassistant.exceptions import ServiceValidationError

SERIAL = "AABBCCDDEEFF"
BERLIN = ZoneInfo("Europe/Berlin")
CALENDAR = "custom_components.ambientika_ventilation.calendar"


def slot(
    slot_id: int,
    day: str = "Monday",
    start: str = "07:00:00",
    end: str = "09:00:00",
    **values: Any,
) -> AmbientikaTimeSlot:
    fields: dict[str, Any] = {
        "operating_mode": "ManualHeatRecovery",
        "fan_speed": "Medium",
        "humidity_level": "Normal",
        "light_sensor_level": "Off",
        **values,
    }
    return AmbientikaTimeSlot(
        slot_id=slot_id, day_of_week=day, start_time=start, end_time=end, **fields
    )


def make_coordinator(
    slots: tuple[AmbientikaTimeSlot, ...] | None = (),
    *,
    timezone: str | None = "Europe/Berlin",
    role: str = "Master",
    state: str = "Off",
    writable: bool = True,
) -> MagicMock:
    coordinator = MagicMock()
    coordinator.last_update_success = True
    coordinator.data = AmbientikaData(
        devices={
            SERIAL: AmbientikaDeviceData(
                device=AmbientikaDevice(
                    serial_number=SERIAL,
                    name="Living room",
                    device_id=101,
                    device_type="Diamond",
                    role=role,
                    house_iana_timezone=timezone,
                ),
                status=AmbientikaStatus(serial_number=SERIAL, schedule_state=state),
                schedule=(
                    AmbientikaSchedule(schedule_id=7, time_slots=slots)
                    if slots is not None
                    else None
                ),
            )
        }
    )
    coordinator.schedule_writable.return_value = writable
    coordinator.async_add_topology_listener = MagicMock(return_value=MagicMock())
    for name in (
        "async_create_time_slot",
        "async_update_time_slot",
        "async_delete_time_slot",
        "async_clear_schedule",
    ):
        setattr(coordinator, name, AsyncMock())
    return coordinator


def make_entity(slots=(), language: str | None = None, **kwargs: Any):
    coordinator = make_coordinator(slots, **kwargs)
    entity = AmbientikaScheduleCalendar(coordinator, SERIAL)
    if language is not None:
        entity.hass = SimpleNamespace(config=SimpleNamespace(language=language))
    return entity, coordinator


def entry_for(coordinator: MagicMock) -> SimpleNamespace:
    return SimpleNamespace(
        runtime_data=SimpleNamespace(coordinator=coordinator),
        async_on_unload=MagicMock(),
    )


async def error_key(call) -> tuple[str, dict[str, str] | None]:
    with pytest.raises(ServiceValidationError) as error:
        await call
    return error.value.translation_key, error.value.translation_placeholders


# ---------------------------------------------------------------------------
# Platform setup and identity
# ---------------------------------------------------------------------------


async def test_setup_registers_actions_and_adds_calendars_with_a_schedule() -> None:
    coordinator = make_coordinator((slot(1),))
    add = MagicMock()
    entry = entry_for(coordinator)

    with patch(f"{CALENDAR}.entity_platform.async_get_current_platform") as platform:
        await async_setup_entry(MagicMock(), entry, add)

    registered = {
        call.args[0]: call
        for call in platform.return_value.async_register_entity_service.call_args_list
    }
    assert set(registered) == {
        "set_time_slot",
        "delete_time_slot",
        "clear_schedule",
        "get_schedule",
    }
    assert (
        registered["get_schedule"].kwargs["supports_response"] is SupportsResponse.ONLY
    )
    assert registered["set_time_slot"].args[2] == "async_set_time_slot"
    entities = list(add.call_args.args[0])
    assert len(entities) == 1
    assert entities[0].unique_id == f"{SERIAL}_schedule_calendar"
    assert entities[0].translation_key == "schedule"
    assert ("ambientika_ventilation", SERIAL) in entities[0].device_info["identifiers"]
    entry.async_on_unload.assert_called_once()


async def test_units_without_a_schedule_or_control_get_no_calendar() -> None:
    for coordinator in (
        make_coordinator(None),
        make_coordinator((slot(1),), role="SlaveEqualMaster"),
    ):
        add = MagicMock()
        with patch(f"{CALENDAR}.entity_platform.async_get_current_platform"):
            await async_setup_entry(MagicMock(), entry_for(coordinator), add)
        add.assert_not_called()


async def test_calendar_appears_when_a_schedule_shows_up_later() -> None:
    coordinator = make_coordinator(None)
    add = MagicMock()
    with patch(f"{CALENDAR}.entity_platform.async_get_current_platform"):
        await async_setup_entry(MagicMock(), entry_for(coordinator), add)
    listener = coordinator.async_add_topology_listener.call_args.args[0]
    add.assert_not_called()

    device_data = coordinator.data.devices[SERIAL]
    coordinator.data.devices[SERIAL] = AmbientikaDeviceData(
        device=device_data.device,
        status=device_data.status,
        schedule=AmbientikaSchedule(),
    )
    listener()
    listener()

    add.assert_called_once()


def test_editing_features_follow_the_write_permission() -> None:
    writable, _ = make_entity()
    read_only, _ = make_entity(writable=False)

    assert writable.supported_features == (
        CalendarEntityFeature.CREATE_EVENT
        | CalendarEntityFeature.UPDATE_EVENT
        | CalendarEntityFeature.DELETE_EVENT
    )
    assert read_only.supported_features == CalendarEntityFeature(0)
    assert writable.writable is True
    assert read_only.writable is False


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


async def test_events_are_recurring_and_carry_the_slot_values() -> None:
    entity, _ = make_entity(
        (
            slot(5, "Monday", fan_speed="High"),
            slot(6, "Wednesday", operating_mode="Night"),
        )
    )

    events = await entity.async_get_events(
        None,
        datetime(2026, 6, 1, 0, 0, tzinfo=BERLIN),
        datetime(2026, 6, 8, 0, 0, tzinfo=BERLIN),
    )

    assert [event.uid for event in events] == ["5", "6"]
    first = events[0]
    assert first.start == datetime(2026, 6, 1, 7, 0, tzinfo=BERLIN)
    assert first.end == datetime(2026, 6, 1, 9, 0, tzinfo=BERLIN)
    assert first.summary == "Heat recovery · High"
    assert first.rrule == "FREQ=WEEKLY;BYDAY=MO"
    assert first.recurrence_id == "2026-06-01T07:00:00+02:00"
    assert "fan_speed: high" in first.description
    assert events[1].summary == "Night"
    assert events[1].rrule == "FREQ=WEEKLY;BYDAY=WE"


async def test_events_use_the_house_zone_and_fall_back_to_the_home_zone() -> None:
    slots = (slot(1, "Monday", "07:00:00", "08:00:00"),)
    berlin, _ = make_entity(slots)
    local, _ = make_entity(slots, timezone=None)
    start = datetime(2026, 5, 31, 0, 0, tzinfo=UTC)
    end = datetime(2026, 6, 2, 0, 0, tzinfo=UTC)

    assert (await berlin.async_get_events(None, start, end))[
        0
    ].start.utcoffset().total_seconds() == 7200
    with patch(f"{CALENDAR}.dt_util.DEFAULT_TIME_ZONE", ZoneInfo("Asia/Tokyo")):
        events = await local.async_get_events(None, start, end)
    assert events[0].start.utcoffset().total_seconds() == 9 * 3600


async def test_event_text_follows_the_home_assistant_language() -> None:
    entity, _ = make_entity((slot(1, fan_speed="High"),), language="de")

    events = await entity.async_get_events(
        None,
        datetime(2026, 6, 1, tzinfo=BERLIN),
        datetime(2026, 6, 2, tzinfo=BERLIN),
    )

    assert events[0].summary == "Wärmerückgewinnung · Hoch"


async def test_slot_without_an_identifier_is_shown_but_has_no_uid() -> None:
    entity, _ = make_entity(
        (
            AmbientikaTimeSlot(
                day_of_week="Monday",
                start_time="07:00:00",
                end_time="08:00:00",
                operating_mode="Smart",
            ),
        )
    )

    events = await entity.async_get_events(
        None, datetime(2026, 6, 1, tzinfo=BERLIN), datetime(2026, 6, 2, tzinfo=BERLIN)
    )

    assert events[0].uid is None


async def test_no_schedule_means_no_events() -> None:
    entity, _ = make_entity(None)

    assert (
        await entity.async_get_events(
            None,
            datetime(2026, 6, 1, tzinfo=BERLIN),
            datetime(2026, 6, 8, tzinfo=BERLIN),
        )
        == []
    )
    assert entity.event is None


def test_event_is_the_current_or_next_slot() -> None:
    entity, _ = make_entity(
        (
            slot(1, "Monday", "07:00:00", "09:00:00"),
            slot(2, "Tuesday", "07:00", "09:00"),
        )
    )

    def event_at(moment: datetime):
        with patch(f"{CALENDAR}.dt_util.now", return_value=moment):
            return entity.event

    current = event_at(datetime(2026, 6, 1, 8, 0, tzinfo=BERLIN))
    upcoming = event_at(datetime(2026, 6, 1, 9, 0, tzinfo=BERLIN))
    wrapped = event_at(datetime(2026, 6, 2, 10, 0, tzinfo=BERLIN))

    assert current.uid == "1" and current.start.hour == 7
    assert upcoming.uid == "2"
    # After the last slot of the week the next one is a week away.
    assert wrapped.uid == "1" and wrapped.start.date() == date(2026, 6, 8)


# ---------------------------------------------------------------------------
# Creating, changing and deleting events
# ---------------------------------------------------------------------------


def weekly_event(**changes: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "summary": "Night",
        "dtstart": datetime(2026, 6, 1, 22, 0, tzinfo=BERLIN),
        "dtend": datetime(2026, 6, 1, 23, 30, tzinfo=BERLIN),
        "rrule": "FREQ=WEEKLY;BYDAY=MO",
    }
    values.update(changes)
    return {key: value for key, value in values.items() if value is not ...}


async def test_create_event_maps_a_weekly_event_to_a_slot() -> None:
    entity, coordinator = make_entity()

    await entity.async_create_event(
        **weekly_event(description="fan_speed: low\nhumidity_level: dry")
    )

    coordinator.async_create_time_slot.assert_awaited_once_with(
        SERIAL,
        day_of_week=1,
        start_time=time(22, 0),
        end_time=time(23, 30),
        operating_mode="Night",
        fan_speed="Low",
        humidity_level="Dry",
    )


async def test_create_event_converts_the_time_zone_and_accepts_naive_times() -> None:
    entity, coordinator = make_entity()

    await entity.async_create_event(
        **weekly_event(
            dtstart=datetime(2026, 6, 1, 20, 0, tzinfo=UTC),
            dtend=datetime(2026, 6, 1, 21, 30, tzinfo=UTC),
        )
    )
    await entity.async_create_event(
        **weekly_event(
            dtstart=datetime(2026, 6, 7, 6, 0),
            dtend=datetime(2026, 6, 7, 7, 0),
            rrule="FREQ=WEEKLY;BYDAY=SU",
            summary="Auto",
        )
    )

    first, second = coordinator.async_create_time_slot.await_args_list
    assert first.kwargs["start_time"] == time(22, 0)
    assert first.kwargs["day_of_week"] == 1
    assert second.kwargs["day_of_week"] == 0
    assert second.kwargs["start_time"] == time(6, 0)
    assert second.kwargs["operating_mode"] == "Auto"


async def test_create_event_uses_the_local_weekday_not_the_utc_one() -> None:
    entity, coordinator = make_entity()

    await entity.async_create_event(
        **weekly_event(
            dtstart=datetime(2026, 6, 1, 22, 30, tzinfo=UTC),
            dtend=datetime(2026, 6, 1, 23, 30, tzinfo=UTC),
            rrule="FREQ=WEEKLY;BYDAY=TU",
        )
    )

    assert coordinator.async_create_time_slot.await_args.kwargs["day_of_week"] == 2


@pytest.mark.parametrize(
    ("changes", "key"),
    [
        (
            {"dtstart": date(2026, 6, 1), "dtend": date(2026, 6, 2)},
            "calendar_event_not_representable",
        ),
        (
            {
                "dtstart": datetime(2026, 6, 1, 23, 0, tzinfo=BERLIN),
                "dtend": datetime(2026, 6, 2, 1, 0, tzinfo=BERLIN),
            },
            "calendar_event_not_representable",
        ),
        (
            {
                "dtstart": datetime(2026, 6, 1, 23, 0, tzinfo=BERLIN),
                "dtend": datetime(2026, 6, 2, 0, 0, tzinfo=BERLIN),
            },
            "calendar_event_not_representable",
        ),
        ({"rrule": ...}, "calendar_event_not_weekly"),
        ({"rrule": "FREQ=DAILY"}, "calendar_event_not_weekly"),
        ({"rrule": "FREQ=WEEKLY;BYDAY=MO,TU"}, "calendar_event_not_weekly"),
        ({"rrule": "FREQ=WEEKLY;BYDAY=TU"}, "calendar_event_not_weekly"),
        ({"rrule": "FREQ=WEEKLY;COUNT=4;BYDAY=MO"}, "calendar_event_not_weekly"),
        ({"rrule": "FREQ=WEEKLY;INTERVAL=2;BYDAY=MO"}, "calendar_event_not_weekly"),
        ({"summary": "Dinner party"}, "calendar_event_mode_missing"),
        ({"summary": ...}, "calendar_event_mode_missing"),
        ({"description": "fan_speed: warp"}, "schedule_invalid_value"),
    ],
)
async def test_unrepresentable_events_are_rejected_with_a_translated_error(
    changes, key
) -> None:
    entity, coordinator = make_entity()

    assert (await error_key(entity.async_create_event(**weekly_event(**changes))))[
        0
    ] == key
    coordinator.async_create_time_slot.assert_not_called()


async def test_create_event_reports_value_placeholders() -> None:
    entity, _ = make_entity()

    key, placeholders = await error_key(
        entity.async_create_event(**weekly_event(description="fan_speed: warp"))
    )

    assert key == "schedule_invalid_value"
    assert placeholders == {"field": "fan_speed", "value": "warp"}


async def test_update_event_changes_only_what_the_event_carries() -> None:
    entity, coordinator = make_entity((slot(5),))

    await entity.async_update_event(
        "5", {"summary": "Night", "description": "fan_speed: high"}
    )
    await entity.async_update_event("5", weekly_event(summary="Smart"))

    title_only, full = coordinator.async_update_time_slot.await_args_list
    assert title_only.args == (SERIAL, 5)
    assert title_only.kwargs == {"operating_mode": "Night", "fan_speed": "High"}
    assert full.kwargs == {
        "day_of_week": 1,
        "start_time": time(22, 0),
        "end_time": time(23, 30),
        "operating_mode": "Smart",
    }


async def test_update_event_with_nothing_to_change_still_calls_through() -> None:
    entity, coordinator = make_entity((slot(5),))

    await entity.async_update_event("5", {"location": "Kitchen"})

    assert coordinator.async_update_time_slot.await_args.kwargs == {}


@pytest.mark.parametrize(
    ("event", "key"),
    [
        (
            {"dtstart": datetime(2026, 6, 1, 8, tzinfo=BERLIN)},
            "calendar_event_not_representable",
        ),
        (
            {"dtend": datetime(2026, 6, 1, 8, tzinfo=BERLIN)},
            "calendar_event_not_representable",
        ),
        (weekly_event(rrule="FREQ=WEEKLY;BYDAY=FR"), "calendar_event_not_weekly"),
        (weekly_event(rrule=...), "calendar_event_not_weekly"),
        (weekly_event(rrule=""), "calendar_event_not_weekly"),
        ({"description": "operating_mode: nope"}, "schedule_invalid_value"),
    ],
)
async def test_update_event_rejects_unrepresentable_changes(event, key) -> None:
    entity, coordinator = make_entity((slot(5),))

    assert (await error_key(entity.async_update_event("5", event)))[0] == key
    coordinator.async_update_time_slot.assert_not_called()


async def test_a_single_occurrence_cannot_be_changed_or_deleted() -> None:
    entity, coordinator = make_entity((slot(5),))

    for recurrence_id, recurrence_range in (
        ("2026-06-01T07:00:00+02:00", None),
        (None, "THISANDFUTURE"),
        ("2026-06-01T07:00:00+02:00", "THISANDFUTURE"),
    ):
        assert (
            await error_key(
                entity.async_update_event(
                    "5", {"summary": "Night"}, recurrence_id, recurrence_range
                )
            )
        )[0] == "calendar_recurrence_unsupported"
        assert (
            await error_key(
                entity.async_delete_event("5", recurrence_id, recurrence_range)
            )
        )[0] == "calendar_recurrence_unsupported"

    coordinator.async_update_time_slot.assert_not_called()
    coordinator.async_delete_time_slot.assert_not_called()


async def test_delete_event_removes_the_slot() -> None:
    entity, coordinator = make_entity((slot(5),))

    await entity.async_delete_event("5")

    coordinator.async_delete_time_slot.assert_awaited_once_with(SERIAL, 5)


@pytest.mark.parametrize("uid", ["", "0", "-1", "abc", "5.5", "\u0665", None, 5])
async def test_event_uids_that_are_not_slot_ids_are_rejected(uid) -> None:
    entity, coordinator = make_entity((slot(5),))

    assert (await error_key(entity.async_delete_event(uid)))[0] == "time_slot_not_found"
    assert (await error_key(entity.async_update_event(uid, {})))[
        0
    ] == "time_slot_not_found"
    coordinator.async_delete_time_slot.assert_not_called()


def test_slot_id_parsing() -> None:
    assert _slot_id("12") == 12


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------


async def test_set_time_slot_creates_with_api_values() -> None:
    entity, coordinator = make_entity()
    data = SET_TIME_SLOT_SCHEMA(
        {
            "day": "sunday",
            "start": "06:30:00",
            "end": "08:00",
            "operating_mode": "heat_recovery",
            "fan_speed": "turbo",
            "humidity_level": "moist",
            "light_sensor_level": "off",
        }
    )

    await entity.async_set_time_slot(**data)

    coordinator.async_create_time_slot.assert_awaited_once_with(
        SERIAL,
        day_of_week="sunday",
        start_time=time(6, 30),
        end_time=time(8, 0),
        operating_mode="ManualHeatRecovery",
        fan_speed="Turbo",
        humidity_level="Moist",
        light_sensor_level="Off",
    )


async def test_set_time_slot_with_an_identifier_changes_that_slot() -> None:
    entity, coordinator = make_entity((slot(5),))

    await entity.async_set_time_slot(
        **SET_TIME_SLOT_SCHEMA({"slot_id": 5.0, "fan_speed": "high"})
    )

    coordinator.async_update_time_slot.assert_awaited_once_with(
        SERIAL,
        5,
        day_of_week=None,
        start_time=None,
        end_time=None,
        fan_speed="High",
    )
    coordinator.async_create_time_slot.assert_not_called()


@pytest.mark.parametrize(
    "given",
    [
        {},
        {"day": "monday", "start": "07:00", "end": "08:00"},
        {"day": "monday", "start": "07:00", "operating_mode": "night"},
        {"start": "07:00", "end": "08:00", "operating_mode": "night"},
    ],
)
async def test_a_new_slot_needs_day_times_and_mode(given) -> None:
    entity, coordinator = make_entity()

    key = (await error_key(entity.async_set_time_slot(**SET_TIME_SLOT_SCHEMA(given))))[
        0
    ]

    assert key == "time_slot_incomplete"
    coordinator.async_create_time_slot.assert_not_called()


@pytest.mark.parametrize(
    "given",
    [
        {"slot_id": 0},
        {"slot_id": "x"},
        {"day": "funday"},
        {"start": "25:00"},
        {"operating_mode": "turbo"},
        {"fan_speed": "night"},
        {"humidity_level": "wet"},
        {"light_sensor_level": "not_available"},
        {"unknown": 1},
    ],
)
def test_action_schema_rejects_unknown_values(given) -> None:
    with pytest.raises(vol.Invalid):
        SET_TIME_SLOT_SCHEMA(given)


def test_other_action_schemas() -> None:
    assert DELETE_TIME_SLOT_SCHEMA({"slot_id": "4"}) == {"slot_id": 4}
    with pytest.raises(vol.Invalid):
        DELETE_TIME_SLOT_SCHEMA({})
    with pytest.raises(vol.Invalid):
        CLEAR_SCHEDULE_SCHEMA({})
    assert CLEAR_SCHEDULE_SCHEMA({"confirm": "on"}) == {"confirm": True}


async def test_delete_time_slot_action() -> None:
    entity, coordinator = make_entity((slot(5),))

    await entity.async_delete_time_slot(5)

    coordinator.async_delete_time_slot.assert_awaited_once_with(SERIAL, 5)


async def test_clear_schedule_requires_the_confirmation() -> None:
    entity, coordinator = make_entity((slot(5),))

    assert (await error_key(entity.async_clear_schedule(False)))[
        0
    ] == "clear_schedule_not_confirmed"
    coordinator.async_clear_schedule.assert_not_called()

    await entity.async_clear_schedule(True)

    coordinator.async_clear_schedule.assert_awaited_once_with(SERIAL)


async def test_get_schedule_returns_the_slots_with_their_identifiers() -> None:
    entity, _ = make_entity(
        (
            slot(5, "Tuesday", fan_speed="Low"),
            slot(None, "Bad", "x", "y"),
        )
    )

    result = await entity.async_get_schedule()

    assert result["schedule_available"] is True
    assert result["writable"] is True
    assert result["time_slots"][0] == {
        "slot_id": 5,
        "day": "tuesday",
        "start": "07:00:00",
        "end": "09:00:00",
        "operating_mode": "heat_recovery",
        "fan_speed": "low",
        "humidity_level": "normal",
        "light_sensor_level": "off",
    }
    assert result["time_slots"][1]["day"] is None


async def test_get_schedule_without_a_schedule() -> None:
    entity, _ = make_entity(None, writable=False)

    assert await entity.async_get_schedule() == {
        "schedule_available": False,
        "writable": False,
        "time_slots": [],
    }


def test_action_descriptions_match_the_registered_schemas() -> None:
    """services.yaml and both translations describe exactly the same actions."""
    import json
    from pathlib import Path

    import yaml

    root = Path(__file__).parents[1] / "custom_components" / "ambientika_ventilation"
    described = yaml.safe_load((root / "services.yaml").read_text("utf-8"))
    names = {"set_time_slot", "delete_time_slot", "clear_schedule", "get_schedule"}
    schemas = {
        "set_time_slot": SET_TIME_SLOT_SCHEMA,
        "delete_time_slot": DELETE_TIME_SLOT_SCHEMA,
        "clear_schedule": CLEAR_SCHEDULE_SCHEMA,
    }

    assert names <= set(described)
    for name, schema in schemas.items():
        assert set(described[name].get("fields", {})) == {
            str(key) for key in schema.schema
        }
    assert "fields" not in described["get_schedule"]
    for source in ("strings.json", "translations/en.json", "translations/de.json"):
        services = json.loads((root / source).read_text("utf-8"))["services"]
        assert names <= set(services)
        for name in names - {"get_schedule"}:
            assert set(services[name]["fields"]) == set(described[name]["fields"])
