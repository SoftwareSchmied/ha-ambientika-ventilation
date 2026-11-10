"""Tests for the removal of devices that left the Ambientika account."""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from custom_components.ambientika_ventilation import async_remove_config_entry_device
from custom_components.ambientika_ventilation.api import AmbientikaResponseError
from custom_components.ambientika_ventilation.const import (
    CLOUD_DEVICE_PREFIX,
    DOMAIN,
    STALE_DEVICE_DISCOVERIES,
    STALE_DEVICE_MIN_AGE,
)
from custom_components.ambientika_ventilation.coordinator import AmbientikaCoordinator
from custom_components.ambientika_ventilation.stale import (
    StaleDeviceTracker,
    device_serials,
    is_removable,
)
from homeassistant.helpers import device_registry as dr
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

KEPT = "AABBCCDDEEFF"
GONE = "112233445566"


def _find(hass, entry, serial):
    return dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, serial), entry.entry_id
    )


def _add_devices(hass, entry, *serials):
    registry = dr.async_get(hass)
    return {
        serial: registry.async_get_or_create(
            config_entry_id=entry.entry_id, identifiers={(DOMAIN, serial)}
        )
        for serial in serials
    }


def test_removable_helpers() -> None:
    """Only devices without any reported serial are removable."""
    device = SimpleNamespace(identifiers={(DOMAIN, GONE), ("other", KEPT)})
    assert device_serials(device) == {GONE}
    assert is_removable([KEPT], device) is True
    assert is_removable([GONE], device) is False


async def test_manual_removal_only_for_unreported_devices(hass) -> None:
    """The user may delete only devices the cloud no longer reports."""
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    devices = _add_devices(hass, entry, KEPT, GONE)
    entry.runtime_data = type(
        "Runtime", (), {"coordinator": AsyncMock(data=AsyncMock(devices={KEPT: 1}))}
    )()

    assert await async_remove_config_entry_device(hass, entry, devices[KEPT]) is False
    assert await async_remove_config_entry_device(hass, entry, devices[GONE]) is True


async def test_device_is_removed_after_enough_discoveries_and_time(hass) -> None:
    """Both the discovery count and the minimum age must be reached."""
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    _add_devices(hass, entry, KEPT, GONE)
    tracker = StaleDeviceTracker(hass, entry)

    for _ in range(STALE_DEVICE_DISCOVERIES + 2):
        assert tracker.async_discovered({KEPT}) == []
    assert tracker.missing[GONE] == STALE_DEVICE_DISCOVERIES + 2
    assert _find(hass, entry, GONE) is not None

    future = dt_util.utcnow() + STALE_DEVICE_MIN_AGE + timedelta(minutes=1)
    with patch(
        "custom_components.ambientika_ventilation.stale.dt_util.utcnow",
        return_value=future,
    ):
        assert tracker.async_discovered({KEPT}) == [GONE]
    assert _find(hass, entry, GONE) is None
    assert _find(hass, entry, KEPT) is not None
    assert tracker.missing == {}


async def test_reappearing_device_resets_the_counter(hass) -> None:
    """A device that is reported again starts over."""
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    _add_devices(hass, entry, KEPT, GONE)
    tracker = StaleDeviceTracker(hass, entry, required=2, min_age=timedelta(0))

    tracker.async_discovered({KEPT})
    assert tracker.missing == {GONE: 1}
    tracker.async_discovered({KEPT, GONE})
    assert tracker.missing == {}
    assert tracker.async_discovered({KEPT}) == []
    assert tracker.async_discovered({KEPT}) == [GONE]


async def test_empty_discovery_never_removes(hass) -> None:
    """An empty answer is ignored entirely."""
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    _add_devices(hass, entry, GONE)
    tracker = StaleDeviceTracker(hass, entry, required=1, min_age=timedelta(0))

    assert tracker.async_discovered(set()) == []
    assert tracker.missing == {}
    assert _find(hass, entry, GONE)


async def test_devices_of_other_integrations_are_ignored(hass) -> None:
    """Registry devices without an Ambientika identifier are left alone."""
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, connections={("mac", "00:00:00:00:00:01")}
    )
    tracker = StaleDeviceTracker(hass, entry, required=1, min_age=timedelta(0))

    assert tracker.async_discovered({KEPT}) == []
    assert tracker.missing == {}


async def test_coordinator_only_counts_successful_discoveries(
    hass, houses_payload, status_payload
) -> None:
    """Failed or empty discoveries do not advance the stale-device counters."""
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    _add_devices(hass, entry, KEPT, "998877665544")
    api = AsyncMock()
    api.async_houses_info.return_value = houses_payload[:1]
    api.async_feature_flags.return_value = {}
    api.async_device_status.return_value = status_payload
    coordinator = AmbientikaCoordinator(hass, entry, api)
    # Keep both serials reported on the first run so only later runs count.
    await coordinator._async_discover()
    assert coordinator.stale_devices.missing == {"998877665544": 1}

    api.async_houses_info.return_value = []
    with pytest.raises(AmbientikaResponseError):
        await coordinator._async_discover()
    api.async_houses_info.side_effect = AmbientikaResponseError("boom")
    with pytest.raises(AmbientikaResponseError):
        await coordinator._async_discover()
    assert coordinator.stale_devices.missing == {"998877665544": 1}


async def test_cloud_service_device_is_never_removed(hass) -> None:
    """The account-level cloud device has no serial and must survive discoveries."""
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    cloud_id = f"{CLOUD_DEVICE_PREFIX}{entry.entry_id}"
    devices = _add_devices(hass, entry, KEPT, cloud_id)
    assert device_serials(devices[cloud_id]) == set()
    assert is_removable([KEPT], devices[cloud_id]) is False

    entry.runtime_data = type(
        "Runtime", (), {"coordinator": AsyncMock(data=AsyncMock(devices={KEPT: 1}))}
    )()
    removable = await async_remove_config_entry_device(hass, entry, devices[cloud_id])
    assert removable is False

    tracker = StaleDeviceTracker(hass, entry)
    start = dt_util.utcnow()
    for step in range(STALE_DEVICE_DISCOVERIES + 2):
        with patch(
            "custom_components.ambientika_ventilation.stale.dt_util.utcnow",
            return_value=start + STALE_DEVICE_MIN_AGE * (step + 1),
        ):
            assert tracker.async_discovered([KEPT]) == []
    assert _find(hass, entry, cloud_id) is not None
