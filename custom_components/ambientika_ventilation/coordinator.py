"""DataUpdateCoordinator for the Ambientika integration."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    TRANSIENT_ERRORS,
    AmbientikaApiClient,
    AmbientikaApiError,
    AmbientikaAuthError,
    AmbientikaDeviceNotFoundError,
    AmbientikaDeviceOfflineError,
    AmbientikaForbiddenError,
    AmbientikaHouseNotFoundError,
    AmbientikaNotFoundError,
    AmbientikaResponseError,
    describe_error,
)
from .const import (
    DEFAULT_SCAN_INTERVAL,
    DISCOVERY_INTERVAL,
    DOMAIN,
    HUMIDITY_LEVELS,
    LIGHT_SENSOR_LEVELS,
    OPERATING_MODES,
    WRITABLE_FAN_SPEEDS,
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

# Discovery retries after a failure: 5, 10, 30 min, then doubling up to 6 h.
DISCOVERY_RETRY_DELAYS = (
    timedelta(minutes=5),
    timedelta(minutes=10),
    timedelta(minutes=30),
)
MAX_FAILURE_CAUSES = 10

# Writable values the official app may report but never accepts back (for
# example the automatic "Night" fan speed) are replaced by the last value seen
# for the device, then by these defaults. Missing values are never guessed.
WRITE_FALLBACKS: dict[str, str] = {
    "fan_speed": "Low",
    "humidity_level": "Normal",
}
WRITABLE_CHOICES: dict[str, tuple[str, ...]] = {
    "fan_speed": WRITABLE_FAN_SPEEDS,
    "humidity_level": HUMIDITY_LEVELS,
    "light_sensor_level": LIGHT_SENSOR_LEVELS,
}
# Turbo is only remembered while it is reported; it is never used as fallback.
CACHEABLE_CHOICES: dict[str, tuple[str, ...]] = {
    **WRITABLE_CHOICES,
    "fan_speed": WRITABLE_FAN_SPEEDS[:3],
}


def mask_serial(serial: str) -> str:
    """Return a serial number reduced to the suffix shown in diagnostics."""
    return f"…{serial[-4:]}" if len(serial) >= 4 else "…"


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
        self._last_writable: dict[str, dict[str, str]] = {}
        self._cycle_causes: list[str] = []
        self.discovery_failures = 0
        self.next_discovery_attempt: datetime | None = None
        self._rediscovery_requested = False
        self.last_successful_update: datetime | None = None
        self.last_update_duration_ms: int | None = None
        self.last_partial_failures = 0
        self.last_failure_causes: tuple[str, ...] = ()

    async def _async_update_data(self) -> AmbientikaData:
        """Refresh metadata when due and poll every supported device."""
        started = time.monotonic()
        self._cycle_causes = []
        try:
            if self._discovery_due:
                try:
                    await self._async_discover()
                except AmbientikaAuthError:
                    raise
                except AmbientikaApiError as err:
                    if not self._known_devices:
                        self.last_failure_causes = (
                            f"discovery: {describe_error(err)}",
                        )
                        raise UpdateFailed(f"{err} ({describe_error(err)})") from err
                    self._discovery_failed(err)
            old_data = self.data or AmbientikaData()
            statuses, failed_devices = await self._async_fetch_statuses(old_data)
        except AmbientikaAuthError as err:
            raise ConfigEntryAuthFailed from err
        except AmbientikaApiError as err:
            self.last_failure_causes = (describe_error(err),)
            raise UpdateFailed(f"{err} ({describe_error(err)})") from err
        finally:
            self.last_update_duration_ms = round((time.monotonic() - started) * 1000)

        self.last_failure_causes = tuple(self._cycle_causes)
        if failed_devices and not set(statuses).difference(failed_devices):
            causes = "; ".join(self._cycle_causes) or "no usable status packets"
            raise UpdateFailed(f"No Ambientika device status could be read ({causes})")

        newly_failed = failed_devices - self._failed_devices
        recovered = self._failed_devices - failed_devices
        if newly_failed:
            LOGGER.warning(
                "Unable to update %d Ambientika device(s) (%s); affected entities "
                "are temporarily unavailable",
                len(newly_failed),
                "; ".join(self._cycle_causes) or "no usable status packets",
            )
        if recovered:
            LOGGER.info("Recovered updates for %d Ambientika device(s)", len(recovered))
        self._failed_devices = failed_devices

        merged: dict[str, AmbientikaDeviceData] = {}
        for serial, device in self._known_devices.items():
            old_status = old_data.devices.get(serial)
            status = (
                statuses.get(serial)
                or (old_status.status if old_status is not None else None)
                if device.role != "NotConfigured"
                else None
            )
            self._remember_writable(serial, status)
            merged[serial] = AmbientikaDeviceData(
                device=device,
                status=status,
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
        if self._last_discovery is None:
            # Never discovered: setup retries are paced by Home Assistant.
            return True
        now = datetime.now(UTC)
        next_attempt = self.next_discovery_attempt
        if next_attempt is not None and now < next_attempt:
            return False
        if self._rediscovery_requested:
            return True
        return now - self._last_discovery >= DISCOVERY_INTERVAL

    def _discovery_failed(self, err: AmbientikaApiError) -> None:
        """Back off discovery retries while keeping the previous device list."""
        self.discovery_failures += 1
        if self.discovery_failures <= len(DISCOVERY_RETRY_DELAYS):
            delay = DISCOVERY_RETRY_DELAYS[self.discovery_failures - 1]
        else:
            delay = min(
                DISCOVERY_RETRY_DELAYS[-1]
                * 2 ** (self.discovery_failures - len(DISCOVERY_RETRY_DELAYS)),
                DISCOVERY_INTERVAL,
            )
        self.next_discovery_attempt = datetime.now(UTC) + delay
        self._note_failure("discovery", err)
        log = LOGGER.warning if self.discovery_failures == 1 else LOGGER.debug
        log(
            "Unable to refresh Ambientika device discovery (%s); using the previous "
            "device list and retrying in %d min",
            describe_error(err),
            int(delay.total_seconds() // 60),
        )

    def _note_failure(self, source: str, err: BaseException) -> None:
        """Record a non-sensitive failure cause for logs and diagnostics."""
        cause = f"{source}: {describe_error(err)}"
        LOGGER.debug("Ambientika update failure: %s", cause)
        if len(self._cycle_causes) < MAX_FAILURE_CAUSES:
            self._cycle_causes.append(cause)

    def _remember_writable(self, serial: str, status: AmbientikaStatus | None) -> None:
        """Remember the last writable settings reported by a device."""
        if status is None:
            return
        cache = self._last_writable.setdefault(serial, {})
        for field, choices in CACHEABLE_CHOICES.items():
            value = getattr(status, field)
            if value in choices:
                cache[field] = value

    def _writable_value(
        self, serial: str, field: str, reported: str | None
    ) -> str | None:
        """Return a value that may be written back for an unchanged setting."""
        if reported in WRITABLE_CHOICES[field]:
            return reported
        cached = self._last_writable.get(serial, {}).get(field)
        if cached is not None:
            return cached
        if reported is None:
            return None
        return WRITE_FALLBACKS.get(field)

    async def _async_discover(self) -> None:
        """Discover all current devices and optional server capabilities."""
        houses = await self.api.async_houses_info()
        house_metadata: object = None
        try:
            house_metadata = await self.api.async_houses()
        except (AmbientikaForbiddenError, AmbientikaNotFoundError, AmbientikaApiError):
            LOGGER.debug("Optional Ambientika house metadata is unavailable")
        self._known_devices = parse_houses(houses, house_metadata)
        # Retry unsupported endpoints after discovery (normally every six hours),
        # including devices whose firmware or role changed since the last probe.
        self._unsupported_status.clear()
        self._last_discovery = datetime.now(UTC)
        self._rediscovery_requested = False
        if self.discovery_failures:
            LOGGER.info("Ambientika device discovery recovered")
        self.discovery_failures = 0
        self.next_discovery_attempt = None
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
        """Fetch batched house statuses and fall back to individual devices.

        Individual requests are only used for devices whose house batch was
        readable but did not contain a usable packet. When the batch failed
        with a temporary error (timeout, transport error, HTTP 5xx or 429),
        one device per house probes whether individual reads still work
        before the remaining devices are requested; otherwise their last
        values are kept.
        """
        readable_serials = {
            serial
            for serial, device in self._known_devices.items()
            if device.role != "NotConfigured"
        }
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
        transient_houses: set[int] = set()
        for house_id, batch_result in zip(house_ids, batch_results, strict=True):
            if isinstance(batch_result, dict):
                statuses.update(
                    (serial, status)
                    for serial, status in parse_house_statuses(batch_result).items()
                    if serial in readable_serials and status.has_values
                )
            elif isinstance(batch_result, AmbientikaAuthError):
                raise batch_result
            elif isinstance(batch_result, Exception):
                self._note_failure("house status", batch_result)
                if isinstance(batch_result, AmbientikaHouseNotFoundError):
                    self._rediscovery_requested = True
                if isinstance(batch_result, TRANSIENT_ERRORS):
                    transient_houses.add(house_id)

        self._unsupported_status.difference_update(statuses)
        pending = sorted(readable_serials - self._unsupported_status - statuses.keys())
        direct: list[str] = []
        deferred: dict[int, list[str]] = {}
        for serial in pending:
            device_house = self._known_devices[serial].house_id
            if device_house is not None and device_house in transient_houses:
                deferred.setdefault(device_house, []).append(serial)
            else:
                direct.append(serial)

        results = await self._async_fetch_many(
            direct + [serials[0] for serials in deferred.values()]
        )
        skipped: set[str] = set()
        for serials in deferred.values():
            if isinstance(results.get(serials[0]), AmbientikaStatus):
                results.update(await self._async_fetch_many(serials[1:]))
            else:
                skipped.update(serials[1:])

        failed_devices = self._unsupported_status & readable_serials
        for serial in pending:
            device_result = results.get(serial)
            if isinstance(device_result, AmbientikaStatus):
                statuses[serial] = device_result
                continue
            if isinstance(device_result, AmbientikaAuthError):
                raise device_result
            if isinstance(device_result, BaseException):
                self._note_failure(f"device {mask_serial(serial)}", device_result)
            if isinstance(
                device_result, AmbientikaNotFoundError | AmbientikaForbiddenError
            ):
                self._unsupported_status.add(serial)
                if isinstance(device_result, AmbientikaDeviceNotFoundError):
                    self._rediscovery_requested = True
            failed_devices.add(serial)
            old_device_data = old_data.devices.get(serial)
            if old_device_data is not None and old_device_data.status is not None:
                statuses[serial] = old_device_data.status
        if skipped:
            LOGGER.debug(
                "Skipped %d individual Ambientika status request(s) during a "
                "house status outage",
                len(skipped),
            )
        return statuses, failed_devices

    async def _async_fetch_many(
        self, serials: list[str]
    ) -> dict[str, AmbientikaStatus | BaseException]:
        """Fetch several individual device statuses concurrently."""
        results = await asyncio.gather(
            *(self._async_fetch_one(serial) for serial in serials),
            return_exceptions=True,
        )
        return dict(zip(serials, results, strict=True))

    async def _async_fetch_one(self, serial: str) -> AmbientikaStatus:
        """Fetch and parse one device status."""
        payload = await self.api.async_device_status(serial)
        status = parse_status(payload, serial)
        if status.serial_number != serial or not status.has_values:
            raise AmbientikaResponseError("Device status is missing or mismatched")
        return status

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
            "fan_speed": fan_speed
            or self._writable_value(serial, "fan_speed", status.fan_speed),
            "humidity_level": (
                "Normal"
                if operating_mode == "AwayHome" and humidity_level is None
                else humidity_level
                or self._writable_value(serial, "humidity_level", status.humidity_level)
            ),
            "light_sensor_level": light_sensor_level
            or self._writable_value(
                serial, "light_sensor_level", status.light_sensor_level
            ),
        }
        if not all(isinstance(value, str) for value in values.values()) or (
            target_mode not in OPERATING_MODES
        ):
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
            self.config_entry.async_start_reauth(self.hass)
            raise action_error("auth_failed") from err
        except AmbientikaDeviceOfflineError as err:
            LOGGER.debug(
                "Ambientika device %s did not respond: %s",
                mask_serial(serial),
                describe_error(err),
            )
            raise action_error("device_offline") from err
        except (AmbientikaApiError, ValueError) as err:
            LOGGER.debug(
                "Ambientika command for %s failed: %s",
                mask_serial(serial),
                describe_error(err),
            )
            raise action_error("cloud_command_failed") from err

        self._remember_writable(serial, refreshed)
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
            self.config_entry.async_start_reauth(self.hass)
            raise action_error("auth_failed") from err
        except AmbientikaDeviceOfflineError as err:
            raise action_error("device_offline") from err
        except AmbientikaApiError as err:
            LOGGER.debug(
                "Ambientika filter reset for %s failed: %s",
                mask_serial(serial),
                describe_error(err),
            )
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
