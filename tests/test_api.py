"""Tests for the embedded Ambientika API client."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import ClientError, ClientSession
from custom_components.ambientika_ventilation.api import (
    AmbientikaApiClient,
    AmbientikaApiError,
    AmbientikaAuthError,
    AmbientikaForbiddenError,
    AmbientikaNotFoundError,
    AmbientikaRateLimitError,
    AmbientikaResponseError,
    AmbientikaServerError,
    RequestMetrics,
    _date_text,
    _expires_soon,
    _response_payload,
    _retry_after,
)

BASE_URL = "https://ambientika.test"


class FakeResponse:
    """Small aiohttp response stand-in compatible with the API parser."""

    def __init__(
        self,
        status: int,
        payload: object = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        """Initialize a fake response."""
        self.status = status
        self.headers = headers or {}
        self._text = "" if payload is None else json.dumps(payload)
        self.content_length = len(self._text)

    async def text(self) -> str:
        """Return the serialized response body."""
        return self._text


def future_expiry() -> str:
    """Return a valid future token expiry."""
    return (datetime.now(UTC) + timedelta(hours=1)).isoformat()


def make_client(
    responses: list[FakeResponse],
    *,
    authenticated: bool = True,
    callback=None,
) -> AmbientikaApiClient:
    """Create a client backed by deterministic mocked responses."""
    session = MagicMock(spec=ClientSession)
    session.request = AsyncMock(side_effect=responses)
    return AmbientikaApiClient(
        cast(ClientSession, session),
        BASE_URL,
        "user@example.com",
        "password",
        token="token" if authenticated else None,
        user_id=1 if authenticated else None,
        expires_at=future_expiry() if authenticated else None,
        token_callback=callback,
    )


@pytest.mark.asyncio
async def test_authentication_and_token_callback() -> None:
    """Authentication parses and publishes token data."""
    callback = MagicMock()
    client = make_client(
        [
            FakeResponse(
                200,
                {"id": 42, "jwtToken": "secret", "expiresAt": future_expiry()},
            )
        ],
        authenticated=False,
        callback=callback,
    )

    token = await client.async_authenticate()

    assert token.user_id == 42
    assert token.token == "secret"
    callback.assert_called_once_with(token)
    assert client.metrics.successful_requests == 1


@pytest.mark.asyncio
async def test_invalid_credentials_raise_auth_error() -> None:
    """A login HTTP 400 is classified as invalid authentication."""
    client = make_client([FakeResponse(400)], authenticated=False)

    with pytest.raises(AmbientikaAuthError):
        await client.async_authenticate()


@pytest.mark.asyncio
async def test_optional_404_is_classified() -> None:
    """A missing device endpoint is distinguishable from an outage."""
    client = make_client([FakeResponse(404)])

    with pytest.raises(AmbientikaNotFoundError):
        await client.async_device_status("serial")


@pytest.mark.asyncio
async def test_batch_status_and_schedule_are_read_as_objects() -> None:
    """Optional aggregate and schedule resources validate their response shape."""
    client = make_client(
        [
            FakeResponse(200, [{"id": 10, "ianaTimezone": "Europe/Berlin"}]),
            FakeResponse(200, {"zoneDevicesInfo": []}),
            FakeResponse(200, {"id": 7, "timeSlots": []}),
        ]
    )

    assert await client.async_houses() == [{"id": 10, "ianaTimezone": "Europe/Berlin"}]
    assert await client.async_house_devices_status(10) == {"zoneDevicesInfo": []}
    assert await client.async_schedule(101) == {"id": 7, "timeSlots": []}


@pytest.mark.asyncio
async def test_rate_limit_retries_then_succeeds() -> None:
    """HTTP 429 observes bounded retry behavior and records metrics."""
    client = make_client([FakeResponse(429), FakeResponse(200, [])])

    with patch(
        "custom_components.ambientika_ventilation.api.asyncio.sleep", new=AsyncMock()
    ):
        assert await client.async_houses_info() == []

    assert client.metrics.rate_limit_events == 1
    assert client.metrics.failed_requests == 1
    assert client.metrics.successful_requests == 1


@pytest.mark.asyncio
async def test_change_mode_validates_before_network() -> None:
    """Unpublished enum values cannot be sent to a device."""
    client = make_client([])

    with pytest.raises(ValueError, match="Unsupported fan speed"):
        await client.async_change_mode(
            "serial",
            operating_mode="Auto",
            fan_speed="Extreme",
            humidity_level="Normal",
            light_sensor_level="Low",
        )

    with pytest.raises(ValueError, match="Unsupported fan speed"):
        await client.async_change_mode(
            "serial",
            operating_mode="Night",
            fan_speed="Night",
            humidity_level="Normal",
            light_sensor_level="Low",
        )


@pytest.mark.asyncio
async def test_unauthorized_request_reauthenticates_once(status_payload) -> None:
    """HTTP 401 causes a credential login and retries the original request."""
    client = make_client(
        [
            FakeResponse(401),
            FakeResponse(
                200,
                {"id": 1, "jwtToken": "renewed", "expiresAt": future_expiry()},
            ),
            FakeResponse(200, status_payload),
        ]
    )

    result = await client.async_device_status("AABBCCDDEEFF")

    assert result["temperature"] == 21
    assert client.metrics.successful_requests == 2


@pytest.mark.asyncio
async def test_forbidden_is_classified() -> None:
    """HTTP 403 represents an unavailable account capability."""
    client = make_client([FakeResponse(403)])

    with pytest.raises(AmbientikaForbiddenError):
        await client.async_feature_flags()


@pytest.mark.asyncio
async def test_server_error_retries_then_raises() -> None:
    """Persistent 5xx responses stop after bounded retries."""
    client = make_client([FakeResponse(503), FakeResponse(503), FakeResponse(503)])

    with (
        patch(
            "custom_components.ambientika_ventilation.api.asyncio.sleep",
            new=AsyncMock(),
        ),
        pytest.raises(AmbientikaServerError),
    ):
        await client.async_houses_info()

    assert client.metrics.failed_requests == 3


@pytest.mark.asyncio
async def test_empty_success_response_is_rejected() -> None:
    """A successful response without the documented object is not accepted."""
    client = make_client([FakeResponse(200)])

    with pytest.raises(AmbientikaResponseError):
        await client.async_device_status("serial")


@pytest.mark.asyncio
async def test_expiring_token_is_refreshed() -> None:
    """An expiring JWT is refreshed before the protected request."""
    client = make_client(
        [
            FakeResponse(
                200,
                {"userId": 1, "token": "renewed", "validTo": future_expiry()},
            ),
            FakeResponse(200, []),
        ]
    )
    client._expires_at = datetime.now(UTC).isoformat()

    assert await client.async_houses_info() == []
    assert client.metrics.token_refreshes == 1


@pytest.mark.parametrize(
    ("method", "payload"),
    [
        ("async_houses_info", {}),
        ("async_houses", {}),
        ("async_device_status", []),
        ("async_house_devices_status", []),
        ("async_schedule", []),
        ("async_feature_flags", []),
    ],
)
async def test_endpoint_response_shapes_are_validated(method, payload) -> None:
    """Every endpoint rejects a success response with the wrong JSON shape."""
    client = make_client([FakeResponse(200, payload)])
    call = getattr(client, method)
    args = {
        "async_device_status": ("serial",),
        "async_house_devices_status": (1,),
        "async_schedule": (1,),
    }.get(method, ())

    with pytest.raises(AmbientikaResponseError):
        await call(*args)

    assert client.metrics.parser_errors == 1


@pytest.mark.asyncio
async def test_feature_flags_keep_boolean_values_only() -> None:
    """Non-boolean feature flag fields do not leak into coordinator data."""
    client = make_client([FakeResponse(200, {"weeklyScheduler": True, "id": 1})])

    assert await client.async_feature_flags() == {"weeklyScheduler": True}


@pytest.mark.asyncio
async def test_write_endpoints_send_validated_payloads() -> None:
    """Control calls use the documented endpoint shapes."""
    client = make_client([FakeResponse(204), FakeResponse(204)])

    await client.async_change_mode(
        "serial",
        operating_mode="Auto",
        fan_speed="Low",
        humidity_level="Normal",
        light_sensor_level="Off",
        schedule_mode=True,
    )
    await client.async_reset_filter("serial")

    requests = client._session.request.await_args_list
    assert requests[0].args[:2] == ("POST", f"{BASE_URL}/Device/change-mode")
    assert requests[0].kwargs["json"]["isScheduleMode"] is True
    assert requests[1].args[:2] == ("GET", f"{BASE_URL}/Device/reset-filter")


@pytest.mark.asyncio
async def test_invalid_authentication_and_refresh_payloads() -> None:
    """Incomplete token responses are rejected without retaining partial data."""
    login = make_client([FakeResponse(200, {"id": 1})], authenticated=False)
    refresh = make_client([FakeResponse(200, {"userId": 1})])

    with pytest.raises(AmbientikaResponseError):
        await login.async_authenticate()
    with pytest.raises(AmbientikaResponseError):
        await refresh.async_refresh_token()

    assert login.metrics.parser_errors == 1
    assert refresh.metrics.parser_errors == 1

    non_object_login = make_client([FakeResponse(200, [])], authenticated=False)
    non_object_refresh = make_client([FakeResponse(200, [])])
    with pytest.raises(AmbientikaResponseError, match="not an object"):
        await non_object_login.async_authenticate()
    with pytest.raises(AmbientikaResponseError, match="not an object"):
        await non_object_refresh.async_refresh_token()


@pytest.mark.asyncio
async def test_missing_token_authenticates_before_request() -> None:
    """A protected request obtains a token when none has been cached."""
    client = make_client(
        [
            FakeResponse(
                200,
                {"id": 1, "jwtToken": "new", "expiresAt": future_expiry()},
            ),
            FakeResponse(200, []),
        ],
        authenticated=False,
    )

    assert await client.async_houses_info() == []
    assert client.user_id == 1


@pytest.mark.asyncio
async def test_failed_refresh_falls_back_to_credentials() -> None:
    """An invalid refresh token falls back to a fresh credential login."""
    client = make_client(
        [
            FakeResponse(401),
            FakeResponse(
                200,
                {"id": 1, "jwtToken": "new", "expiresAt": future_expiry()},
            ),
            FakeResponse(200, []),
        ]
    )
    client._expires_at = datetime.now(UTC).isoformat()

    assert await client.async_houses_info() == []


@pytest.mark.asyncio
async def test_persistent_unauthorized_request_fails() -> None:
    """A second HTTP 401 is not retried indefinitely."""
    client = make_client(
        [
            FakeResponse(401),
            FakeResponse(
                200,
                {"id": 1, "jwtToken": "new", "expiresAt": future_expiry()},
            ),
            FakeResponse(401),
        ]
    )

    with pytest.raises(AmbientikaAuthError):
        await client.async_device_status("serial")


@pytest.mark.asyncio
async def test_network_error_retries_then_fails() -> None:
    """Transient transport errors use bounded retries."""
    client = make_client([])
    client._session.request.side_effect = ClientError("offline")

    with (
        patch(
            "custom_components.ambientika_ventilation.api.asyncio.sleep",
            new=AsyncMock(),
        ),
        pytest.raises(AmbientikaApiError, match="unreachable"),
    ):
        await client.async_houses_info()

    assert client.metrics.failed_requests == 3


@pytest.mark.asyncio
async def test_rate_limit_and_generic_http_failures() -> None:
    """Persistent throttling and unexpected HTTP responses are classified."""
    throttled = make_client(
        [
            FakeResponse(429, headers={"Retry-After": "invalid"}),
            FakeResponse(429),
            FakeResponse(429),
        ]
    )
    unexpected = make_client([FakeResponse(418)])

    with (
        patch(
            "custom_components.ambientika_ventilation.api.asyncio.sleep",
            new=AsyncMock(),
        ),
        pytest.raises(AmbientikaRateLimitError),
    ):
        await throttled.async_houses_info()
    with pytest.raises(AmbientikaResponseError, match="HTTP 418"):
        await unexpected.async_houses_info()


@pytest.mark.asyncio
async def test_response_payload_and_date_helpers() -> None:
    """Low-level helpers cover empty, malformed, and fallback values."""
    empty = FakeResponse(204)
    malformed_success = FakeResponse(200)
    malformed_success._text = "not json"
    malformed_success.content_length = len(malformed_success._text)
    malformed_error = FakeResponse(500)
    malformed_error._text = "not json"
    malformed_error.content_length = len(malformed_error._text)
    empty_text = FakeResponse(200)
    empty_text.content_length = 1

    assert await _response_payload(empty) is None
    with pytest.raises(AmbientikaResponseError):
        await _response_payload(malformed_success)
    assert await _response_payload(malformed_error) is None
    assert await _response_payload(empty_text) is None
    assert _expires_soon(None) is True
    assert _expires_soon("invalid") is True
    assert _expires_soon(future_expiry()) is False
    naive_future = (datetime.now() + timedelta(hours=1)).isoformat()
    assert _expires_soon(naive_future) is False
    assert datetime.fromisoformat(_date_text(None)).tzinfo is not None
    assert _date_text("fixed") == "fixed"
    assert _retry_after(FakeResponse(429, headers={"Retry-After": "30"}), 0) == 15


def test_request_metrics_are_serializable() -> None:
    """Diagnostics receive a plain mapping instead of a Counter."""
    metrics = RequestMetrics(successful_requests=1)
    metrics.status_groups["2xx"] = 1

    assert metrics.as_dict()["status_groups"] == {"2xx": 1}
