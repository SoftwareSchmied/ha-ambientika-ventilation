"""Async client for the Ambientika cloud API."""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from aiohttp import ClientError, ClientResponse, ClientSession

from .const import (
    HUMIDITY_LEVELS,
    LIGHT_SENSOR_LEVELS,
    MAX_CONCURRENT_REQUESTS,
    OPERATING_MODES,
    REQUEST_TIMEOUT,
    WRITABLE_FAN_SPEEDS,
)

LOGGER = logging.getLogger(__name__)


class AmbientikaApiError(Exception):
    """Base class for Ambientika API errors.

    ``kind`` classifies the failure for logging and diagnostics (for example
    ``timeout``, ``connection``, ``http_5xx``, ``http_429``, ``http_4xx``,
    ``auth`` or ``parse``). ``status`` and ``trace_id`` are only set when the
    cloud returned an HTTP response.
    """

    default_kind = "api_error"

    def __init__(
        self,
        message: str = "",
        *,
        kind: str | None = None,
        status: int | None = None,
        trace_id: str | None = None,
    ) -> None:
        """Store the message and non-sensitive failure details."""
        super().__init__(message)
        self.kind = kind or self.default_kind
        self.status = status
        self.trace_id = trace_id


class AmbientikaConnectionError(AmbientikaApiError):
    """The cloud could not be reached (timeout or transport error)."""

    default_kind = "connection"


class AmbientikaAuthError(AmbientikaApiError):
    """Authentication is missing or no longer valid."""

    default_kind = "auth"


class AmbientikaForbiddenError(AmbientikaApiError):
    """The account may not access a resource."""

    default_kind = "http_4xx"


class AmbientikaNotFoundError(AmbientikaApiError):
    """An optional resource is not supported."""

    default_kind = "http_4xx"


class AmbientikaHouseNotFoundError(AmbientikaNotFoundError):
    """The house no longer exists or belongs to another account."""


class AmbientikaDeviceNotFoundError(AmbientikaNotFoundError):
    """The cloud no longer knows the device serial number."""


class AmbientikaNoScheduleError(AmbientikaNotFoundError):
    """The device has no weekly schedule configured."""


class AmbientikaRateLimitError(AmbientikaApiError):
    """The cloud API rate limit was exceeded."""

    default_kind = "http_429"


class AmbientikaServerError(AmbientikaApiError):
    """The cloud API failed temporarily."""

    default_kind = "http_5xx"


class AmbientikaResponseError(AmbientikaApiError):
    """The cloud API returned an invalid response."""

    default_kind = "parse"


class AmbientikaDeviceOfflineError(AmbientikaResponseError):
    """The device did not acknowledge a command (HTTP 406)."""

    default_kind = "device_offline"


class AmbientikaStatusUnavailableError(AmbientikaDeviceOfflineError):
    """The cloud holds no current status packet, so the device is offline.

    The cloud answers HTTP 404 "Status packet not found!" a few minutes after a
    unit lost power and returns to normal as soon as it reconnects. This is not
    an unsupported endpoint and the device must keep being polled.
    """


class AmbientikaHouseNotConfiguredError(AmbientikaResponseError):
    """The house has no configured master device (HTTP 406)."""

    default_kind = "http_4xx"


CONTEXT_COMMAND = "command"
CONTEXT_DEVICE_STATUS = "device_status"
CONTEXT_HOUSE_STATUS = "house_status"
CONTEXT_SCHEDULE = "schedule"

TRANSIENT_ERRORS = (
    AmbientikaConnectionError,
    AmbientikaServerError,
    AmbientikaRateLimitError,
)


def describe_error(err: BaseException) -> str:
    """Return a short, non-sensitive description of an API failure."""
    if not isinstance(err, AmbientikaApiError):
        return type(err).__name__
    parts = [err.kind]
    if err.status is not None:
        parts.append(f"HTTP {err.status}")
    if err.trace_id:
        parts.append(f"traceId {err.trace_id}")
    return ", ".join(parts)


@dataclass(frozen=True, slots=True)
class AmbientikaToken:
    """Authenticated Ambientika session data."""

    user_id: int
    token: str
    expires_at: str


@dataclass(slots=True)
class RequestMetrics:
    """Non-sensitive request counters used by diagnostics."""

    successful_requests: int = 0
    failed_requests: int = 0
    status_groups: Counter[str] = field(default_factory=Counter)
    error_kinds: Counter[str] = field(default_factory=Counter)
    parser_errors: int = 0
    rate_limit_events: int = 0
    token_refreshes: int = 0
    last_request_duration_ms: int | None = None
    last_error: dict[str, Any] | None = None

    def record_error(
        self,
        kind: str,
        *,
        endpoint: str,
        status: int | None = None,
        trace_id: str | None = None,
    ) -> None:
        """Count one failed attempt and remember its non-sensitive details."""
        self.error_kinds[kind] += 1
        self.last_error = {
            "kind": kind,
            "endpoint": endpoint,
            "status": status,
            "trace_id": trace_id,
            "at": datetime.now(UTC).isoformat(),
        }

    def as_dict(self) -> dict[str, Any]:
        """Return JSON-serializable metrics."""
        data = asdict(self)
        data["status_groups"] = dict(self.status_groups)
        data["error_kinds"] = dict(self.error_kinds)
        return data


TokenCallback = Callable[[AmbientikaToken], None]


class AmbientikaApiClient:
    """Rate-limited async Ambientika cloud API client."""

    def __init__(
        self,
        session: ClientSession,
        base_url: str,
        username: str,
        password: str,
        *,
        token: str | None = None,
        user_id: int | None = None,
        expires_at: str | None = None,
        token_callback: TokenCallback | None = None,
    ) -> None:
        """Initialize the client without performing network I/O."""
        self._session = session
        self._base_url = base_url.rstrip("/")
        self._username = username
        self._password = password
        self._token = token
        self._user_id = user_id
        self._expires_at = expires_at
        self._token_callback = token_callback
        self._auth_lock = asyncio.Lock()
        self._semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)
        self.metrics = RequestMetrics()

    @property
    def user_id(self) -> int | None:
        """Return the authenticated account identifier."""
        return self._user_id

    async def async_authenticate(self) -> AmbientikaToken:
        """Authenticate with user credentials and retain the returned JWT."""
        payload = await self._request(
            "POST",
            "/Users/authenticate",
            json_body={"username": self._username, "password": self._password},
            authenticated=False,
        )
        if not isinstance(payload, dict):
            self.metrics.parser_errors += 1
            raise AmbientikaResponseError("Authentication response is not an object")
        token = payload.get("jwtToken")
        user_id = payload.get("id")
        expires_at = payload.get("expiresAt")
        if not isinstance(token, str) or not token or not isinstance(user_id, int):
            self.metrics.parser_errors += 1
            raise AmbientikaResponseError("Authentication response is incomplete")
        return self._store_token(user_id, token, _date_text(expires_at))

    async def async_refresh_token(self) -> AmbientikaToken:
        """Refresh the current JWT before it expires."""
        if self._token is None:
            return await self.async_authenticate()
        payload = await self._request(
            "GET",
            "/Users/refresh-token",
            ensure_token=False,
            recover_auth=False,
        )
        if not isinstance(payload, dict):
            self.metrics.parser_errors += 1
            raise AmbientikaResponseError("Token refresh response is not an object")
        token = payload.get("token")
        user_id = payload.get("userId", self._user_id)
        expires_at = payload.get("validTo")
        if not isinstance(token, str) or not token or not isinstance(user_id, int):
            self.metrics.parser_errors += 1
            raise AmbientikaResponseError("Token refresh response is incomplete")
        self.metrics.token_refreshes += 1
        return self._store_token(user_id, token, _date_text(expires_at))

    async def async_houses_info(self) -> list[object]:
        """Return all houses and their device metadata."""
        payload = await self._request("GET", "/House/houses-info")
        if not isinstance(payload, list):
            self.metrics.parser_errors += 1
            raise AmbientikaResponseError("Houses response is not a list")
        return payload

    async def async_houses(self) -> list[object]:
        """Return house-level metadata such as address and timezone."""
        payload = await self._request("GET", "/House/houses")
        if not isinstance(payload, list):
            self.metrics.parser_errors += 1
            raise AmbientikaResponseError("Houses response is not a list")
        return payload

    async def async_device_status(self, serial_number: str) -> dict[str, Any]:
        """Return the latest status for a device."""
        payload = await self._request(
            "GET",
            "/Device/device-status",
            params={"deviceSerialNumber": serial_number},
            context=CONTEXT_DEVICE_STATUS,
        )
        if not isinstance(payload, dict):
            self.metrics.parser_errors += 1
            raise AmbientikaResponseError("Device status response is not an object")
        return payload

    async def async_house_devices_status(self, house_id: int) -> dict[str, Any]:
        """Return all available status packets for one house."""
        payload = await self._request(
            "GET",
            "/Device/house-devices-status",
            params={"houseId": str(house_id)},
            context=CONTEXT_HOUSE_STATUS,
        )
        if not isinstance(payload, dict):
            self.metrics.parser_errors += 1
            raise AmbientikaResponseError("House status response is not an object")
        return payload

    async def async_schedule(self, device_id: int) -> dict[str, Any]:
        """Return the weekly schedule configured for one device."""
        payload = await self._request(
            "GET", f"/Schedule/{device_id}", context=CONTEXT_SCHEDULE
        )
        if not isinstance(payload, dict):
            self.metrics.parser_errors += 1
            raise AmbientikaResponseError("Schedule response is not an object")
        return payload

    async def async_feature_flags(self) -> dict[str, bool]:
        """Return account-independent server feature flags."""
        payload = await self._request("GET", "/Users/feature-flags")
        if not isinstance(payload, dict):
            self.metrics.parser_errors += 1
            raise AmbientikaResponseError("Feature flags response is not an object")
        return {key: value for key, value in payload.items() if isinstance(value, bool)}

    async def async_change_mode(
        self,
        serial_number: str,
        *,
        operating_mode: str,
        fan_speed: str,
        humidity_level: str,
        light_sensor_level: str,
        schedule_mode: bool = False,
    ) -> None:
        """Validate and send the complete writable device state."""
        _validate_choice("operating mode", operating_mode, OPERATING_MODES)
        _validate_choice("fan speed", fan_speed, WRITABLE_FAN_SPEEDS)
        _validate_choice("humidity level", humidity_level, HUMIDITY_LEVELS)
        _validate_choice("light sensor level", light_sensor_level, LIGHT_SENSOR_LEVELS)
        await self._request(
            "POST",
            "/Device/change-mode",
            json_body={
                "deviceSerialNumber": serial_number,
                "operatingMode": operating_mode,
                "fanSpeed": fan_speed,
                "humidityLevel": humidity_level,
                "lightSensorLevel": light_sensor_level,
                "isScheduleMode": schedule_mode,
            },
            context=CONTEXT_COMMAND,
        )

    async def async_reset_filter(self, serial_number: str) -> None:
        """Request a filter status reset for a device."""
        await self._request(
            "GET",
            "/Device/reset-filter",
            params={"deviceSerialNumber": serial_number},
            context=CONTEXT_COMMAND,
        )

    async def _ensure_token(self) -> None:
        """Authenticate or refresh a JWT using one serialized operation."""
        if self._token is not None and not _expires_soon(self._expires_at):
            return
        async with self._auth_lock:
            if self._token is not None and not _expires_soon(self._expires_at):
                return
            if self._token is not None:
                try:
                    await self.async_refresh_token()
                    return
                except AmbientikaAuthError:
                    self._token = None
            await self.async_authenticate()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        json_body: dict[str, Any] | None = None,
        authenticated: bool = True,
        ensure_token: bool = True,
        recover_auth: bool = True,
        context: str | None = None,
    ) -> Any:
        """Perform one logical request with bounded transient retries."""
        if authenticated and ensure_token:
            await self._ensure_token()

        endpoint = _endpoint_label(path)
        auth_recovered = False
        for attempt in range(3):
            headers = {}
            if authenticated and self._token:
                headers["Authorization"] = f"Bearer {self._token}"
            started = time.monotonic()
            try:
                async with self._semaphore, asyncio.timeout(REQUEST_TIMEOUT):
                    response = await self._session.request(
                        method,
                        f"{self._base_url}{path}",
                        params=params,
                        json=json_body,
                        headers=headers,
                    )
                    payload = await _response_payload(response)
            except (TimeoutError, ClientError) as err:
                kind = "timeout" if isinstance(err, TimeoutError) else "connection"
                self.metrics.failed_requests += 1
                self.metrics.record_error(kind, endpoint=endpoint)
                LOGGER.debug(
                    "Ambientika %s %s attempt %d failed: %s",
                    method,
                    endpoint,
                    attempt + 1,
                    kind,
                )
                if attempt < 2:
                    await asyncio.sleep(_backoff(attempt))
                    continue
                raise AmbientikaConnectionError(
                    "Ambientika cloud is unreachable", kind=kind
                ) from err
            except AmbientikaResponseError as err:
                self.metrics.failed_requests += 1
                self.metrics.record_error(err.kind, endpoint=endpoint)
                raise
            finally:
                self.metrics.last_request_duration_ms = round(
                    (time.monotonic() - started) * 1000
                )

            status = response.status
            self.metrics.status_groups[f"{status // 100}xx"] += 1
            if 200 <= status < 300:
                self.metrics.successful_requests += 1
                return payload

            self.metrics.failed_requests += 1
            error = _http_error(
                status,
                payload,
                _content_type(response),
                authenticated=authenticated,
                context=context,
            )
            self.metrics.record_error(
                error.kind,
                endpoint=endpoint,
                status=status,
                trace_id=error.trace_id,
            )
            LOGGER.debug(
                "Ambientika %s %s attempt %d failed: %s",
                method,
                endpoint,
                attempt + 1,
                describe_error(error),
            )
            if status == 401 and authenticated and recover_auth and not auth_recovered:
                auth_recovered = True
                self._token = None
                await self._ensure_token()
                continue
            if status == 429:
                self.metrics.rate_limit_events += 1
                if attempt < 2:
                    await asyncio.sleep(_retry_after(response, attempt))
                    continue
            elif status >= 500 and attempt < 2:
                await asyncio.sleep(_backoff(attempt))
                continue
            raise error

        raise AmbientikaApiError("Ambientika request failed")

    def _store_token(
        self, user_id: int, token: str, expires_at: str
    ) -> AmbientikaToken:
        """Store and publish refreshed token data."""
        self._user_id = user_id
        self._token = token
        self._expires_at = expires_at
        session = AmbientikaToken(user_id=user_id, token=token, expires_at=expires_at)
        if self._token_callback is not None:
            self._token_callback(session)
        return session


async def _response_payload(response: ClientResponse) -> Any:
    """Parse JSON when present and ignore error response details."""
    if response.status == 204 or response.content_length == 0:
        return None
    text = await response.text()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        if 200 <= response.status < 300:
            raise AmbientikaResponseError("Ambientika returned invalid JSON") from None
        return None


def _endpoint_label(path: str) -> str:
    """Return a path label without identifiers for logs and diagnostics."""
    return "/".join("{id}" if part.isdigit() else part for part in path.split("/"))


def _content_type(response: ClientResponse) -> str:
    """Return the lower-case response content type, if any."""
    headers = getattr(response, "headers", None) or {}
    return str(headers.get("Content-Type", "")).lower()


def _error_text(payload: object) -> str | None:
    """Return the message of a JSON string or object error body."""
    if isinstance(payload, str):
        return payload.strip() or None
    if isinstance(payload, dict):
        for key in ("message", "title", "detail"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _trace_id(payload: object) -> str | None:
    """Return the RFC 9110 problem trace identifier, if present."""
    if isinstance(payload, dict):
        value = payload.get("traceId")
        if isinstance(value, str) and value:
            return value[:80]
    return None


def _http_error(
    status: int,
    payload: object,
    content_type: str,
    *,
    authenticated: bool,
    context: str | None,
) -> AmbientikaApiError:
    """Classify an unsuccessful HTTP response into a typed API error."""
    text = (_error_text(payload) or "").casefold()
    trace_id = _trace_id(payload)
    is_problem = "problem+json" in content_type or (
        isinstance(payload, dict) and ("traceId" in payload or "title" in payload)
    )

    if status == 401:
        return AmbientikaAuthError(
            "Ambientika rejected the credentials", status=status, trace_id=trace_id
        )
    if status == 403:
        return AmbientikaForbiddenError(
            "Ambientika denied access to this resource",
            status=status,
            trace_id=trace_id,
        )
    if status == 404:
        if context == CONTEXT_DEVICE_STATUS and (
            text.rstrip("!. ") == "status packet not found"
        ):
            return AmbientikaStatusUnavailableError(
                "Ambientika device has no current status packet",
                status=status,
                trace_id=trace_id,
            )
        if text == "house not found":
            return AmbientikaHouseNotFoundError(
                "Ambientika house was not found", status=status, trace_id=trace_id
            )
        if context == CONTEXT_SCHEDULE and is_problem:
            return AmbientikaNoScheduleError(
                "No Ambientika schedule is configured",
                status=status,
                trace_id=trace_id,
            )
        return AmbientikaNotFoundError(
            "Ambientika resource is not supported", status=status, trace_id=trace_id
        )
    if status == 406 and context == CONTEXT_COMMAND:
        return AmbientikaDeviceOfflineError(
            "Ambientika device did not respond", status=status, trace_id=trace_id
        )
    if status == 406 and context == CONTEXT_HOUSE_STATUS:
        return AmbientikaHouseNotConfiguredError(
            "Ambientika house has no configured master",
            status=status,
            trace_id=trace_id,
        )
    if status == 429:
        return AmbientikaRateLimitError(
            "Ambientika rate limit exceeded", status=status, trace_id=trace_id
        )
    if status >= 500:
        return AmbientikaServerError(
            "Ambientika cloud returned a server error",
            status=status,
            trace_id=trace_id,
        )
    if (
        status == 400
        and authenticated
        and context == CONTEXT_DEVICE_STATUS
        and text == "device not found"
    ):
        return AmbientikaDeviceNotFoundError(
            "Ambientika device was not found", status=status, trace_id=trace_id
        )
    return AmbientikaResponseError(
        f"Ambientika request failed with HTTP {status}",
        kind="http_4xx",
        status=status,
        trace_id=trace_id,
    )


def _date_text(value: object) -> str:
    """Normalize an API expiry date or choose a conservative fallback."""
    if isinstance(value, str) and value:
        return value
    return (datetime.now(UTC) + timedelta(minutes=30)).isoformat()


def _expires_soon(value: str | None) -> bool:
    """Return whether the token is absent, invalid, or close to expiry."""
    if value is None:
        return True
    try:
        expires = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=UTC)
    except ValueError:
        return True
    return expires <= datetime.now(UTC) + timedelta(minutes=5)


def _validate_choice(name: str, value: str, choices: tuple[str, ...]) -> None:
    """Reject writes not proven valid by the published API schema."""
    if value not in choices:
        raise ValueError(f"Unsupported {name}: {value}")


def _backoff(attempt: int) -> float:
    """Return bounded exponential backoff with jitter."""
    return float(min(8.0, (2**attempt) + random.uniform(0.0, 0.5)))


def _retry_after(response: ClientResponse, attempt: int) -> float:
    """Honor a small Retry-After value without blocking HA indefinitely."""
    value = response.headers.get("Retry-After")
    try:
        retry_seconds = float(str(value)) if value else _backoff(attempt)
        return min(15.0, max(0.0, retry_seconds))
    except ValueError:
        return _backoff(attempt)
