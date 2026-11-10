"""Tests for HTTP error classification and failure metrics of the API client."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from aiohttp import ClientConnectionError
from custom_components.ambientika_ventilation.api import (
    TRANSIENT_ERRORS,
    AmbientikaApiError,
    AmbientikaAuthError,
    AmbientikaConnectionError,
    AmbientikaDeviceNotFoundError,
    AmbientikaDeviceOfflineError,
    AmbientikaHouseNotConfiguredError,
    AmbientikaHouseNotFoundError,
    AmbientikaNoScheduleError,
    AmbientikaNotFoundError,
    AmbientikaResponseError,
    AmbientikaServerError,
    _endpoint_label,
    _http_error,
    describe_error,
)

from .test_api import FakeResponse, make_client

PROBLEM = {"Content-Type": "application/problem+json; charset=utf-8"}
SLEEP = "custom_components.ambientika_ventilation.api.asyncio.sleep"


async def test_command_406_is_device_offline_without_retry() -> None:
    """A command the device does not acknowledge is reported once as offline."""
    client = make_client([FakeResponse(406)])

    with pytest.raises(AmbientikaDeviceOfflineError) as error:
        await client.async_change_mode(
            "AABBCCDDEEFF",
            operating_mode="Off",
            fan_speed="Low",
            humidity_level="Normal",
            light_sensor_level="Low",
        )

    assert error.value.kind == "device_offline"
    assert error.value.status == 406
    assert isinstance(error.value, AmbientikaResponseError)
    assert client._session.request.await_count == 1
    assert client.metrics.error_kinds == {"device_offline": 1}


async def test_filter_reset_406_is_device_offline() -> None:
    """The filter reset command uses the same offline classification."""
    client = make_client([FakeResponse(406)])

    with pytest.raises(AmbientikaDeviceOfflineError):
        await client.async_reset_filter("AABBCCDDEEFF")


async def test_house_status_406_means_house_not_configured() -> None:
    """A house without a configured master is not treated as a cloud outage."""
    client = make_client([FakeResponse(406)])

    with pytest.raises(AmbientikaHouseNotConfiguredError) as error:
        await client.async_house_devices_status(10)

    assert not isinstance(error.value, TRANSIENT_ERRORS)


async def test_house_not_found_body_is_classified() -> None:
    """The JSON string body identifies a deleted or foreign house."""
    client = make_client([FakeResponse(404, "House not found")])

    with pytest.raises(AmbientikaHouseNotFoundError) as error:
        await client.async_house_devices_status(10)

    assert isinstance(error.value, AmbientikaNotFoundError)


async def test_unknown_device_400_is_device_not_found() -> None:
    """The cloud answers unknown serial numbers with HTTP 400."""
    client = make_client([FakeResponse(400, "Device not found")])

    with pytest.raises(AmbientikaDeviceNotFoundError) as error:
        await client.async_device_status("AABBCCDDEEFF")

    assert isinstance(error.value, AmbientikaNotFoundError)
    assert error.value.status == 400


async def test_other_400_on_device_status_stays_generic() -> None:
    """Other validation errors are not mistaken for removed devices."""
    client = make_client([FakeResponse(400, {"title": "Bad", "traceId": "t-1"})])

    with pytest.raises(AmbientikaResponseError) as error:
        await client.async_device_status("AABBCCDDEEFF")

    assert not isinstance(error.value, AmbientikaNotFoundError)
    assert error.value.kind == "http_4xx"
    assert error.value.trace_id == "t-1"


async def test_missing_schedule_problem_is_no_schedule() -> None:
    """A problem+json 404 on the schedule resource means no schedule exists."""
    client = make_client(
        [FakeResponse(404, {"title": "Not Found", "traceId": "00-abc"}, PROBLEM)]
    )

    with pytest.raises(AmbientikaNoScheduleError) as error:
        await client.async_schedule(101)

    assert error.value.trace_id == "00-abc"
    assert client.metrics.last_error["endpoint"] == "/Schedule/{id}"
    assert client.metrics.last_error["trace_id"] == "00-abc"


async def test_empty_404_remains_an_unsupported_resource() -> None:
    """An empty 404 keeps meaning that the endpoint is not supported."""
    client = make_client([FakeResponse(404)])

    with pytest.raises(AmbientikaNotFoundError) as error:
        await client.async_schedule(101)

    assert type(error.value) is AmbientikaNotFoundError


@pytest.mark.parametrize(
    ("side_effect", "kind"),
    [
        (TimeoutError(), "timeout"),
        (ClientConnectionError("reset"), "connection"),
    ],
)
async def test_transport_failures_are_counted_by_kind(side_effect, kind) -> None:
    """Timeouts and transport errors are distinguishable in metrics."""
    client = make_client([])
    client._session.request.side_effect = side_effect

    with (
        patch(SLEEP, new=AsyncMock()),
        pytest.raises(AmbientikaConnectionError) as error,
    ):
        await client.async_houses_info()

    assert error.value.kind == kind
    assert isinstance(error.value, TRANSIENT_ERRORS)
    assert client.metrics.error_kinds == {kind: 3}
    assert client.metrics.last_error["kind"] == kind
    assert client.metrics.last_error["status"] is None


async def test_server_errors_are_counted_and_described() -> None:
    """Retried server errors are counted per attempt and keep the trace id."""
    client = make_client(
        [FakeResponse(503, {"title": "Unavailable", "traceId": "tr"}, PROBLEM)] * 3
    )

    with patch(SLEEP, new=AsyncMock()), pytest.raises(AmbientikaServerError) as error:
        await client.async_houses_info()

    assert client.metrics.error_kinds == {"http_5xx": 3}
    assert describe_error(error.value) == "http_5xx, HTTP 503, traceId tr"
    metrics = client.metrics.as_dict()
    assert metrics["error_kinds"] == {"http_5xx": 3}
    assert metrics["last_error"]["endpoint"] == "/House/houses-info"


async def test_invalid_success_json_is_counted_as_parse_error() -> None:
    """A malformed success body is recorded as a parse failure."""
    response = FakeResponse(200)
    response._text = "not json"
    response.content_length = len(response._text)
    client = make_client([response])

    with pytest.raises(AmbientikaResponseError):
        await client.async_houses_info()

    assert client.metrics.error_kinds == {"parse": 1}


async def test_unauthorized_attempts_are_counted() -> None:
    """A failed reauthentication is visible as an auth failure kind."""
    client = make_client([FakeResponse(401), FakeResponse(401)])

    with pytest.raises(AmbientikaAuthError):
        await client.async_houses_info()

    assert client.metrics.error_kinds["auth"] == 2


def test_error_helpers_handle_unusual_inputs() -> None:
    """Classification helpers never expose identifiers or crash on odd bodies."""
    assert _endpoint_label("/Schedule/12345") == "/Schedule/{id}"
    assert describe_error(RuntimeError("x")) == "RuntimeError"
    assert describe_error(AmbientikaApiError("plain")) == "api_error"
    assert (
        _http_error(
            418, {"message": "teapot"}, "", authenticated=True, context=None
        ).kind
        == "http_4xx"
    )
    long_trace = _http_error(
        500, {"traceId": "x" * 200}, "", authenticated=True, context=None
    )
    assert long_trace.trace_id == "x" * 80
    assert _http_error(406, None, "", authenticated=True, context=None).kind == (
        "http_4xx"
    )
    assert (
        _http_error(404, {"detail": "  "}, "", authenticated=True, context=None).kind
        == "http_4xx"
    )
