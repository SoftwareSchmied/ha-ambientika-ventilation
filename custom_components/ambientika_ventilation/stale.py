"""Removal of devices that disappeared from the Ambientika account."""

from __future__ import annotations

import logging
from collections.abc import Collection
from datetime import datetime, timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.util import dt as dt_util

from .const import (
    CLOUD_DEVICE_PREFIX,
    DOMAIN,
    HOUSE_DEVICE_PREFIX,
    STALE_DEVICE_DISCOVERIES,
    STALE_DEVICE_MIN_AGE,
)

LOGGER = logging.getLogger(__name__)


def device_serials(device_entry: dr.DeviceEntry) -> set[str]:
    """Return the Ambientika serial numbers of a device registry entry.

    The account-level cloud service device and the house devices have no serial
    number and are never treated as units.
    """
    return {
        identifier
        for domain, identifier in device_entry.identifiers
        if domain == DOMAIN
        and not identifier.startswith((CLOUD_DEVICE_PREFIX, HOUSE_DEVICE_PREFIX))
    }


def is_removable(
    reported_serials: Collection[str], device_entry: dr.DeviceEntry
) -> bool:
    """Return whether the cloud no longer reports any serial of the device.

    Devices without a unit serial (the cloud service device) are not removable.
    """
    serials = device_serials(device_entry)
    return bool(serials) and not serials & set(reported_serials)


class StaleDeviceTracker:
    """Remove registry devices that stay missing from successful discoveries.

    A device is removed only after it was absent from ``STALE_DEVICE_DISCOVERIES``
    consecutive successful, non-empty discoveries and for at least
    ``STALE_DEVICE_MIN_AGE``. Failed or empty discoveries never reach the
    tracker, and a device that reappears starts over. The counters live in
    memory only, so a restart postpones removal instead of risking it.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        *,
        required: int = STALE_DEVICE_DISCOVERIES,
        min_age: timedelta = STALE_DEVICE_MIN_AGE,
    ) -> None:
        """Initialize the tracker of one config entry."""
        self._hass = hass
        self._entry = entry
        self._required = required
        self._min_age = min_age
        self._missing: dict[str, tuple[int, datetime]] = {}

    @property
    def missing(self) -> dict[str, int]:
        """Return the number of consecutive discoveries each device missed."""
        return {serial: count for serial, (count, _) in self._missing.items()}

    def async_discovered(self, reported_serials: Collection[str]) -> list[str]:
        """Record one successful discovery and remove devices that stay missing.

        Returns the serial numbers whose registry devices were removed.
        """
        if not reported_serials:
            return []
        registry = dr.async_get(self._hass)
        now = dt_util.utcnow()
        present: set[str] = set()
        removed: list[str] = []
        for device_entry in dr.async_entries_for_config_entry(
            registry, self._entry.entry_id
        ):
            serials = device_serials(device_entry)
            if not serials:
                continue
            if not is_removable(reported_serials, device_entry):
                continue
            key = sorted(serials)[0]
            present.add(key)
            count, first_missed = self._missing.get(key, (0, now))
            count += 1
            self._missing[key] = (count, first_missed)
            if count < self._required or now - first_missed < self._min_age:
                continue
            LOGGER.info(
                "Removing an Ambientika device that was absent from the last "
                "%d discoveries",
                count,
            )
            registry.async_update_device(
                device_entry.id, remove_config_entry_id=self._entry.entry_id
            )
            self._missing.pop(key, None)
            removed.append(key)
        for key in set(self._missing) - present:
            del self._missing[key]
        return removed
