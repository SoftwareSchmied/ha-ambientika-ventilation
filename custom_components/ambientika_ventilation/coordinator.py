"""DataUpdateCoordinator for the Ambientika integration."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import replace
from datetime import UTC, datetime

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    AmbientikaApiClient,
    AmbientikaApiError,
    AmbientikaAuthError,
    AmbientikaForbiddenError,
    AmbientikaNotFoundError,
)
from .const import (
    DEFAULT_SCAN_INTERVAL,
    DISCOVERY_INTERVAL,
    DOMAIN,
    mode_allows_setting,
    operating_modes_for_device,
)
from .errors import action_error, validation_error
from .models import (
    AmbientikaData,
    AmbientikaDevice,
    AmbientikaDeviceData,
    AmbientikaSchedule,
    AmbientikaStatus,
    is_controllable_device,
    parse_house_statuses,
    parse_houses,
    parse_schedule,
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
        self._schedules: dict[str, AmbientikaSchedule] = {}
        self._last_discovery: datetime | None = None
        self._failed_devices: set[str] = set()
        self.last_successful_update: datetime | None = None
        self.last_update_duration_ms: int | None = None
        self.last_partial_failures = 0

    async def _async_update_data(self) -> AmbientikaData:
        """Refresh metadata when due and poll every supported device."""
        started = time.monotonic()
        try:
            if self._discovery_due:
                try:
                    await self._async_discover()
                except AmbientikaAuthError:
                    raise
                except AmbientikaApiError as err:
                    if not self._known_devices:
                        raise UpdateFailed(str(err)) from err
                    LOGGER.warning(
                        "Unable to refresh Ambientika device discovery; using the "
                        "previous device list"
                    )
            old_data = self.data or AmbientikaData()
            statuses, failed_devices = await self._async_fetch_statuses(old_data)
        except AmbientikaAuthError as err:
            raise ConfigEntryAuthFailed from err
        except AmbientikaApiError as err:
            raise UpdateFailed(str(err)) from err
        finally:
            self.last_update_duration_ms = round((time.monotonic() - started) * 1000)

        if failed_devices and not set(statuses).difference(failed_devices):
            raise UpdateFailed("No Ambientika device status could be read")

        newly_failed = failed_devices - self._failed_devices
        recovered = self._failed_devices - failed_devices
        if newly_failed:
            LOGGER.warning(
                "Unable to update %d Ambientika device(s); affected entities are "
                "temporarily unavailable",
                len(newly_failed),
            )
        if recovered:
            LOGGER.info("Recovered updates for %d Ambientika device(s)", len(recovered))
        self._failed_devices = failed_devices

        merged: dict[str, AmbientikaDeviceData] = {}
        for serial, device in self._known_devices.items():
            old_status = old_data.devices.get(serial)
            merged[serial] = AmbientikaDeviceData(
                device=device,
                status=statuses.get(serial)
                or (old_status.status if old_status is not None else None),
                schedule=self._schedules.get(serial)
                or (old_status.schedule if old_status is not None else None),
            )

        self.last_partial_failures = len(failed_devices)
        self.last_successful_update = datetime.now(UTC)
        return AmbientikaData(
            devices=merged,
            feature_flags=dict(self._feature_flags),
            failed_devices=frozenset(failed_devices),
        )

    @property
    def _discovery_due(self) -> bool:
        """Return whether static metadata should be refreshed."""
        return self._last_discovery is None or (
            datetime.now(UTC) - self._last_discovery >= DISCOVERY_INTERVAL
        )

    async def _async_discover(self) -> None:
        """Discover all current devices and optional server capabilities."""
        houses = await self.api.async_houses_info()
        house_metadata: object = None
        try:
            house_metadata = await self.api.async_houses()
        except (AmbientikaForbiddenError, AmbientikaNotFoundError, AmbientikaApiError):
            LOGGER.debug("Optional Ambientika house metadata is unavailable")
        self._known_devices = parse_houses(houses, house_metadata)
        self._last_discovery = datetime.now(UTC)
        feature_flags: dict[str, bool] = {}
        try:
            feature_flags = await self.api.async_feature_flags()
        except (AmbientikaForbiddenError, AmbientikaNotFoundError, AmbientikaApiError):
            LOGGER.debug("Optional Ambientika feature flags are unavailable")
        if feature_flags:
            self._feature_flags = feature_flags
        await self._async_fetch_schedules()

    async def _async_fetch_schedules(self) -> None:
        """Refresh optional weekly schedules without blocking discovery."""
        if self._feature_flags.get("weeklyScheduler") is False:
            self._schedules = {}
            return

        candidates = [
            (serial, device.device_id)
            for serial, device in self._known_devices.items()
            if device.device_id is not None and is_controllable_device(device)
        ]
        results = await asyncio.gather(
            *(self.api.async_schedule(device_id) for _, device_id in candidates),
            return_exceptions=True,
        )
        schedules: dict[str, AmbientikaSchedule] = {}
        for (serial, _device_id), result in zip(candidates, results, strict=True):
            if isinstance(result, dict):
                schedules[serial] = parse_schedule(result)
            elif isinstance(result, AmbientikaAuthError):
                raise result
            elif not isinstance(result, AmbientikaNotFoundError):
                previous = self._schedules.get(serial)
                if previous is not None:
                    schedules[serial] = previous
        self._schedules = schedules

    async def _async_fetch_statuses(
        self, old_data: AmbientikaData
    ) -> tuple[dict[str, AmbientikaStatus], set[str]]:
        """Fetch batched house statuses and fall back to individual devices."""
        eligible_serials = [
            serial
            for serial, device in self._known_devices.items()
            if serial not in self._unsupported_status and is_controllable_device(device)
        ]
        house_ids = sorted(
            {
                device.house_id
                for device in self._known_devices.values()
                if device.house_id is not None
            }
        )
        batch_results = await asyncio.gather(
            *(self.api.async_house_devices_status(house_id) for house_id in house_ids),
            return_exceptions=True,
        )
        statuses: dict[str, AmbientikaStatus] = {}
        for batch_result in batch_results:
            if isinstance(batch_result, dict):
                statuses.update(
                    (serial, status)
                    for serial, status in parse_house_statuses(batch_result).items()
                    if serial in self._known_devices
                )
            elif isinstance(batch_result, AmbientikaAuthError):
                raise batch_result

        self._unsupported_status.difference_update(statuses)
        serials = [serial for serial in eligible_serials if serial not in statuses]
        results = await asyncio.gather(
            *(self._async_fetch_one(serial) for serial in serials),
            return_exceptions=True,
        )
        failed_devices: set[str] = set(self._unsupported_status)
        for serial, device_result in zip(serials, results, strict=True):
            if isinstance(device_result, AmbientikaStatus):
                statuses[serial] = device_result
                continue
            if isinstance(
                device_result, AmbientikaNotFoundError | AmbientikaForbiddenError
            ):
                self._unsupported_status.add(serial)
            elif isinstance(device_result, AmbientikaAuthError):
                raise device_result
            failed_devices.add(serial)
            old_device_data = old_data.devices.get(serial)
            if old_device_data is not None and old_device_data.status is not None:
                statuses[serial] = old_device_data.status
        return statuses, failed_devices

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
        schedule_mode: bool | None = None,
    ) -> None:
        """Write a complete validated state and read the actual result back."""
        device_data = self.data.devices.get(serial) if self.data else None
        status = device_data.status if device_data else None
        if device_data is None or status is None:
            raise validation_error("device_unavailable")

        if not is_controllable_device(device_data.device, status):
            raise validation_error("device_not_controllable")

        requested_settings = {
            "operating_mode": operating_mode,
            "fan_speed": fan_speed,
            "humidity_level": humidity_level,
            "light_sensor_level": light_sensor_level,
        }
        explicitly_changed = {
            field for field, value in requested_settings.items() if value is not None
        }

        if explicitly_changed and status.filter_status == "Bad":
            raise validation_error("controls_locked_filter")
        if (
            explicitly_changed
            and status.schedule_state == "On"
            and schedule_mode is not False
        ):
            raise validation_error("controls_locked_schedule")

        target_mode = operating_mode or status.operating_mode
        device_type = device_data.device.device_type or status.device_type
        allowed_modes = operating_modes_for_device(device_type)
        if operating_mode is not None and operating_mode not in allowed_modes:
            raise validation_error(
                "unsupported_operating_mode",
                placeholders={"mode": operating_mode},
            )
        for field in explicitly_changed - {"operating_mode"}:
            if not mode_allows_setting(target_mode, field):
                raise validation_error(
                    "setting_unavailable",
                    placeholders={
                        "setting": field.replace("_", " ").title(),
                        "mode": target_mode or "the current",
                    },
                )

        if schedule_mode is not None and status.schedule_state in (
            None,
            "NotAvailable",
        ):
            raise validation_error("schedule_unsupported")

        values = {
            "operating_mode": target_mode,
            "fan_speed": fan_speed or status.fan_speed,
            "humidity_level": (
                "Normal"
                if operating_mode == "AwayHome" and humidity_level is None
                else humidity_level or status.humidity_level
            ),
            "light_sensor_level": light_sensor_level or status.light_sensor_level,
        }
        if not all(isinstance(value, str) for value in values.values()):
            raise validation_error("incomplete_writable_state")

        try:
            await self.api.async_change_mode(
                serial,
                **values,  # type: ignore[arg-type]
                schedule_mode=(
                    schedule_mode
                    if schedule_mode is not None
                    else status.schedule_state == "On"
                ),
            )
            refreshed = await self._async_fetch_one(serial)
        except AmbientikaAuthError as err:
            raise ConfigEntryAuthFailed from err
        except (AmbientikaApiError, ValueError) as err:
            raise action_error("cloud_command_failed") from err

        devices = dict(self.data.devices)
        devices[serial] = replace(device_data, status=refreshed)
        self.async_set_updated_data(
            AmbientikaData(
                devices=devices,
                feature_flags=self.data.feature_flags,
                failed_devices=self.data.failed_devices - {serial},
            )
        )

    async def async_reset_filter(self, serial: str) -> None:
        """Reset the filter and verify the resulting device state."""
        device_data = self.data.devices.get(serial) if self.data else None
        if device_data is None or not is_controllable_device(
            device_data.device, device_data.status
        ):
            raise validation_error("device_not_controllable")
        try:
            await self.api.async_reset_filter(serial)
            refreshed = await self._async_fetch_one(serial)
        except AmbientikaAuthError as err:
            raise ConfigEntryAuthFailed from err
        except AmbientikaApiError as err:
            raise action_error("filter_reset_failed") from err

        if self.data is None or serial not in self.data.devices:
            return
        devices = dict(self.data.devices)
        devices[serial] = replace(devices[serial], status=refreshed)
        self.async_set_updated_data(
            AmbientikaData(
                devices=devices,
                feature_flags=self.data.feature_flags,
                failed_devices=self.data.failed_devices - {serial},
            )
        )
