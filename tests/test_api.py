"""Tests for the embedded Ambientika API client."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import ClientSession
from custom_components.ambientika_ventilation.api import (
    AmbientikaApiClient,
    AmbientikaAuthError,
    AmbientikaForbiddenError,
    AmbientikaNotFoundError,
    AmbientikaResponseError,
    AmbientikaServerError,
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
