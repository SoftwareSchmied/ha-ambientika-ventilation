"""Tests for schedule editing through the coordinator."""

from __future__ import annotations

from dataclasses import replace
from datetime import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from custom_components.ambientika_ventilation.api import (
    AmbientikaApiClient,
    AmbientikaApiError,
    AmbientikaAuthError,
    AmbientikaDeviceOfflineError,
    AmbientikaForbiddenError,
    AmbientikaScheduleOverlapError,
    AmbientikaTimeSlotNotFoundError,
)
from custom_components.ambientika_ventilation.const import DOMAIN
from custom_components.ambientika_ventilation.coordinator import (
    AmbientikaCoordinator,
    _kept_fan_speed,
)
from custom_components.ambientika_ventilation.models import (
    AmbientikaData,
    AmbientikaDevice,
    AmbientikaDeviceData,
    AmbientikaSchedule,
    AmbientikaStatus,
    AmbientikaTimeSlot,
)
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from pytest_homeassistant_custom_component.common import MockConfigEntry

SERIAL = "AABBCCDDEEFF"


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
        slot_id=slot_id,
        day_of_week=day,
        start_time=start,
        end_time=end,
        schedule_id=7,
        **fields,
    )


def payload_of(*slots: AmbientikaTimeSlot) -> dict[str, Any]:
    return {
        "id": 7,
        "timeSlots": [
            {
                "id": item.slot_id,
                "dayOfWeek": item.day_of_week,
                "startTime": item.start_time,
                "endTime": item.end_time,
                "operatingMode": item.operating_mode,
                "fanSpeed": item.fan_speed,
                "humidityLevel": item.humidity_level,
                "lightSensorLevel": item.light_sensor_level,
                "scheduleId": 7,
            }
            for item in slots
        ],
    }


def build(
    hass,
    *,
    flags: dict[str, bool] | None = None,
    slots: tuple[AmbientikaTimeSlot, ...] = (),
    schedule_state: str | None = "Off",
    role: str = "Master",
    device_type: str = "Diamond",
    device_id: int | None = 101,
    light: str = "Low",
    turbo: bool = False,
    with_schedule: bool = True,
) -> tuple[AmbientikaCoordinator, AsyncMock]:
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    api = AsyncMock()
    api.async_schedule.return_value = payload_of(*slots)
    coordinator = AmbientikaCoordinator(hass, entry, api)
    coordinator._feature_flags = {"weeklyScheduler": True} if flags is None else flags
    coordinator.data = AmbientikaData(
        devices={
            SERIAL: AmbientikaDeviceData(
                device=AmbientikaDevice(
                    serial_number=SERIAL,
                    name="Living room",
                    device_id=device_id,
                    device_type=device_type,
                    role=role,
                ),
                status=AmbientikaStatus(
                    serial_number=SERIAL,
                    operating_mode="Smart",
                    fan_speed="Low",
                    humidity_level="Normal",
                    light_sensor_level=light,
                    schedule_state=schedule_state,
                    turbo_available=turbo,
                ),
                schedule=(
                    AmbientikaSchedule(schedule_id=7, device_id=101, time_slots=slots)
                    if with_schedule
                    else None
                ),
            )
        }
    )
    coordinator._schedules[SERIAL] = coordinator.data.devices[SERIAL].schedule
    return coordinator, api


NEW = {
    "day_of_week": "monday",
    "start_time": time(10, 0),
    "end_time": time(11, 0),
    "operating_mode": "Night",
}


def real_validation(api: AsyncMock) -> AmbientikaApiClient:
    """Route writes through the real client (validation) with a dead transport."""
    real = AmbientikaApiClient(MagicMock(), "https://x.test", "u", "p")
    real._request = AsyncMock()
    api.async_create_time_slot.side_effect = real.async_create_time_slot
    api.async_update_time_slot.side_effect = real.async_update_time_slot
    return real


async def key_of(call) -> str:
    with pytest.raises((ServiceValidationError, HomeAssistantError)) as error:
        await call
    return error.value.translation_key


async def test_create_sends_defaults_and_publishes_the_new_schedule(hass) -> None:
    created = slot(
        2, "Monday", "10:00:00", "11:00:00", operating_mode="Night", fan_speed="Low"
    )
    coordinator, api = build(hass, slots=(slot(1),))
    api.async_schedule.return_value = payload_of(slot(1), created)

    await coordinator.async_create_time_slot(SERIAL, **NEW)

    kwargs = api.async_create_time_slot.await_args.kwargs
    assert api.async_create_time_slot.await_args.args == (101,)
    assert kwargs["schedule_id"] == 7
    assert kwargs["fan_speed"] == "Low"
    assert kwargs["humidity_level"] == "Normal"
    assert kwargs["light_sensor_level"] == "Off"
    assert kwargs["allow_turbo"] is False
    assert len(kwargs["existing_slots"]) == 1
    api.async_schedule.assert_awaited_once_with(101)
    assert len(coordinator.data.devices[SERIAL].schedule.time_slots) == 2
    assert coordinator._schedules[SERIAL] is coordinator.data.devices[SERIAL].schedule


async def test_create_without_schedule_or_light_sensor_uses_safe_defaults(hass) -> None:
    coordinator, api = build(hass, light="NotAvailable", with_schedule=False)

    await coordinator.async_create_time_slot(SERIAL, **NEW, fan_speed=None)

    kwargs = api.async_create_time_slot.await_args.kwargs
    assert kwargs["schedule_id"] is None
    assert kwargs["existing_slots"] == ()
    assert kwargs["light_sensor_level"] == "NotAvailable"
    assert kwargs["fan_speed"] == "Low"


async def test_explicit_values_are_passed_on(hass) -> None:
    coordinator, api = build(hass, turbo=True)

    await coordinator.async_create_time_slot(
        SERIAL,
        **NEW,
        fan_speed="Turbo",
        humidity_level="Dry",
        light_sensor_level="Medium",
    )

    kwargs = api.async_create_time_slot.await_args.kwargs
    assert (
        kwargs["fan_speed"],
        kwargs["humidity_level"],
        kwargs["light_sensor_level"],
    ) == (
        "Turbo",
        "Dry",
        "Medium",
    )
    assert kwargs["allow_turbo"] is True


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"flags": {}}, "schedule_read_only"),
        ({"flags": {"weeklyScheduler": False}}, "schedule_read_only"),
        ({"schedule_state": "NotAvailable"}, "schedule_read_only"),
        ({"schedule_state": None}, "schedule_read_only"),
        ({"device_id": None}, "schedule_read_only"),
        ({"role": "SlaveEqualMaster"}, "device_not_controllable"),
    ],
)
async def test_read_only_devices_reject_every_write(hass, kwargs, expected) -> None:
    coordinator, api = build(hass, slots=(slot(1),), **kwargs)

    assert coordinator.schedule_writable(SERIAL) is False
    assert await key_of(coordinator.async_create_time_slot(SERIAL, **NEW)) == expected
    assert (
        await key_of(coordinator.async_update_time_slot(SERIAL, 1, start_time="08:00"))
        == expected
    )
    assert await key_of(coordinator.async_delete_time_slot(SERIAL, 1)) == expected
    assert await key_of(coordinator.async_clear_schedule(SERIAL)) == expected
    api.async_create_time_slot.assert_not_called()
    api.async_delete_time_slot.assert_not_called()


async def test_unknown_device_is_not_writable(hass) -> None:
    coordinator, _api = build(hass)

    assert coordinator.schedule_writable("MISSING") is False
    assert (
        await key_of(coordinator.async_create_time_slot("MISSING", **NEW))
        == "device_not_controllable"
    )
    coordinator.data = None
    assert coordinator.schedule_writable(SERIAL) is False


@pytest.mark.parametrize(
    ("changes", "device_type", "turbo", "expected"),
    [
        ({"fan_speed": "Night"}, "Diamond", True, "schedule_invalid_value"),
        ({"fan_speed": "Turbo"}, "Diamond", False, "schedule_invalid_value"),
        ({"operating_mode": "AwayHome"}, "Gemini", False, "schedule_invalid_value"),
        ({"operating_mode": "Turbo"}, "Diamond", True, "schedule_invalid_value"),
        ({"day_of_week": "someday"}, "Diamond", False, "schedule_invalid_day"),
        ({"start_time": "11:00"}, "Diamond", False, "schedule_time_order"),
        ({"start_time": "10:00:30"}, "Diamond", False, "schedule_invalid_time"),
        (
            {"start_time": "06:00", "end_time": "08:00"},
            "Diamond",
            False,
            "time_slot_overlap",
        ),
    ],
)
async def test_invalid_slots_are_rejected_before_any_request(
    hass, changes, device_type, turbo, expected
) -> None:
    coordinator, api = build(
        hass, slots=(slot(1),), device_type=device_type, turbo=turbo
    )
    real = real_validation(api)

    assert (
        await key_of(coordinator.async_create_time_slot(SERIAL, **{**NEW, **changes}))
        == expected
    )
    real._request.assert_not_called()


async def test_gemini_accepts_its_own_modes(hass) -> None:
    coordinator, api = build(hass, device_type="Gemini")

    await coordinator.async_create_time_slot(SERIAL, **NEW)

    assert (
        "AwayHome" not in api.async_create_time_slot.await_args.kwargs["allowed_modes"]
    )


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (AmbientikaScheduleOverlapError("o", status=400), "time_slot_overlap"),
        (AmbientikaTimeSlotNotFoundError("n", status=404), "time_slot_not_found"),
        (AmbientikaDeviceOfflineError("d", status=406), "device_offline"),
        (AmbientikaForbiddenError("f", status=403), "schedule_write_failed"),
        (AmbientikaApiError("boom"), "schedule_write_failed"),
    ],
)
async def test_cloud_errors_become_translated_errors(hass, error, expected) -> None:
    coordinator, api = build(hass, slots=(slot(1),))
    api.async_create_time_slot.side_effect = error
    api.async_update_time_slot.side_effect = error
    api.async_delete_time_slot.side_effect = error
    api.async_schedule.return_value = payload_of(slot(1))

    assert await key_of(coordinator.async_create_time_slot(SERIAL, **NEW)) == expected
    assert (
        await key_of(
            coordinator.async_update_time_slot(SERIAL, 1, operating_mode="Night")
        )
        == expected
    )
    if not isinstance(error, AmbientikaTimeSlotNotFoundError):
        assert await key_of(coordinator.async_delete_time_slot(SERIAL, 1)) == expected


async def test_auth_failure_starts_reauthentication(hass) -> None:
    coordinator, api = build(hass)
    api.async_create_time_slot.side_effect = AmbientikaAuthError("no")

    with patch.object(coordinator.config_entry, "async_start_reauth") as reauth:
        assert (
            await key_of(coordinator.async_create_time_slot(SERIAL, **NEW))
            == "auth_failed"
        )

    reauth.assert_called_once_with(hass)


async def test_lost_first_attempt_that_created_the_slot_is_success(hass) -> None:
    created = slot(
        2, "Monday", "10:00:00", "11:00:00", operating_mode="Night", fan_speed="Low"
    )
    coordinator, api = build(hass, slots=(slot(1),))
    api.async_create_time_slot.side_effect = AmbientikaScheduleOverlapError(
        "o", status=400
    )
    api.async_schedule.return_value = payload_of(slot(1), created)

    await coordinator.async_create_time_slot(SERIAL, **NEW)

    assert len(coordinator.data.devices[SERIAL].schedule.time_slots) == 2


async def test_overlap_with_a_different_slot_is_still_an_error(hass) -> None:
    other = slot(2, "Monday", "10:00:00", "11:00:00", operating_mode="Auto")
    coordinator, api = build(hass, slots=(slot(1),))
    api.async_create_time_slot.side_effect = AmbientikaScheduleOverlapError(
        "o", status=400
    )
    api.async_schedule.return_value = payload_of(slot(1), other)

    assert (
        await key_of(coordinator.async_create_time_slot(SERIAL, **NEW))
        == "time_slot_overlap"
    )
    assert len(coordinator.data.devices[SERIAL].schedule.time_slots) == 1


async def test_overlap_when_the_check_read_fails_is_an_error(hass) -> None:
    coordinator, api = build(hass, with_schedule=False)
    coordinator._schedules.clear()
    api.async_create_time_slot.side_effect = AmbientikaScheduleOverlapError(
        "o", status=400
    )
    api.async_schedule.side_effect = AmbientikaApiError("down")

    assert (
        await key_of(coordinator.async_create_time_slot(SERIAL, **NEW))
        == "time_slot_overlap"
    )
    assert SERIAL in coordinator._schedule_due


async def test_update_keeps_unspecified_values_and_sends_the_slot_id(hass) -> None:
    stored = slot(
        1,
        humidity_level="Dry",
        fan_speed="High",
        light_sensor_level="Low",
        operating_mode="Auto",
    )
    coordinator, api = build(hass, slots=(stored,))

    await coordinator.async_update_time_slot(
        SERIAL, 1, end_time=time(9, 30), fan_speed=None
    )

    assert api.async_update_time_slot.await_args.args == (101, 1)
    kwargs = api.async_update_time_slot.await_args.kwargs
    assert kwargs["day_of_week"] == 1
    assert kwargs["start_time"] == time(7)
    assert kwargs["end_time"] == time(9, 30)
    assert kwargs["operating_mode"] == "Auto"
    assert kwargs["fan_speed"] == "High"
    assert kwargs["humidity_level"] == "Dry"
    assert kwargs["light_sensor_level"] == "Low"
    api.async_schedule.assert_awaited_once_with(101)


async def test_update_unknown_slot_is_rejected(hass) -> None:
    coordinator, api = build(hass, slots=(slot(1),))

    assert (
        await key_of(coordinator.async_update_time_slot(SERIAL, 99, start_time="08:00"))
        == "time_slot_not_found"
    )
    coordinator.data.devices[SERIAL] = replace(
        coordinator.data.devices[SERIAL], schedule=None
    )
    assert (
        await key_of(coordinator.async_update_time_slot(SERIAL, 1, start_time="08:00"))
        == "time_slot_not_found"
    )
    api.async_update_time_slot.assert_not_called()


@pytest.mark.parametrize(
    ("stored", "turbo", "expected"),
    [
        ("Night", False, "Low"),
        (None, False, "Low"),
        ("Turbo", False, "High"),
        ("Turbo", True, "Turbo"),
        ("Medium", False, "Medium"),
    ],
)
async def test_update_never_writes_back_unwritable_fan_speeds(
    hass, stored, turbo, expected
) -> None:
    coordinator, api = build(hass, slots=(slot(1, fan_speed=stored),), turbo=turbo)

    await coordinator.async_update_time_slot(SERIAL, 1, end_time="09:30")

    assert api.async_update_time_slot.await_args.kwargs["fan_speed"] == expected


def test_kept_fan_speed() -> None:
    assert _kept_fan_speed("Low", False) == "Low"
    assert _kept_fan_speed("Night", True) is None
    assert _kept_fan_speed("Turbo", False) == "High"


async def test_update_of_an_unreadable_slot_needs_a_complete_new_time(hass) -> None:
    coordinator, api = build(hass, slots=(slot(1, day="Noday"),))
    real = real_validation(api)

    assert (
        await key_of(
            coordinator.async_update_time_slot(SERIAL, 1, operating_mode="Night")
        )
        == "schedule_invalid_day"
    )
    await coordinator.async_update_time_slot(
        SERIAL, 1, day_of_week="monday", start_time="07:00", end_time="08:00"
    )

    assert real._request.await_count == 1


async def test_delete_re_reads_and_treats_a_missing_slot_as_deleted(hass) -> None:
    coordinator, api = build(hass, slots=(slot(1), slot(2, "Tuesday")))
    api.async_schedule.return_value = payload_of(slot(2, "Tuesday"))

    await coordinator.async_delete_time_slot(SERIAL, 1)

    api.async_delete_time_slot.assert_awaited_once_with(101, 1)
    assert [
        item.slot_id for item in coordinator.data.devices[SERIAL].schedule.time_slots
    ] == [2]

    api.async_delete_time_slot.side_effect = AmbientikaTimeSlotNotFoundError(
        "gone", status=404
    )
    await coordinator.async_delete_time_slot(SERIAL, 1)
    assert api.async_schedule.await_count == 2


async def test_read_back_failure_does_not_fail_the_write(hass) -> None:
    coordinator, api = build(hass, slots=(slot(1),))
    api.async_schedule.side_effect = AmbientikaApiError("down")

    await coordinator.async_delete_time_slot(SERIAL, 1)

    assert SERIAL in coordinator._schedule_due
    assert len(coordinator.data.devices[SERIAL].schedule.time_slots) == 1


async def test_read_back_is_skipped_when_the_device_vanished(hass) -> None:
    coordinator, api = build(hass, slots=(slot(1),))

    async def vanish(*_args: Any) -> None:
        coordinator.data = AmbientikaData()

    api.async_delete_time_slot.side_effect = vanish

    await coordinator.async_delete_time_slot(SERIAL, 1)

    assert coordinator.data.devices == {}


async def test_clear_deletes_every_slot_and_counts(hass) -> None:
    coordinator, api = build(
        hass, slots=(slot(1), slot(2, "Tuesday"), slot(3, "Friday"))
    )
    api.async_delete_time_slot.side_effect = [
        None,
        AmbientikaTimeSlotNotFoundError("gone"),
        None,
    ]
    api.async_schedule.return_value = payload_of()

    assert await coordinator.async_clear_schedule(SERIAL) == 2

    assert [call.args for call in api.async_delete_time_slot.await_args_list] == [
        (101, 1),
        (101, 2),
        (101, 3),
    ]
    assert coordinator.data.devices[SERIAL].schedule.time_slots == ()


async def test_clear_without_schedule_deletes_nothing(hass) -> None:
    coordinator, api = build(hass, with_schedule=False)

    assert await coordinator.async_clear_schedule(SERIAL) == 0

    api.async_delete_time_slot.assert_not_called()


async def test_clear_stops_at_the_first_failure_but_still_re_reads(hass) -> None:
    coordinator, api = build(hass, slots=(slot(1), slot(2, "Tuesday")))
    api.async_delete_time_slot.side_effect = [None, AmbientikaApiError("down")]
    api.async_schedule.return_value = payload_of(slot(2, "Tuesday"))

    assert (
        await key_of(coordinator.async_clear_schedule(SERIAL))
        == "schedule_write_failed"
    )

    assert [
        item.slot_id for item in coordinator.data.devices[SERIAL].schedule.time_slots
    ] == [2]
