"""DataUpdateCoordinator for the Ambientika integration."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import device_registry as dr
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
    AmbientikaStatusUnavailableError,
    describe_error,
)
from .const import (
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DISCOVERY_INTERVAL,
    DOMAIN,
    EVENT_DEVICE_CHANGED,
    FAST_POLL_DURATION,
    HUMIDITY_LEVELS,
    LIGHT_SENSOR_LEVELS,
    MAX_BACKOFF_INTERVAL,
    MIN_SCAN_INTERVAL,
    OPERATING_MODES,
    WRITABLE_FAN_SPEEDS,
    mode_allows_setting,
    operating_modes_for_device,
)
from .errors import (
    action_error,
    auth_failed_error,
    update_failed,
    validation_error,
)
from .models import (
    AmbientikaData,
    AmbientikaDevice,
    AmbientikaDeviceData,
    AmbientikaDeviceMetadata,
    AmbientikaSchedule,
    AmbientikaStatus,
    is_controllable_device,
    parse_house_status_metadata,
    parse_house_statuses,
    parse_houses,
    parse_schedule,
    parse_status,
)
from .stale import StaleDeviceTracker

LOGGER = logging.getLogger(__name__)

# Discovery retries after a failure: 5, 10, 30 min, then doubling up to 6 h.
DISCOVERY_RETRY_DELAYS = (
    timedelta(minutes=5),
    timedelta(minutes=10),
    timedelta(minutes=30),
)
MAX_FAILURE_CAUSES = 10

# A device whose status endpoint answers 403/404 is probed again after 1, 2, 5
# and 15 minutes and only reported as unsupported after this many consecutive
# failures, so that a short outage does not disable it until the next discovery.
DEVICE_RETRY_DELAYS = (60.0, 120.0, 300.0, 900.0)
UNSUPPORTED_AFTER_FAILURES = 4

# The cloud reflects an accepted command after about 0.2-3.1 s (median 2.1 s);
# an immediate read-back usually still returns the previous state.
READBACK_DELAY = 3.0
READBACK_ATTEMPTS = 2
# Commands arriving within this window are merged into one request.
WRITE_BATCH_DELAY = 0.4
# A poll started this soon after a command may still carry the old state.
STATUS_SETTLE_TIME = 3.0

MAX_CYCLE_HISTORY = 20
MAX_FIRMWARE_HISTORY = 10
DEVICE_METADATA_FIELDS = (
    "name",
    "role",
    "micro_firmware",
    "radio_firmware",
    "radio_at_firmware",
    "zone_index",
)

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


def firmware_text(device: AmbientikaDevice) -> str | None:
    """Return the combined firmware versions shown in the device registry."""
    parts = (device.micro_firmware, device.radio_firmware, device.radio_at_firmware)
    return " / ".join(part for part in parts if part) or None


@dataclass(slots=True)
class _PendingWrite:
    """Commands for one device that are collected before they are sent."""

    fields: dict[str, Any]
    future: asyncio.Future[None]


@dataclass(frozen=True, slots=True)
class _WritePlan:
    """A validated command: the complete state to send and what to expect."""

    values: dict[str, str]
    expected: dict[str, str]
    schedule_mode: bool
    status: AmbientikaStatus


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
        configured = entry.options.get(
            CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL.total_seconds()
        )
        try:
            seconds = max(MIN_SCAN_INTERVAL, int(configured))
        except (TypeError, ValueError):
            seconds = int(DEFAULT_SCAN_INTERVAL.total_seconds())
        self.base_interval = timedelta(seconds=seconds)
        super().__init__(
            hass,
            LOGGER,
            name=DOMAIN,
            config_entry=entry,
            update_interval=self.base_interval,
            # Polls without a change do not wake any entity.
            always_update=False,
        )
        self.api = api
        self._known_devices: dict[str, AmbientikaDevice] = {}
        self._unsupported_status: set[str] = set()
        self._status_errors: dict[str, int] = {}
        self._status_retry_at: dict[str, float] = {}
        self._feature_flags: dict[str, bool] = {}
        self._schedules: dict[str, AmbientikaSchedule] = {}
        self._schedule_due: set[str] = set()
        self._schedule_states: dict[str, str] = {}
        self._last_discovery: datetime | None = None
        self._failed_devices: set[str] = set()
        self._last_writable: dict[str, dict[str, str]] = {}
        self._cycle_causes: list[str] = []
        self._cycle_offline: set[str] = set()
        self._cycle_metadata: dict[str, AmbientikaDeviceMetadata] = {}
        self._cycle_outage = True
        self._unknown_batch_serials: set[str] = set()
        self._pending_writes: dict[str, _PendingWrite] = {}
        self._write_locks: dict[str, asyncio.Lock] = {}
        self._generation: dict[str, int] = {}
        self._command_time: dict[str, float] = {}
        self._fast_until = 0.0
        self._topology: object = None
        self._topology_listeners: dict[int, CALLBACK_TYPE] = {}
        self._topology_listener_id = 0
        self.consecutive_failures = 0
        self.offline_devices: set[str] = set()
        self.offline_transitions = 0
        self.discovery_failures = 0
        self.next_discovery_attempt: datetime | None = None
        self._rediscovery_requested = False
        self.stale_devices = StaleDeviceTracker(hass, entry)
        self.last_successful_update: datetime | None = None
        self.last_update_duration_ms: int | None = None
        self.cycle_durations_ms: deque[int] = deque(maxlen=MAX_CYCLE_HISTORY)
        self.firmware_history: dict[str, list[dict[str, Any]]] = {}
        self.device_changes = 0
        self.discarded_polls = 0
        self.last_partial_failures = 0
        self.last_failure_causes: tuple[str, ...] = ()

    # ------------------------------------------------------------------
    # Topology signal
    # ------------------------------------------------------------------

    @callback
    def async_add_topology_listener(
        self, update_callback: CALLBACK_TYPE
    ) -> CALLBACK_TYPE:
        """Call back when devices or capabilities that create entities change.

        Platforms use this instead of the generic update listener so that
        ordinary value updates do not run their discovery code.
        """
        self._topology_listener_id += 1
        listener_id = self._topology_listener_id
        self._topology_listeners[listener_id] = update_callback

        @callback
        def remove() -> None:
            self._topology_listeners.pop(listener_id, None)

        return remove

    @callback
    def async_update_listeners(self) -> None:
        """Notify entities and, if the device topology changed, platforms."""
        super().async_update_listeners()
        topology = self._topology_signature()
        if topology == self._topology:
            return
        self._topology = topology
        for listener in list(self._topology_listeners.values()):
            listener()

    def _topology_signature(self) -> object:
        """Summarize everything that decides which entities exist."""
        if self.data is None:
            return None
        entries = []
        for serial, device_data in self.data.devices.items():
            status = device_data.status
            entries.append(
                (
                    serial,
                    device_data.device,
                    status is not None,
                    status.device_role if status else None,
                    status.schedule_state not in (None, "NotAvailable")
                    if status
                    else None,
                    status.light_sensor_level not in (None, "NotAvailable")
                    if status
                    else None,
                    device_data.schedule is not None,
                )
            )
        return tuple(sorted(entries, key=lambda entry: entry[0]))

    # ------------------------------------------------------------------
    # Polling
    # ------------------------------------------------------------------

    async def _async_update_data(self) -> AmbientikaData:
        """Poll the cloud and adapt the interval to success or failure."""
        self._cycle_outage = True
        try:
            data = await self._async_poll()
        except UpdateFailed:
            if self._cycle_outage:
                self.consecutive_failures += 1
            self._apply_interval()
            raise
        self.consecutive_failures = 0
        self._apply_interval()
        return data

    def _apply_interval(self) -> None:
        """Choose the next poll interval.

        Failures stretch the interval up to ten minutes. After a command the
        cloud is polled at the minimum interval for a few minutes because the
        unit uploads a status packet about every 30 s.
        """
        base = self.base_interval
        if self.consecutive_failures:
            self.update_interval = max(
                base,
                min(base * 2**self.consecutive_failures, MAX_BACKOFF_INTERVAL),
            )
        elif time.monotonic() < self._fast_until:
            self.update_interval = min(base, timedelta(seconds=MIN_SCAN_INTERVAL))
        else:
            self.update_interval = base

    @property
    def cycle_budget(self) -> float:
        """Return the time one status fetch may take, always below the interval."""
        interval = (self.update_interval or self.base_interval).total_seconds()
        return max(10.0, interval - 5.0)

    async def _async_poll(self) -> AmbientikaData:
        """Refresh metadata when due and poll every supported device."""
        started = time.monotonic()
        generations = dict(self._generation)
        self._cycle_causes = []
        self._cycle_offline = set()
        self._cycle_metadata = {}
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
                        raise update_failed(f"{err} ({describe_error(err)})") from err
                    self._discovery_failed(err)
            old_data = self.data or AmbientikaData()
            try:
                async with asyncio.timeout(self.cycle_budget):
                    statuses, failed_devices = await self._async_fetch_statuses(
                        old_data
                    )
                    await self._async_refresh_schedules(statuses)
            except TimeoutError as err:
                self._cycle_causes.append("cycle: timeout")
                self.last_failure_causes = tuple(self._cycle_causes)
                raise update_failed(
                    "The Ambientika update exceeded its time budget"
                ) from err
        except AmbientikaAuthError as err:
            raise auth_failed_error() from err
        except AmbientikaApiError as err:
            self.last_failure_causes = (describe_error(err),)
            raise update_failed(f"{err} ({describe_error(err)})") from err
        finally:
            self.last_update_duration_ms = round((time.monotonic() - started) * 1000)
            self.cycle_durations_ms.append(self.last_update_duration_ms)

        self.last_failure_causes = tuple(self._cycle_causes)
        back_online = self._track_offline(failed_devices)
        if failed_devices and not set(statuses).difference(failed_devices):
            causes = "; ".join(self._cycle_causes) or "no usable status packets"
            # A unit without power is not a cloud outage: keep the normal pace.
            self._cycle_outage = not failed_devices <= self._cycle_offline
            raise update_failed(f"No Ambientika device status could be read ({causes})")

        # Offline devices are logged separately on their own transitions.
        newly_failed = failed_devices - self._failed_devices - self.offline_devices
        recovered = self._failed_devices - failed_devices - back_online
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

        self._apply_metadata(self._cycle_metadata)

        # Results fetched before or right after a command may still carry the
        # old state; the commanded state stays until a later poll confirms it.
        current = self.data or AmbientikaData()
        merged: dict[str, AmbientikaDeviceData] = {}
        for serial, device in self._known_devices.items():
            current_device = current.devices.get(serial)
            superseded = self._poll_superseded(serial, generations, started)
            if superseded:
                self.discarded_polls += 1
            status = (
                (
                    current_device.status
                    if superseded and current_device is not None
                    else statuses.get(serial)
                )
                or (current_device.status if current_device is not None else None)
                if device.role != "NotConfigured"
                else None
            )
            self._remember_writable(serial, status)
            merged[serial] = AmbientikaDeviceData(
                device=device,
                status=status,
                schedule=self._schedules.get(serial),
            )

        self.last_partial_failures = len(failed_devices)
        self.last_successful_update = datetime.now(UTC)
        return AmbientikaData(
            devices=merged,
            feature_flags=dict(self._feature_flags),
            failed_devices=frozenset(failed_devices),
            offline_devices=frozenset(self.offline_devices),
        )

    def _poll_superseded(
        self, serial: str, generations: dict[str, int], started: float
    ) -> bool:
        """Return whether a command made this poll's result for a device stale."""
        if self._generation.get(serial, 0) != generations.get(serial, 0):
            return True
        command_time = self._command_time.get(serial)
        return command_time is not None and started - command_time < STATUS_SETTLE_TIME

    def _track_offline(self, failed_devices: set[str]) -> set[str]:
        """Log offline transitions once and return devices back online."""
        for serial in sorted(self._cycle_offline - self.offline_devices):
            self.offline_transitions += 1
            LOGGER.warning(
                "Ambientika device %s is offline: the cloud has no current status "
                "packet; its entities are unavailable until it reconnects",
                mask_serial(serial),
            )
        back_online = {
            serial
            for serial in self.offline_devices - self._cycle_offline
            if serial not in failed_devices
        }
        for serial in sorted(back_online):
            LOGGER.info("Ambientika device %s is back online", mask_serial(serial))
        self.offline_devices = (
            (self.offline_devices | self._cycle_offline) - back_online
        ) & set(self._known_devices)
        return back_online

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

    # ------------------------------------------------------------------
    # Discovery, metadata and schedules
    # ------------------------------------------------------------------

    async def _async_discover(self) -> None:
        """Discover all current devices and optional server capabilities."""
        houses = await self.api.async_houses_info()
        house_metadata: object = None
        try:
            house_metadata = await self.api.async_houses()
        except AmbientikaAuthError:
            raise
        except AmbientikaApiError:
            LOGGER.debug("Optional Ambientika house metadata is unavailable")
        devices = parse_houses(houses, house_metadata)
        if not devices and self._known_devices:
            # An empty answer for an account that had devices is a cloud
            # problem, not a reason to remove every device.
            raise AmbientikaResponseError("Discovery returned no devices")
        changes = self._diff_devices(self._known_devices, devices)
        self._known_devices = devices
        for serial, device in devices.items():
            self.firmware_history.setdefault(serial, [self._firmware_entry(device)])
        self._publish_device_changes(changes)
        # Retry unsupported endpoints after discovery (normally every six hours),
        # including devices whose firmware or role changed since the last probe.
        self._unsupported_status.clear()
        self._status_errors.clear()
        self._status_retry_at.clear()
        self._last_discovery = datetime.now(UTC)
        self._rediscovery_requested = False
        if self.discovery_failures:
            LOGGER.info("Ambientika device discovery recovered")
        self.discovery_failures = 0
        self.next_discovery_attempt = None
        self.stale_devices.async_discovered(devices)
        feature_flags: dict[str, bool] = {}
        try:
            feature_flags = await self.api.async_feature_flags()
        except AmbientikaAuthError:
            raise
        except AmbientikaApiError:
            LOGGER.debug("Optional Ambientika feature flags are unavailable")
        if feature_flags:
            self._feature_flags = feature_flags
        # Schedules are fetched with the next status update, once it is known
        # which devices report a schedule at all.
        self._schedule_due = {
            serial
            for serial, device in devices.items()
            if device.device_id is not None and is_controllable_device(device)
        }

    async def _async_refresh_schedules(
        self, statuses: dict[str, AmbientikaStatus]
    ) -> None:
        """Fetch weekly schedules only for devices that report one.

        A schedule is requested after discovery and whenever a device starts
        reporting a schedule state; devices that report ``NotAvailable`` have
        none and are not asked.
        """
        if self._feature_flags.get("weeklyScheduler") is False:
            self._schedules = {}
            self._schedule_due.clear()
            return

        candidates: list[tuple[str, int]] = []
        for serial, device in self._known_devices.items():
            status = statuses.get(serial)
            state = status.schedule_state if status is not None else None
            previous = self._schedule_states.get(serial)
            if state is None:
                continue
            self._schedule_states[serial] = state
            if state == "NotAvailable":
                self._schedules.pop(serial, None)
                self._schedule_due.discard(serial)
            elif (
                device.device_id is not None
                and is_controllable_device(device)
                and (
                    serial in self._schedule_due
                    or (
                        previous in (None, "NotAvailable")
                        and serial not in self._schedules
                    )
                )
            ):
                candidates.append((serial, device.device_id))
        if not candidates:
            return

        results = await asyncio.gather(
            *(self.api.async_schedule(device_id) for _, device_id in candidates),
            return_exceptions=True,
        )
        for (serial, _device_id), result in zip(candidates, results, strict=True):
            self._schedule_due.discard(serial)
            if isinstance(result, dict):
                self._schedules[serial] = parse_schedule(result)
            elif isinstance(result, AmbientikaAuthError):
                raise result
            elif isinstance(result, AmbientikaNotFoundError):
                self._schedules.pop(serial, None)

    @staticmethod
    def _firmware_entry(device: AmbientikaDevice) -> dict[str, Any]:
        """Return one firmware history entry (no personal data)."""
        return {
            "at": datetime.now(UTC).isoformat(),
            "micro": device.micro_firmware,
            "radio": device.radio_firmware,
            "radio_at": device.radio_at_firmware,
        }

    @staticmethod
    def _diff_devices(
        old: dict[str, AmbientikaDevice], new: dict[str, AmbientikaDevice]
    ) -> dict[str, dict[str, tuple[Any, Any]]]:
        """Return changed name, role, firmware and zone index of known devices."""
        changes: dict[str, dict[str, tuple[Any, Any]]] = {}
        for serial, device in new.items():
            previous = old.get(serial)
            if previous is None:
                continue
            changed = {
                field: (getattr(previous, field), getattr(device, field))
                for field in DEVICE_METADATA_FIELDS
                if getattr(previous, field) != getattr(device, field)
            }
            if changed:
                changes[serial] = changed
        return changes

    def _apply_metadata(self, metadata: dict[str, AmbientikaDeviceMetadata]) -> None:
        """Merge metadata from the house status tree into the known devices.

        Every poll carries names, roles and firmware versions, so changes show up
        within one poll instead of at the next discovery. Devices that appear in
        the tree but are unknown trigger an early discovery.
        """
        updated = dict(self._known_devices)
        for serial, meta in metadata.items():
            device = updated.get(serial)
            if device is None:
                if serial not in self._unknown_batch_serials:
                    self._unknown_batch_serials.add(serial)
                    self._rediscovery_requested = True
                continue
            values = {
                field: getattr(meta, field)
                for field in DEVICE_METADATA_FIELDS
                if getattr(meta, field) is not None
                and getattr(meta, field) != getattr(device, field)
            }
            if values:
                updated[serial] = replace(device, **values)
        changes = self._diff_devices(self._known_devices, updated)
        if not changes:
            return
        self._known_devices = updated
        if any("role" in fields for fields in changes.values()):
            self._rediscovery_requested = True
        self._publish_device_changes(changes)

    def _publish_device_changes(
        self, changes: dict[str, dict[str, tuple[Any, Any]]]
    ) -> None:
        """Update the device registry, record history and fire change events."""
        if not changes:
            return
        registry = dr.async_get(self.hass)
        for serial, fields in changes.items():
            device = self._known_devices[serial]
            self.device_changes += 1
            if any(field.endswith("firmware") for field in fields):
                history = self.firmware_history.setdefault(serial, [])
                history.append(self._firmware_entry(device))
                del history[:-MAX_FIRMWARE_HISTORY]
            entry = registry.async_get_device(identifiers={(DOMAIN, serial)})
            if entry is not None:
                registry.async_update_device(
                    entry.id, name=device.name, sw_version=firmware_text(device)
                )
            LOGGER.info(
                "Ambientika device %s changed: %s",
                mask_serial(serial),
                ", ".join(sorted(fields)),
            )
            self.hass.bus.async_fire(
                EVENT_DEVICE_CHANGED,
                {
                    "device_id": entry.id if entry is not None else None,
                    "changes": {
                        field: {"old": old, "new": new}
                        for field, (old, new) in fields.items()
                    },
                },
            )

    # ------------------------------------------------------------------
    # Status fetching
    # ------------------------------------------------------------------

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
                self._cycle_metadata.update(parse_house_status_metadata(batch_result))
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

        for serial in statuses:
            self._status_errors.pop(serial, None)
            self._status_retry_at.pop(serial, None)
        self._unsupported_status.difference_update(statuses)
        now = time.monotonic()
        pending: list[str] = []
        backed_off: list[str] = []
        for serial in sorted(
            readable_serials - self._unsupported_status - statuses.keys()
        ):
            if self._status_retry_at.get(serial, 0.0) > now:
                backed_off.append(serial)
            else:
                pending.append(serial)
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

        def keep_last_status(serial: str) -> None:
            old_device_data = old_data.devices.get(serial)
            if old_device_data is not None and old_device_data.status is not None:
                statuses[serial] = old_device_data.status

        for serial in pending:
            device_result = results.get(serial)
            if isinstance(device_result, AmbientikaStatus):
                statuses[serial] = device_result
                self._status_errors.pop(serial, None)
                self._status_retry_at.pop(serial, None)
                continue
            if isinstance(device_result, AmbientikaAuthError):
                raise device_result
            if isinstance(device_result, BaseException):
                self._note_failure(f"device {mask_serial(serial)}", device_result)
            if isinstance(device_result, AmbientikaStatusUnavailableError):
                # Offline, not unsupported: keep polling it every cycle.
                self._cycle_offline.add(serial)
            if isinstance(
                device_result, AmbientikaNotFoundError | AmbientikaForbiddenError
            ):
                self._note_status_rejected(serial)
                if isinstance(device_result, AmbientikaDeviceNotFoundError):
                    self._rediscovery_requested = True
            failed_devices.add(serial)
            keep_last_status(serial)
        for serial in backed_off:
            failed_devices.add(serial)
            keep_last_status(serial)
        if skipped:
            LOGGER.debug(
                "Skipped %d individual Ambientika status request(s) during a "
                "house status outage",
                len(skipped),
            )
        return statuses, failed_devices

    def _note_status_rejected(self, serial: str) -> None:
        """Back off a device whose status request was rejected with 403/404.

        Only repeated rejections mark the endpoint as unsupported until the next
        discovery; a unit that was briefly unreachable recovers on its own.
        """
        streak = self._status_errors.get(serial, 0) + 1
        self._status_errors[serial] = streak
        if streak >= UNSUPPORTED_AFTER_FAILURES:
            self._unsupported_status.add(serial)
            self._status_retry_at.pop(serial, None)
            return
        delay = DEVICE_RETRY_DELAYS[min(streak, len(DEVICE_RETRY_DELAYS)) - 1]
        self._status_retry_at[serial] = time.monotonic() + delay

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

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    def _write_lock(self, serial: str) -> asyncio.Lock:
        """Return the lock that serializes the requests of one device."""
        return self._write_locks.setdefault(serial, asyncio.Lock())

    def _mark_command(self, serial: str) -> int:
        """Record a command: invalidate older polls and poll faster for a while."""
        generation = self._generation.get(serial, 0) + 1
        self._generation[serial] = generation
        self._command_time[serial] = time.monotonic()
        self._fast_until = time.monotonic() + FAST_POLL_DURATION.total_seconds()
        self._apply_interval()
        return generation

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
        """Write a validated state, merging commands that arrive together.

        Commands for the same device within a short window are combined into
        one request, based on the last known state plus unconfirmed commands.
        The call returns once the cloud confirmed the result or after the
        read-back attempts.
        """
        requested = {
            field: value
            for field, value in (
                ("operating_mode", operating_mode),
                ("fan_speed", fan_speed),
                ("humidity_level", humidity_level),
                ("light_sensor_level", light_sensor_level),
                ("schedule_mode", schedule_mode),
            )
            if value is not None
        }
        pending = self._pending_writes.get(serial)
        if pending is not None:
            merged = {**pending.fields, **requested}
            try:
                self._plan_write(serial, merged)
            except ServiceValidationError:
                # Not valid together (for example a speed and then "off"):
                # the collected commands go out first, this one follows.
                del self._pending_writes[serial]
                pending = None
            else:
                pending.fields = merged
        if pending is None:
            self._plan_write(serial, requested)
            pending = _PendingWrite(
                fields=requested, future=self.hass.loop.create_future()
            )
            self._pending_writes[serial] = pending
            pending.future.add_done_callback(_consume_exception)
            self.config_entry.async_create_background_task(
                self.hass,
                self._async_flush_write(serial, pending),
                f"{DOMAIN}_write_{mask_serial(serial)}",
            )
        await asyncio.shield(pending.future)

    async def _async_flush_write(self, serial: str, pending: _PendingWrite) -> None:
        """Send the collected commands and report the outcome to every caller."""
        try:
            await asyncio.sleep(WRITE_BATCH_DELAY)
            if self._pending_writes.get(serial) is pending:
                del self._pending_writes[serial]
            await self._async_execute_write(serial, pending.fields)
        except asyncio.CancelledError:
            pending.future.cancel()
            raise
        except Exception as err:
            pending.future.set_exception(err)
        else:
            pending.future.set_result(None)

    def _plan_write(self, serial: str, fields: dict[str, Any]) -> _WritePlan:
        """Validate commands against the current state and build the full state."""
        device_data = self.data.devices.get(serial) if self.data else None
        status = device_data.status if device_data else None
        if device_data is None or status is None:
            raise validation_error("device_unavailable")

        if not is_controllable_device(device_data.device, status):
            raise validation_error("device_not_controllable")

        operating_mode: str | None = fields.get("operating_mode")
        fan_speed: str | None = fields.get("fan_speed")
        humidity_level: str | None = fields.get("humidity_level")
        light_sensor_level: str | None = fields.get("light_sensor_level")
        schedule_mode: bool | None = fields.get("schedule_mode")
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

        expected: dict[str, str] = {
            field: value
            for field, value in requested_settings.items()
            if value is not None
        }
        expected["operating_mode"] = str(target_mode)
        if schedule_mode is not None:
            expected["schedule_state"] = "On" if schedule_mode else "Off"
        return _WritePlan(
            values={key: str(value) for key, value in values.items()},
            expected=expected,
            schedule_mode=(
                schedule_mode
                if schedule_mode is not None
                else status.schedule_state == "On"
            ),
            status=status,
        )

    async def _async_execute_write(self, serial: str, fields: dict[str, Any]) -> None:
        """Send one validated command and confirm it with a delayed read-back."""
        async with self._write_lock(serial):
            plan = self._plan_write(serial, fields)
            try:
                await self.api.async_change_mode(
                    serial,
                    **plan.values,
                    schedule_mode=plan.schedule_mode,
                )
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

            # Accepted is not delivered: the cloud also accepts commands for
            # units that are offline. Show the written values at once; the
            # state read back below, or the next poll, corrects them.
            generation = self._mark_command(serial)
            self._apply_status(serial, _with_expected(plan.status, plan.expected))

        def matches(candidate: AmbientikaStatus) -> bool:
            return all(
                getattr(candidate, field) == value
                for field, value in plan.expected.items()
            )

        latest, matched = await self._async_read_back(serial, matches)
        if self._generation.get(serial, 0) != generation:
            LOGGER.debug(
                "Ambientika read-back for %s dropped: a newer command was sent",
                mask_serial(serial),
            )
            return
        if matched and latest is not None:
            refreshed = latest
        else:
            LOGGER.debug(
                "Ambientika command for %s is not reflected yet; showing the "
                "written values until the next poll",
                mask_serial(serial),
            )
            refreshed = _with_expected(latest or plan.status, plan.expected)
        self._remember_writable(serial, refreshed)
        self._apply_status(serial, refreshed)

    async def _async_read_back(
        self, serial: str, matches: Callable[[AmbientikaStatus], bool]
    ) -> tuple[AmbientikaStatus | None, bool]:
        """Read a device back after a command, retrying once if not reflected."""
        latest: AmbientikaStatus | None = None
        for _attempt in range(READBACK_ATTEMPTS):
            await asyncio.sleep(READBACK_DELAY)
            try:
                latest = await self._async_fetch_one(serial)
            except AmbientikaAuthError:
                self.config_entry.async_start_reauth(self.hass)
                break
            except AmbientikaApiError as err:
                LOGGER.debug(
                    "Ambientika read-back for %s failed: %s",
                    mask_serial(serial),
                    describe_error(err),
                )
                continue
            if matches(latest):
                return latest, True
        return latest, False

    def _apply_status(self, serial: str, status: AmbientikaStatus) -> None:
        """Publish a status read back or written by an action."""
        if self.data is None or serial not in self.data.devices:
            return
        devices = dict(self.data.devices)
        devices[serial] = replace(devices[serial], status=status)
        self.async_set_updated_data(
            replace(
                self.data,
                devices=devices,
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
        async with self._write_lock(serial):
            try:
                await self.api.async_reset_filter(serial)
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
            generation = self._mark_command(serial)

        latest, matched = await self._async_read_back(
            serial, lambda candidate: candidate.filter_status not in (None, "Bad")
        )
        if self._generation.get(serial, 0) != generation:
            return
        base = latest or device_data.status
        if matched and latest is not None:
            self._apply_status(serial, latest)
        elif base is not None:
            # The official app also shows the filter as good right after a reset.
            self._apply_status(serial, replace(base, filter_status="Good"))


def _with_expected(
    base: AmbientikaStatus, expected: dict[str, str]
) -> AmbientikaStatus:
    """Return a status with the written values applied."""
    return replace(
        base,
        operating_mode=expected["operating_mode"],
        fan_speed=expected.get("fan_speed", base.fan_speed),
        humidity_level=expected.get("humidity_level", base.humidity_level),
        light_sensor_level=expected.get("light_sensor_level", base.light_sensor_level),
        schedule_state=expected.get("schedule_state", base.schedule_state),
    )


def _consume_exception(future: asyncio.Future[None]) -> None:
    """Mark a command outcome as retrieved when every caller went away."""
    if not future.cancelled():
        future.exception()
