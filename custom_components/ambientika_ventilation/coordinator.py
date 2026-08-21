"""DataUpdateCoordinator for the Ambientika integration."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import replace
from datetime import UTC, datetime

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    AmbientikaApiClient,
    AmbientikaApiError,
    AmbientikaAuthError,
    AmbientikaForbiddenError,
    AmbientikaNotFoundError,
)
from .const import DEFAULT_SCAN_INTERVAL, DISCOVERY_INTERVAL, DOMAIN
from .models import (
    AmbientikaData,
    AmbientikaDevice,
    AmbientikaDeviceData,
    AmbientikaStatus,
    parse_houses,
    parse_status,
)

LOGGER = logging.getLogger(__name__)


class AmbientikaCoordinator(DataUpdateCoordinator[AmbientikaData]):
    """Coordinate discovery, polling, partial failures, and verified writes."""

    config_entry: ConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        api: AmbientikaApiClient,
    ) -> None:
        """Initialize the account coordinator."""
        super().__init__(
            hass,
            LOGGER,
            name=DOMAIN,
            config_entry=entry,
            update_interval=DEFAULT_SCAN_INTERVAL,
        )
        self.api = api
        self._known_devices: dict[str, AmbientikaDevice] = {}
        self._unsupported_status: set[str] = set()
        self._feature_flags: dict[str, bool] = {}
        self._last_discovery: datetime | None = None
        self.last_successful_update: datetime | None = None
        self.last_update_duration_ms: int | None = None
        self.last_partial_failures = 0

    async def _async_update_data(self) -> AmbientikaData:
        """Refresh metadata when due and poll every supported device."""
        started = time.monotonic()
        try:
            if self._discovery_due:
                await self._async_discover()
            old_data = self.data or AmbientikaData()
            statuses, failures = await self._async_fetch_statuses(old_data)
        except AmbientikaAuthError as err:
            raise ConfigEntryAuthFailed from err
        except AmbientikaApiError as err:
            if self.data is not None:
                self.last_partial_failures += 1
                return self.data
            raise UpdateFailed(str(err)) from err
        finally:
            self.last_update_duration_ms = round((time.monotonic() - started) * 1000)

        if failures and not statuses and self.data is None:
            raise UpdateFailed("No Ambientika device status could be read")

        merged: dict[str, AmbientikaDeviceData] = {}
        for serial, device in self._known_devices.items():
            old_status = old_data.devices.get(serial)
            merged[serial] = AmbientikaDeviceData(
                device=device,
                status=statuses.get(serial)
                or (old_status.status if old_status is not None else None),
            )

        self.last_partial_failures = failures
        self.last_successful_update = datetime.now(UTC)
        return AmbientikaData(devices=merged, feature_flags=dict(self._feature_flags))

    @property
    def _discovery_due(self) -> bool:
        """Return whether static metadata should be refreshed."""
        return self._last_discovery is None or (
            datetime.now(UTC) - self._last_discovery >= DISCOVERY_INTERVAL
        )

    async def _async_discover(self) -> None:
        """Discover all current devices and optional server capabilities."""
        houses = await self.api.async_houses_info()
        self._known_devices = parse_houses(houses)
        self._last_discovery = datetime.now(UTC)
        feature_flags: dict[str, bool] = {}
        try:
            feature_flags = await self.api.async_feature_flags()
        except (AmbientikaForbiddenError, AmbientikaNotFoundError, AmbientikaApiError):
            LOGGER.debug("Optional Ambientika feature flags are unavailable")
        if feature_flags:
            self._feature_flags = feature_flags

    async def _async_fetch_statuses(
        self, old_data: AmbientikaData
    ) -> tuple[dict[str, AmbientikaStatus], int]:
        """Fetch statuses concurrently within the API client's request limit."""
        serials = [
            serial
            for serial in self._known_devices
            if serial not in self._unsupported_status
        ]
        results = await asyncio.gather(
            *(self._async_fetch_one(serial) for serial in serials),
            return_exceptions=True,
        )
        statuses: dict[str, AmbientikaStatus] = {}
        failures = 0
        for serial, result in zip(serials, results, strict=True):
            if isinstance(result, AmbientikaStatus):
                statuses[serial] = result
                continue
            if isinstance(result, AmbientikaNotFoundError | AmbientikaForbiddenError):
                self._unsupported_status.add(serial)
            elif isinstance(result, AmbientikaAuthError):
                raise result
            failures += 1
            old_device_data = old_data.devices.get(serial)
            if old_device_data is not None and old_device_data.status is not None:
                statuses[serial] = old_device_data.status
        return statuses, failures

    async def _async_fetch_one(self, serial: str) -> AmbientikaStatus:
        """Fetch and parse one device status."""
        payload = await self.api.async_device_status(serial)
        return parse_status(payload, serial)

    async def async_write_state(
        self,
        serial: str,
        *,
        operating_mode: str | None = None,
        fan_speed: str | None = None,
        humidity_level: str | None = None,
        light_sensor_level: str | None = None,
    ) -> None:
        """Write a complete validated state and read the actual result back."""
        device_data = self.data.devices.get(serial) if self.data else None
        status = device_data.status if device_data else None
        if device_data is None or status is None:
            raise HomeAssistantError("The device has no readable state")

        values = {
            "operating_mode": operating_mode or status.operating_mode,
            "fan_speed": fan_speed or status.fan_speed,
            "humidity_level": humidity_level or status.humidity_level,
            "light_sensor_level": light_sensor_level or status.light_sensor_level,
        }
        if not all(isinstance(value, str) for value in values.values()):
            raise HomeAssistantError(
                "The device did not report a complete writable state"
            )

        try:
            await self.api.async_change_mode(serial, **values)  # type: ignore[arg-type]
            refreshed = await self._async_fetch_one(serial)
        except AmbientikaAuthError as err:
            raise ConfigEntryAuthFailed from err
        except (AmbientikaApiError, ValueError) as err:
            raise HomeAssistantError(str(err)) from err

        devices = dict(self.data.devices)
        devices[serial] = replace(device_data, status=refreshed)
        self.async_set_updated_data(
            AmbientikaData(devices=devices, feature_flags=self.data.feature_flags)
        )

    async def async_reset_filter(self, serial: str) -> None:
        """Reset the filter and verify the resulting device state."""
        try:
            await self.api.async_reset_filter(serial)
            refreshed = await self._async_fetch_one(serial)
        except AmbientikaAuthError as err:
            raise ConfigEntryAuthFailed from err
        except AmbientikaApiError as err:
            raise HomeAssistantError(str(err)) from err

        if self.data is None or serial not in self.data.devices:
            return
        devices = dict(self.data.devices)
        devices[serial] = replace(devices[serial], status=refreshed)
        self.async_set_updated_data(
            AmbientikaData(devices=devices, feature_flags=self.data.feature_flags)
        )
