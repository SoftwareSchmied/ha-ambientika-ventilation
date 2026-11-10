"""Tests for the time slot write endpoints of the API client."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from custom_components.ambientika_ventilation.api import (
    AmbientikaApiError,
    AmbientikaAuthError,
    AmbientikaConnectionError,
    AmbientikaDeviceOfflineError,
    AmbientikaForbiddenError,
    AmbientikaRateLimitError,
    AmbientikaResponseError,
    AmbientikaScheduleOverlapError,
    AmbientikaServerError,
    AmbientikaTimeSlotNotFoundError,
)
from custom_components.ambientika_ventilation.models import AmbientikaTimeSlot
from custom_components.ambientika_ventilation.schedule import ScheduleValidationError

from .test_api import FakeResponse, make_client

PROBLEM = {"Content-Type": "application/problem+json; charset=utf-8"}
SLEEP = "custom_components.ambientika_ventilation.api.asyncio.sleep"

SLOT = {
    "schedule_id": 7,
    "day_of_week": "monday",
    "start_time": "07:00",
    "end_time": "09:00",
    "operating_mode": "ManualHeatRecovery",
    "fan_speed": "Medium",
    "humidity_level": "Normal",
    "light_sensor_level": "Off",
}
EXISTING = (
    AmbientikaTimeSlot(
        slot_id=4,
        day_of_week="Monday",
        start_time="10:00:00",
        end_time="11:00:00",
    ),
)


def requests_of(client):
    return client._session.request.await_args_list


async def test_create_posts_the_documented_slot_and_ignores_the_answer() -> None:
    client = make_client([FakeResponse(200, {"id": 12}), FakeResponse(204)])

    assert await client.async_create_time_slot(101, **SLOT) is None
    assert await client.async_create_time_slot(101, **SLOT) is None

    call = requests_of(client)[0]
    assert call.args[0] == "POST"
    assert call.args[1].endswith("/Schedule/101/timeslots")
    assert call.kwargs["json"] == {
        "id": 0,
        "dayOfWeek": 1,
        "startTime": "07:00:00",
        "endTime": "09:00:00",
        "operatingMode": "ManualHeatRecovery",
        "fanSpeed": "Medium",
        "humidityLevel": "Normal",
        "lightSensorLevel": "Off",
        "scheduleId": 7,
    }
    assert client.metrics.last_response_shapes["/Schedule/{id}/timeslots"] == (
        "NoneType"
    )


async def test_update_puts_the_complete_slot() -> None:
    client = make_client([FakeResponse(200)])

    await client.async_update_time_slot(101, 4, **{**SLOT, "schedule_id": None})

    call = requests_of(client)[0]
    assert call.args[0] == "PUT"
    assert call.args[1].endswith("/Schedule/101/timeslots/4")
    assert call.kwargs["json"]["id"] == 4
    assert call.kwargs["json"]["scheduleId"] == 0
    assert call.kwargs["json"]["fanSpeed"] == "Medium"


async def test_delete_sends_no_body() -> None:
    client = make_client([FakeResponse(200)])

    await client.async_delete_time_slot(101, 4)

    call = requests_of(client)[0]
    assert call.args[0] == "DELETE"
    assert call.args[1].endswith("/Schedule/101/timeslots/4")
    assert call.kwargs["json"] is None


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"day_of_week": 9}, "schedule_invalid_day"),
        ({"start_time": "07:00:15"}, "schedule_invalid_time"),
        ({"end_time": "06:00"}, "schedule_time_order"),
        ({"fan_speed": "Night"}, "schedule_invalid_value"),
        ({"fan_speed": "Turbo"}, "schedule_invalid_value"),
        ({"operating_mode": "Turbo"}, "schedule_invalid_value"),
        ({"humidity_level": "Wet"}, "schedule_invalid_value"),
        ({"light_sensor_level": "High"}, "schedule_invalid_value"),
        ({"start_time": "10:30", "end_time": "10:45"}, "time_slot_overlap"),
    ],
)
async def test_invalid_values_are_never_sent(changes: dict, code: str) -> None:
    client = make_client([])

    with pytest.raises(ScheduleValidationError) as error:
        await client.async_create_time_slot(
            101, **{**SLOT, **changes}, existing_slots=EXISTING
        )
    with pytest.raises(ScheduleValidationError):
        await client.async_update_time_slot(
            101, 9, **{**SLOT, **changes}, existing_slots=EXISTING
        )

    assert error.value.code == code
    client._session.request.assert_not_called()


async def test_update_may_overlap_itself_but_not_others() -> None:
    client = make_client([FakeResponse(200)])
    same = {**SLOT, "start_time": "10:15", "end_time": "10:45"}

    await client.async_update_time_slot(101, 4, **same, existing_slots=EXISTING)
    with pytest.raises(ScheduleValidationError):
        await client.async_update_time_slot(101, 5, **same, existing_slots=EXISTING)

    assert client._session.request.await_count == 1


async def test_turbo_and_modes_follow_the_caller_permissions() -> None:
    client = make_client([FakeResponse(200)])

    await client.async_create_time_slot(
        101, **{**SLOT, "fan_speed": "Turbo"}, allow_turbo=True
    )
    with pytest.raises(ScheduleValidationError):
        await client.async_create_time_slot(101, **SLOT, allowed_modes=("Smart",))

    assert requests_of(client)[0].kwargs["json"]["fanSpeed"] == "Turbo"


@pytest.mark.parametrize("bad", [0, -1, "7", None, True, 1.5])
async def test_identifiers_must_be_positive_integers(bad: object) -> None:
    client = make_client([])

    with pytest.raises(ScheduleValidationError) as error:
        await client.async_delete_time_slot(bad, 4)  # type: ignore[arg-type]
    with pytest.raises(ScheduleValidationError):
        await client.async_delete_time_slot(101, bad)  # type: ignore[arg-type]
    with pytest.raises(ScheduleValidationError):
        await client.async_create_time_slot(bad, **SLOT)  # type: ignore[arg-type]
    with pytest.raises(ScheduleValidationError):
        await client.async_update_time_slot(101, bad, **SLOT)  # type: ignore[arg-type]

    assert error.value.code == "schedule_invalid_id"
    client._session.request.assert_not_called()


@pytest.mark.parametrize(
    "payload",
    [
        {"title": "Bad Request", "detail": "Schedule.OverlapError"},
        {"code": "Schedule.OverlapError"},
        "Time slots overlap",
        None,
    ],
)
async def test_400_is_an_overlap_without_retry(payload: object) -> None:
    client = make_client([FakeResponse(400, payload, PROBLEM)])

    with pytest.raises(AmbientikaScheduleOverlapError) as error:
        await client.async_create_time_slot(101, **SLOT)

    assert error.value.status == 400
    assert error.value.kind == "schedule_overlap"
    assert isinstance(error.value, AmbientikaResponseError)
    assert client._session.request.await_count == 1
    assert client.metrics.error_kinds == {"schedule_overlap": 1}


async def test_400_naming_another_problem_is_a_plain_rejection() -> None:
    client = make_client(
        [FakeResponse(400, {"title": "One or more validation errors occurred."})]
    )

    with pytest.raises(AmbientikaResponseError) as error:
        await client.async_update_time_slot(101, 4, **SLOT)

    assert not isinstance(error.value, AmbientikaScheduleOverlapError)
    assert error.value.kind == "http_4xx"
    assert error.value.status == 400


async def test_401_renews_the_login_and_repeats_the_write() -> None:
    client = make_client(
        [
            FakeResponse(401),
            FakeResponse(
                200, {"id": 1, "jwtToken": "fresh", "expiresAt": "2099-01-01T00:00:00Z"}
            ),
            FakeResponse(200),
        ]
    )

    await client.async_delete_time_slot(101, 4)

    assert requests_of(client)[-1].kwargs["headers"]["Authorization"] == "Bearer fresh"


async def test_persistent_401_raises_auth_error() -> None:
    client = make_client(
        [
            FakeResponse(401),
            FakeResponse(
                200, {"id": 1, "jwtToken": "fresh", "expiresAt": "2099-01-01T00:00:00Z"}
            ),
            FakeResponse(401),
        ]
    )

    with pytest.raises(AmbientikaAuthError):
        await client.async_delete_time_slot(101, 4)


async def test_404_is_a_missing_time_slot() -> None:
    client = make_client([FakeResponse(404)])

    with pytest.raises(AmbientikaTimeSlotNotFoundError) as error:
        await client.async_delete_time_slot(101, 4)

    assert error.value.status == 404
    assert client.metrics.last_error["endpoint"] == "/Schedule/{id}/timeslots/{id}"


async def test_406_means_the_unit_did_not_answer() -> None:
    client = make_client([FakeResponse(406)])

    with pytest.raises(AmbientikaDeviceOfflineError):
        await client.async_update_time_slot(101, 4, **SLOT)

    assert client._session.request.await_count == 1


async def test_403_is_forbidden() -> None:
    client = make_client([FakeResponse(403)])

    with pytest.raises(AmbientikaForbiddenError):
        await client.async_create_time_slot(101, **SLOT)


async def test_5xx_is_retried_like_any_command() -> None:
    client = make_client([FakeResponse(503), FakeResponse(500), FakeResponse(200)])

    with patch(SLEEP):
        await client.async_create_time_slot(101, **SLOT)

    assert client._session.request.await_count == 3


async def test_persistent_5xx_gives_up_after_three_attempts() -> None:
    client = make_client([FakeResponse(502)] * 3)

    with patch(SLEEP), pytest.raises(AmbientikaServerError):
        await client.async_delete_time_slot(101, 4)

    assert client._session.request.await_count == 3


async def test_429_waits_and_retries() -> None:
    client = make_client(
        [FakeResponse(429, headers={"Retry-After": "1"}), FakeResponse(200)]
    )

    with patch(SLEEP) as sleep:
        await client.async_delete_time_slot(101, 4)

    sleep.assert_awaited_once_with(1.0)


async def test_persistent_429_raises_rate_limit() -> None:
    client = make_client([FakeResponse(429)] * 3)

    with patch(SLEEP), pytest.raises(AmbientikaRateLimitError):
        await client.async_delete_time_slot(101, 4)


async def test_transport_failure_is_a_connection_error() -> None:
    from aiohttp import ClientConnectionError

    client = make_client([ClientConnectionError()] * 3)  # type: ignore[list-item]

    with patch(SLEEP), pytest.raises(AmbientikaConnectionError):
        await client.async_delete_time_slot(101, 4)

    assert issubclass(AmbientikaConnectionError, AmbientikaApiError)
