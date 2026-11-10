"""Tests for the 0.10 request handling: attempts, lanes, token renewal."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from aiohttp import ClientError, ClientTimeout
from custom_components.ambientika_ventilation.api import (
    AmbientikaAuthError,
    AmbientikaConnectionError,
    AmbientikaRateLimitError,
    AmbientikaServerError,
    _expires_within,
    _shape,
)
from custom_components.ambientika_ventilation.const import MAX_CONCURRENT_REQUESTS

from .test_api import FakeResponse, future_expiry, make_client

SLEEP = "custom_components.ambientika_ventilation.api.asyncio.sleep"
SERIAL = "AABBCCDDEEFF"


def expiry_in(days: float) -> str:
    """Return a token expiry timestamp the given number of days ahead."""
    return (datetime.now(UTC) + timedelta(days=days)).isoformat()


async def test_poll_requests_make_a_single_attempt() -> None:
    """The next poll is the retry, so polls never retry on their own."""
    for failure in (FakeResponse(503), FakeResponse(429)):
        client = make_client([failure, FakeResponse(200, {})])
        with pytest.raises((AmbientikaServerError, AmbientikaRateLimitError)):
            await client.async_device_status(SERIAL)
        assert client._session.request.await_count == 1

    client = make_client([FakeResponse(503), FakeResponse(200, {})])
    with pytest.raises(AmbientikaServerError):
        await client.async_house_devices_status(10)
    assert client._session.request.await_count == 1

    client = make_client([])
    client._session.request.side_effect = ClientError("offline")
    with pytest.raises(AmbientikaConnectionError):
        await client.async_schedule(7)
    assert client._session.request.await_count == 1
    assert client.metrics.failed_requests == 1


async def test_commands_still_retry_transient_failures() -> None:
    """A command survives temporary server errors."""
    client = make_client([FakeResponse(503), FakeResponse(503), FakeResponse(200, {})])

    with patch(SLEEP, new=AsyncMock()):
        await client.async_change_mode(
            SERIAL,
            operating_mode="Auto",
            fan_speed="Low",
            humidity_level="Normal",
            light_sensor_level="Low",
        )

    assert client._session.request.await_count == 3


async def test_requests_carry_connect_and_total_timeouts() -> None:
    """Every request is bounded by a short total and connect timeout."""
    client = make_client([FakeResponse(200, [])])

    await client.async_houses_info()

    timeout = client._session.request.await_args.kwargs["timeout"]
    assert isinstance(timeout, ClientTimeout)
    assert (timeout.total, timeout.connect) == (10, 5)


async def test_commands_do_not_wait_for_a_busy_poll_lane() -> None:
    """A command has its own concurrency lane and records the queue time."""
    client = make_client([FakeResponse(200, {})])
    for _ in range(MAX_CONCURRENT_REQUESTS):
        await client._semaphore.acquire()

    await asyncio.wait_for(
        client.async_change_mode(
            SERIAL,
            operating_mode="Auto",
            fan_speed="Low",
            humidity_level="Normal",
            light_sensor_level="Low",
        ),
        timeout=1,
    )

    assert client.metrics.last_semaphore_wait_ms == 0
    assert client.metrics.last_request_duration_ms is not None


async def test_poll_queue_wait_is_not_part_of_the_request_duration() -> None:
    """Time spent waiting for a free slot is measured separately."""
    client = make_client([FakeResponse(200, {"temperature": 1})])
    for _ in range(MAX_CONCURRENT_REQUESTS):
        await client._semaphore.acquire()

    task = asyncio.create_task(client.async_device_status(SERIAL))
    await asyncio.sleep(0.05)
    client._semaphore.release()
    await task

    assert client.metrics.last_semaphore_wait_ms >= 40
    assert client.metrics.last_request_duration_ms < 40


async def test_concurrent_401_logs_in_only_once() -> None:
    """Requests rejected with the same stale token share one fresh login."""
    logins = 0

    async def respond(method: str, url: str, **kwargs: Any) -> FakeResponse:
        nonlocal logins
        await asyncio.sleep(0)
        if url.endswith("/Users/authenticate"):
            logins += 1
            return FakeResponse(
                200, {"id": 1, "jwtToken": "fresh", "expiresAt": future_expiry()}
            )
        if kwargs["headers"].get("Authorization") == "Bearer fresh":
            return FakeResponse(200, {"deviceSerialNumber": SERIAL, "temperature": 1})
        return FakeResponse(401)

    client = make_client([])
    client._session.request.side_effect = respond

    results = await asyncio.gather(
        client.async_device_status(SERIAL), client.async_device_status("112233445566")
    )

    assert logins == 1
    assert all(result["temperature"] == 1 for result in results)


async def test_401_after_failed_login_raises_auth_error() -> None:
    """A rejected fresh login is an authentication error, not a retry loop."""
    client = make_client([FakeResponse(401), FakeResponse(401)])

    with pytest.raises(AmbientikaAuthError):
        await client.async_device_status(SERIAL)


async def test_token_is_renewed_ahead_of_expiry() -> None:
    """A token with fewer than 30 days left is refreshed and published."""
    seen: list[str] = []
    client = make_client(
        [
            FakeResponse(
                200,
                {"token": "renewed", "userId": 1, "validTo": expiry_in(180)},
            )
        ],
        callback=lambda token: seen.append(token.token),
    )
    client._expires_at = expiry_in(10)

    assert await client.async_renew_token_if_due(timedelta(days=30)) is True

    assert seen == ["renewed"]
    assert client.token_expires_at is not None
    assert client.metrics.token_refreshes == 1
    assert await client.async_renew_token_if_due(timedelta(days=30)) is False
    assert client._session.request.await_count == 1


async def test_token_far_from_expiry_or_missing_is_not_renewed() -> None:
    """Nothing is requested while the token is valid long enough or absent."""
    client = make_client([])
    client._expires_at = expiry_in(100)
    assert await client.async_renew_token_if_due(timedelta(days=30)) is False

    anonymous = make_client([], authenticated=False)
    assert await anonymous.async_renew_token_if_due(timedelta(days=30)) is False
    assert client._session.request.await_count == 0
    assert anonymous._session.request.await_count == 0


async def test_rejected_token_is_replaced_by_a_login_while_renewing() -> None:
    """If the old token no longer refreshes, the stored credentials are used."""
    client = make_client(
        [
            FakeResponse(401),
            FakeResponse(
                200, {"id": 1, "jwtToken": "login", "expiresAt": expiry_in(180)}
            ),
        ]
    )
    client._expires_at = expiry_in(5)

    assert await client.async_renew_token_if_due(timedelta(days=30)) is True

    assert client._token == "login"


async def test_failed_renewal_keeps_the_working_token() -> None:
    """A temporary error while renewing early leaves the current token alone."""
    client = make_client([FakeResponse(503), FakeResponse(503), FakeResponse(503)])
    client._expires_at = expiry_in(5)

    with (
        patch(SLEEP, new=AsyncMock()),
        pytest.raises(AmbientikaServerError),
    ):
        await client.async_renew_token_if_due(timedelta(days=30))

    assert client._token == "token"


async def test_concurrent_renewals_refresh_once() -> None:
    """A second caller waiting for the lock finds the token already renewed."""
    client = make_client([])

    async def refresh(method: str, url: str, **kwargs: Any) -> FakeResponse:
        await asyncio.sleep(0)
        return FakeResponse(
            200, {"token": "renewed", "userId": 1, "validTo": expiry_in(180)}
        )

    client._session.request.side_effect = refresh
    client._expires_at = expiry_in(5)

    results = await asyncio.gather(
        client.async_renew_token_if_due(timedelta(days=30)),
        client.async_renew_token_if_due(timedelta(days=30)),
    )

    assert sorted(results) == [False, True]
    assert client._session.request.await_count == 1


async def test_response_structure_is_recorded_without_values() -> None:
    """Diagnostics get keys and types of the last response, never the values."""
    client = make_client(
        [FakeResponse(200, {"deviceSerialNumber": SERIAL, "temperature": 21})]
    )

    await client.async_device_status(SERIAL)

    shapes = client.metrics.as_dict()["last_response_shapes"]
    assert shapes == {
        "/Device/device-status": {"deviceSerialNumber": "str", "temperature": "int"}
    }
    assert SERIAL not in repr(shapes)


def test_shape_describes_nesting_lists_and_depth() -> None:
    """Shapes cover empty and filled lists and stop at a maximum depth."""
    assert _shape([]) == []
    assert _shape([{"a": 1}, {"a": 2}]) == [{"a": "int"}, "2 item(s)"]
    assert _shape(None) == "NoneType"
    deep: Any = {"a": {"b": {"c": {"d": {"e": 1}}}}}
    assert _shape(deep) == {"a": {"b": {"c": {"d": "object"}}}}
    assert _shape({"a": {"b": {"c": {"d": [1, 2]}}}}) == {
        "a": {"b": {"c": {"d": "list[2]"}}}
    }


def test_expires_within_handles_missing_and_invalid_values() -> None:
    """Missing or invalid expiry data always counts as due."""
    margin = timedelta(days=30)
    assert _expires_within(None, margin) is True
    assert _expires_within("invalid", margin) is True
    assert _expires_within(expiry_in(5), margin) is True
    assert _expires_within(expiry_in(60), margin) is False


async def test_failed_renewal_keeps_the_current_token() -> None:
    """A renewal that cannot sign in again must not discard the valid token."""
    client = make_client([FakeResponse(401), FakeResponse(401)])
    client._expires_at = expiry_in(5)
    token = client._token
    assert token is not None

    with pytest.raises(AmbientikaAuthError):
        await client.async_renew_token_if_due(timedelta(days=30))

    assert client._token == token
