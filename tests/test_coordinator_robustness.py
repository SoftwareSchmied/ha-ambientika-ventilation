"""Tests for polling pace, command merging, device metadata and schedules."""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest
from custom_components.ambientika_ventilation import coordinator as coordinator_module
from custom_components.ambientika_ventilation.api import (
    AmbientikaApiError,
    AmbientikaAuthError,
    AmbientikaForbiddenError,
    AmbientikaNoScheduleError,
    AmbientikaNotFoundError,
    AmbientikaServerError,
    AmbientikaStatusUnavailableError,
)
from custom_components.ambientika_ventilation.const import (
    DISCOVERY_INTERVAL,
    DOMAIN,
    EVENT_DEVICE_CHANGED,
)
from custom_components.ambientika_ventilation.coordinator import (
    DEVICE_RETRY_DELAYS,
    UNSUPPORTED_AFTER_FAILURES,
    AmbientikaCoordinator,
)
from custom_components.ambientika_ventilation.models import AmbientikaStatus
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    HomeAssistantError,
    ServiceValidationError,
)
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
)

from .test_offline_readback import OFF_STATUS, packet
from .test_resilience import GEMINI, MASTER, polling_coordinator, write_coordinator

MANUAL_STATUS = AmbientikaStatus(
    serial_number=MASTER,
    operating_mode="ManualHeatRecovery",
    fan_speed="Low",
    humidity_level="Normal",
    light_sensor_level="Low",
    schedule_state="Off",
)


def tree_device(serial: str, **values: Any) -> dict[str, Any]:
    """Return one device of the zone tree carried by a house status response."""
    return {"serialNumber": serial, **values}


def batch(
    status_payload: dict[str, Any], *devices: dict[str, Any], **status: Any
) -> dict[str, Any]:
    """Return a house status response with the given zone tree devices."""
    return {
        "zoneDevicesInfo": [
            {
                "statusPacket": {**status_payload, **status},
                "zone": {"rooms": [{"devices": list(devices)}]},
            }
        ]
    }


# --- polling pace -----------------------------------------------------------


async def test_unchanged_poll_does_not_notify_entities(
    hass, houses_payload, status_payload
) -> None:
    """Only polls that bring new data wake the entities."""
    coordinator, api = polling_coordinator(hass, houses_payload)
    api.async_house_devices_status.return_value = batch(status_payload)

    with patch.object(
        coordinator, "async_update_listeners", wraps=coordinator.async_update_listeners
    ) as notify:
        await coordinator.async_refresh()
        await coordinator.async_refresh()
        assert notify.call_count == 1

        api.async_house_devices_status.return_value = batch(
            status_payload, temperature=19
        )
        await coordinator.async_refresh()
        assert notify.call_count == 2


async def test_topology_listeners_run_only_when_capabilities_change(
    hass, houses_payload, status_payload
) -> None:
    """Platforms look for new entities only after devices or capabilities change."""
    coordinator, api = polling_coordinator(hass, houses_payload)
    api.async_house_devices_status.return_value = batch(status_payload)
    changed = MagicMock()
    remove = coordinator.async_add_topology_listener(changed)

    await coordinator.async_refresh()
    assert changed.call_count == 1

    api.async_house_devices_status.return_value = batch(status_payload, temperature=18)
    await coordinator.async_refresh()
    assert changed.call_count == 1

    api.async_house_devices_status.return_value = batch(
        status_payload, lightSensorLevel="NotAvailable"
    )
    await coordinator.async_refresh()
    assert changed.call_count == 2

    remove()
    api.async_house_devices_status.return_value = batch(status_payload)
    await coordinator.async_refresh()
    assert changed.call_count == 2


async def test_failures_stretch_the_interval_and_success_resets_it(
    hass, houses_payload, status_payload
) -> None:
    """Consecutive failed polls slow polling down to ten minutes."""
    coordinator, api = polling_coordinator(hass, houses_payload)
    api.async_house_devices_status.side_effect = AmbientikaServerError(status=503)
    api.async_device_status.side_effect = AmbientikaServerError(status=503)

    intervals = []
    for _ in range(5):
        with pytest.raises(UpdateFailed):
            await coordinator._async_update_data()
        intervals.append(coordinator.update_interval.total_seconds())
    assert intervals == [120, 240, 480, 600, 600]
    assert coordinator.consecutive_failures == 5

    api.async_house_devices_status.side_effect = None
    api.async_house_devices_status.return_value = batch(status_payload)
    await coordinator._async_update_data()
    assert coordinator.update_interval == timedelta(seconds=60)
    assert coordinator.consecutive_failures == 0


async def test_offline_unit_does_not_slow_down_polling(hass, houses_payload) -> None:
    """A unit without power is not a cloud outage and keeps the normal pace."""
    coordinator, api = polling_coordinator(hass, houses_payload)
    api.async_device_status.side_effect = AmbientikaStatusUnavailableError

    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()

    assert coordinator.consecutive_failures == 0
    assert coordinator.update_interval == timedelta(seconds=60)


@pytest.mark.parametrize(
    ("configured", "expected"),
    [(None, 60), (90, 90), (5, 30), ("fast", 60), (900, 900)],
)
async def test_configured_interval_is_used_with_a_floor(
    hass, configured, expected
) -> None:
    """The option sets the base interval, never below 30 seconds."""
    options = {} if configured is None else {"scan_interval": configured}
    entry = MockConfigEntry(domain=DOMAIN, data={}, options=options)
    entry.add_to_hass(hass)

    coordinator = AmbientikaCoordinator(hass, entry, AsyncMock())

    assert coordinator.update_interval == timedelta(seconds=expected)
    assert coordinator.base_interval == timedelta(seconds=expected)


async def test_commands_trigger_fast_polling_for_a_while(hass) -> None:
    """After a command the cloud is polled at the minimum interval."""
    coordinator, api = write_coordinator(hass, OFF_STATUS)
    api.async_device_status.return_value = packet(operatingMode="Auto")

    await coordinator.async_write_state(MASTER, operating_mode="Auto")
    assert coordinator.update_interval == timedelta(seconds=30)

    coordinator._fast_until = 0.0
    coordinator._apply_interval()
    assert coordinator.update_interval == timedelta(seconds=60)


async def test_cycle_budget_stays_below_the_interval(hass) -> None:
    """The time allowed for one status fetch depends on the poll interval."""
    entry = MockConfigEntry(domain=DOMAIN, data={}, options={"scan_interval": 30})
    entry.add_to_hass(hass)
    coordinator = AmbientikaCoordinator(hass, entry, AsyncMock())
    assert coordinator.cycle_budget == 25
    coordinator.update_interval = timedelta(seconds=600)
    assert coordinator.cycle_budget == 595
    coordinator.update_interval = None
    assert coordinator.cycle_budget == 25


async def test_hanging_status_requests_hit_the_cycle_budget(
    hass, houses_payload
) -> None:
    """A poll that does not finish in time fails instead of overrunning."""
    coordinator, api = polling_coordinator(hass, houses_payload)

    async def hang(house_id: int) -> None:
        await asyncio.sleep(5)

    api.async_house_devices_status.side_effect = hang
    with (
        patch.object(
            AmbientikaCoordinator, "cycle_budget", new_callable=PropertyMock
        ) as budget,
        pytest.raises(UpdateFailed, match="time budget"),
    ):
        budget.return_value = 0.01
        await coordinator._async_update_data()

    assert coordinator.last_failure_causes == ("cycle: timeout",)
    assert coordinator.consecutive_failures == 1
    assert coordinator.cycle_durations_ms[-1] < 1000


# --- polls versus commands --------------------------------------------------


async def test_poll_started_before_a_command_does_not_overwrite_it(
    hass, houses_payload, status_payload
) -> None:
    """A slow poll cannot bring back the state from before a command."""
    coordinator, api = polling_coordinator(hass, houses_payload)
    api.async_house_devices_status.return_value = batch(status_payload)
    coordinator.data = await coordinator._async_update_data()
    api.async_device_status.return_value = {**status_payload, "fanSpeed": "High"}

    release = asyncio.Event()

    async def slow_batch(house_id: int) -> dict[str, Any]:
        await release.wait()
        return batch(status_payload)

    api.async_house_devices_status.side_effect = slow_batch
    poll = asyncio.create_task(coordinator._async_update_data())
    await asyncio.sleep(0)
    await coordinator.async_write_state(MASTER, fan_speed="High")
    release.set()
    data = await poll

    assert data.devices[MASTER].status.fan_speed == "High"
    assert coordinator.discarded_polls == 1


async def test_poll_right_after_a_command_waits_for_the_cloud(
    hass, houses_payload, status_payload, monkeypatch
) -> None:
    """The cloud needs up to 3 s to reflect a command; early polls are ignored."""
    monkeypatch.setattr(coordinator_module, "STATUS_SETTLE_TIME", 3.0)
    coordinator, api = polling_coordinator(hass, houses_payload)
    api.async_house_devices_status.return_value = batch(status_payload)
    coordinator.data = await coordinator._async_update_data()
    api.async_device_status.return_value = {**status_payload, "fanSpeed": "High"}
    await coordinator.async_write_state(MASTER, fan_speed="High")

    early = await coordinator._async_update_data()
    assert early.devices[MASTER].status.fan_speed == "High"

    coordinator._command_time[MASTER] -= 10
    later = await coordinator._async_update_data()
    assert later.devices[MASTER].status.fan_speed == "Medium"


# --- merged commands --------------------------------------------------------


async def test_simultaneous_commands_become_one_request(hass) -> None:
    """A scene setting mode and humidity sends one combined command."""
    coordinator, api = write_coordinator(hass, OFF_STATUS)
    api.async_device_status.return_value = packet(
        operatingMode="Auto", humidityLevel="Moist"
    )

    await asyncio.gather(
        coordinator.async_write_state(MASTER, operating_mode="Auto"),
        coordinator.async_write_state(MASTER, humidity_level="Moist"),
    )

    api.async_change_mode.assert_awaited_once_with(
        MASTER,
        operating_mode="Auto",
        fan_speed="Low",
        humidity_level="Moist",
        light_sensor_level="Low",
        schedule_mode=False,
    )
    assert coordinator.data.devices[MASTER].status.humidity_level == "Moist"
    assert not coordinator._pending_writes


async def test_conflicting_commands_are_sent_in_order(hass) -> None:
    """Commands that are invalid together are not merged but kept in order."""
    coordinator, api = write_coordinator(hass, MANUAL_STATUS)
    api.async_device_status.return_value = packet(
        operatingMode="Off", fanSpeed="High", humidityLevel="Normal"
    )

    await asyncio.gather(
        coordinator.async_write_state(MASTER, fan_speed="High"),
        coordinator.async_write_state(MASTER, operating_mode="Off"),
    )

    modes = [
        (call.kwargs["operating_mode"], call.kwargs["fan_speed"])
        for call in api.async_change_mode.await_args_list
    ]
    assert modes == [("ManualHeatRecovery", "High"), ("Off", "High")]


async def test_invalid_command_fails_without_blocking_the_pending_ones(hass) -> None:
    """A rejected command raises to its caller only."""
    coordinator, api = write_coordinator(hass, MANUAL_STATUS)
    api.async_device_status.return_value = packet(
        operatingMode="ManualHeatRecovery", fanSpeed="High"
    )

    good = asyncio.create_task(coordinator.async_write_state(MASTER, fan_speed="High"))
    await asyncio.sleep(0)
    with pytest.raises(ServiceValidationError) as error:
        await coordinator.async_write_state(MASTER, humidity_level="Moist")
    await good

    assert error.value.translation_key == "setting_unavailable"
    api.async_change_mode.assert_awaited_once()


async def test_cloud_rejection_reaches_every_merged_caller(hass) -> None:
    """All callers of one merged request see the same failure."""
    coordinator, api = write_coordinator(hass, OFF_STATUS)
    api.async_change_mode.side_effect = AmbientikaServerError(status=500)

    results = await asyncio.gather(
        coordinator.async_write_state(MASTER, operating_mode="Auto"),
        coordinator.async_write_state(MASTER, humidity_level="Dry"),
        return_exceptions=True,
    )

    assert all(isinstance(result, HomeAssistantError) for result in results)
    assert {result.translation_key for result in results} == {"cloud_command_failed"}
    api.async_change_mode.assert_awaited_once()


async def test_command_is_sent_even_if_the_caller_goes_away(hass) -> None:
    """Cancelling the service call does not drop a command that is queued."""
    coordinator, api = write_coordinator(hass, OFF_STATUS)
    api.async_device_status.return_value = packet(operatingMode="Auto")

    caller = asyncio.create_task(
        coordinator.async_write_state(MASTER, operating_mode="Auto")
    )
    await asyncio.sleep(0)
    caller.cancel()
    await asyncio.sleep(0.05)

    api.async_change_mode.assert_awaited_once()

    api.async_change_mode.side_effect = AmbientikaServerError(status=500)
    caller = asyncio.create_task(
        coordinator.async_write_state(MASTER, operating_mode="Off")
    )
    await asyncio.sleep(0)
    caller.cancel()
    await asyncio.sleep(0.05)
    assert api.async_change_mode.await_count == 2


async def test_cancelled_command_task_cancels_its_callers(hass) -> None:
    """A command task cancelled during the batching window is not reported done."""
    coordinator, api = write_coordinator(hass, OFF_STATUS)

    with (
        patch(
            "custom_components.ambientika_ventilation.coordinator.asyncio.sleep",
            side_effect=asyncio.CancelledError,
        ),
        pytest.raises(asyncio.CancelledError),
    ):
        await coordinator.async_write_state(MASTER, operating_mode="Auto")

    api.async_change_mode.assert_not_awaited()


async def test_read_back_of_an_older_command_is_dropped(hass) -> None:
    """A newer command wins over the read-back of the one before it."""
    coordinator, api = write_coordinator(hass, OFF_STATUS)
    api.async_device_status.return_value = packet(operatingMode="Off")
    original = coordinator._async_read_back
    calls = 0

    async def read_back(serial: str, matches: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls == 1:
            await coordinator.async_write_state(MASTER, operating_mode="Off")
            return AmbientikaStatus(serial_number=MASTER, operating_mode="Auto"), True
        return await original(serial, matches)

    coordinator._async_read_back = read_back
    await coordinator.async_write_state(MASTER, operating_mode="Auto")

    assert api.async_change_mode.await_count == 2
    assert coordinator.data.devices[MASTER].status.operating_mode == "Off"


async def test_filter_reset_read_back_is_dropped_after_a_newer_command(hass) -> None:
    """The filter reset does not overwrite what a later command wrote."""
    coordinator, api = write_coordinator(
        hass,
        AmbientikaStatus(
            serial_number=MASTER,
            operating_mode="Auto",
            filter_status="Bad",
            fan_speed="Low",
            humidity_level="Normal",
            light_sensor_level="Low",
        ),
    )

    async def read_back(serial: str, matches: Any) -> Any:
        coordinator._mark_command(serial)
        return None, False

    coordinator._async_read_back = read_back
    await coordinator.async_reset_filter(MASTER)

    api.async_reset_filter.assert_awaited_once()
    assert coordinator.data.devices[MASTER].status.filter_status == "Bad"


# --- rejected status requests ----------------------------------------------


@pytest.mark.parametrize("error", [AmbientikaNotFoundError, AmbientikaForbiddenError])
async def test_rejected_status_is_retried_before_it_is_unsupported(
    hass, houses_payload, status_payload, error
) -> None:
    """403/404 on a device only disables it after repeated rejections."""
    coordinator, api = polling_coordinator(hass, houses_payload)
    api.async_house_devices_status.return_value = batch(status_payload)
    api.async_device_status.side_effect = error

    for streak in range(1, UNSUPPORTED_AFTER_FAILURES):
        await coordinator._async_update_data()
        assert GEMINI not in coordinator._unsupported_status
        assert coordinator._status_errors[GEMINI] == streak
        wait = coordinator._status_retry_at[GEMINI] - time.monotonic()
        assert wait <= DEVICE_RETRY_DELAYS[streak - 1] + 1
        # Backed off: the next cycle does not ask again.
        calls = api.async_device_status.await_count
        data = await coordinator._async_update_data()
        assert api.async_device_status.await_count == calls
        assert GEMINI in data.failed_devices
        coordinator._status_retry_at[GEMINI] = 0.0

    await coordinator._async_update_data()
    assert GEMINI in coordinator._unsupported_status
    assert GEMINI not in coordinator._status_retry_at


async def test_successful_status_resets_the_rejection_streak(
    hass, houses_payload, status_payload
) -> None:
    """A device that answers again starts over."""
    coordinator, api = polling_coordinator(hass, houses_payload)
    api.async_house_devices_status.return_value = batch(status_payload)
    api.async_device_status.side_effect = AmbientikaNotFoundError
    await coordinator._async_update_data()
    assert coordinator._status_errors[GEMINI] == 1
    coordinator._status_retry_at[GEMINI] = 0.0

    api.async_device_status.side_effect = None
    api.async_device_status.return_value = {
        **status_payload,
        "deviceSerialNumber": GEMINI,
    }
    data = await coordinator._async_update_data()

    assert not data.failed_devices
    assert GEMINI not in coordinator._status_errors
    assert GEMINI not in coordinator._status_retry_at


# --- discovery --------------------------------------------------------------


async def test_empty_discovery_keeps_the_known_devices(
    hass, houses_payload, status_payload, caplog
) -> None:
    """An empty answer is a cloud problem, not an account without devices."""
    coordinator, api = polling_coordinator(hass, houses_payload)
    api.async_house_devices_status.return_value = batch(status_payload)
    api.async_houses_info.return_value = []
    coordinator._last_discovery = datetime.now(UTC) - DISCOVERY_INTERVAL

    data = await coordinator._async_update_data()

    assert set(data.devices) == {MASTER, GEMINI}
    assert coordinator.discovery_failures == 1
    assert coordinator.last_failure_causes[0] == "discovery: parse"
    assert coordinator.next_discovery_attempt is not None


async def test_auth_errors_in_optional_discovery_calls_require_reauth(
    hass, houses_payload
) -> None:
    """Optional resources no longer swallow rejected credentials."""
    for failing in ("async_houses", "async_feature_flags"):
        coordinator, api = polling_coordinator(hass, houses_payload)
        coordinator._last_discovery = None
        getattr(api, failing).side_effect = AmbientikaAuthError

        with pytest.raises(ConfigEntryAuthFailed):
            await coordinator._async_update_data()

    coordinator, api = polling_coordinator(hass, houses_payload)
    coordinator._last_discovery = None
    api.async_houses.side_effect = AmbientikaApiError
    api.async_feature_flags.side_effect = AmbientikaApiError
    await coordinator._async_discover()
    assert set(coordinator._known_devices) == {MASTER, GEMINI}


# --- metadata from the status tree -----------------------------------------


async def test_status_tree_updates_names_firmware_and_fires_events(
    hass, houses_payload, status_payload
) -> None:
    """Renames and firmware updates are noticed with every poll."""
    coordinator, api = polling_coordinator(hass, houses_payload)
    registry = dr.async_get(hass)
    device = registry.async_get_or_create(
        config_entry_id=coordinator.config_entry.entry_id,
        identifiers={(DOMAIN, MASTER)},
        name="Living room",
        sw_version="1.2.3 / 2.3.4 / 3.4.5",
    )
    events = async_capture_events(hass, EVENT_DEVICE_CHANGED)
    api.async_house_devices_status.return_value = batch(
        status_payload,
        tree_device(MASTER, name="Lounge", microFwVersion="1.2.4"),
    )

    await coordinator._async_update_data()
    await hass.async_block_till_done()

    updated = coordinator._known_devices[MASTER]
    assert (updated.name, updated.micro_firmware) == ("Lounge", "1.2.4")
    assert updated.radio_firmware == "2.3.4"
    assert registry.async_get(device.id).name == "Lounge"
    assert registry.async_get(device.id).sw_version == "1.2.4 / 2.3.4 / 3.4.5"
    assert len(events) == 1
    assert events[0].data["device_id"] == device.id
    assert events[0].data["changes"] == {
        "name": {"old": "Living room", "new": "Lounge"},
        "micro_firmware": {"old": "1.2.3", "new": "1.2.4"},
    }
    assert coordinator.device_changes == 1
    assert coordinator.firmware_history[MASTER][-1]["micro"] == "1.2.4"

    await coordinator._async_update_data()
    await hass.async_block_till_done()
    assert len(events) == 1


async def test_status_tree_without_values_changes_nothing(
    hass, houses_payload, status_payload
) -> None:
    """Missing or blank tree fields never wipe known metadata."""
    coordinator, api = polling_coordinator(hass, houses_payload)
    api.async_house_devices_status.return_value = batch(
        status_payload, tree_device(MASTER, name="  ", role=None), "garbage", {}
    )
    events = async_capture_events(hass, EVENT_DEVICE_CHANGED)

    await coordinator._async_update_data()
    await hass.async_block_till_done()

    assert coordinator._known_devices[MASTER].name == "Living room"
    assert not events
    assert not coordinator._rediscovery_requested


async def test_unknown_device_and_role_change_request_an_early_discovery(
    hass, houses_payload, status_payload
) -> None:
    """A device or role the discovery did not know triggers a new discovery."""
    coordinator, api = polling_coordinator(hass, houses_payload)
    api.async_house_devices_status.return_value = batch(
        status_payload, tree_device("NEWSERIAL0001", name="New")
    )
    await coordinator._async_update_data()
    assert coordinator._rediscovery_requested

    coordinator._rediscovery_requested = False
    await coordinator._async_update_data()
    assert not coordinator._rediscovery_requested

    api.async_house_devices_status.return_value = batch(
        status_payload, tree_device(MASTER, role="SlaveEqualMaster")
    )
    await coordinator._async_update_data()
    assert coordinator._known_devices[MASTER].role == "SlaveEqualMaster"
    assert coordinator._rediscovery_requested


async def test_discovery_reports_changes_since_the_last_one(
    hass, houses_payload
) -> None:
    """A rename that only discovery sees is published as well."""
    coordinator, _api = polling_coordinator(hass, houses_payload)
    await coordinator._async_discover()
    events = async_capture_events(hass, EVENT_DEVICE_CHANGED)
    device = houses_payload[0]["nonGeminiZones"][0]["rooms"][0]["devices"][0]
    device["radioFwVersion"] = "9.9.9"

    await coordinator._async_discover()
    await hass.async_block_till_done()

    assert len(events) == 1
    assert events[0].data["device_id"] is None
    assert events[0].data["changes"]["radio_firmware"]["new"] == "9.9.9"
    assert len(coordinator.firmware_history[MASTER]) == 2


async def test_firmware_history_is_bounded(hass, houses_payload) -> None:
    """Only the most recent firmware changes are kept."""
    coordinator, _api = polling_coordinator(hass, houses_payload)
    await coordinator._async_discover()
    device = houses_payload[0]["nonGeminiZones"][0]["rooms"][0]["devices"][0]
    for version in range(15):
        device["microFwVersion"] = f"2.0.{version}"
        await coordinator._async_discover()

    assert len(coordinator.firmware_history[MASTER]) == 10


# --- schedules --------------------------------------------------------------


async def test_schedules_are_only_requested_for_units_that_report_one(
    hass, houses_payload, status_payload
) -> None:
    """A unit reporting NotAvailable is never asked; a new schedule is found fast."""
    coordinator, api = polling_coordinator(hass, houses_payload)
    coordinator._feature_flags = {}
    coordinator._schedule_due = {MASTER}
    api.async_schedule.return_value = {"id": 5, "timeSlots": []}
    api.async_house_devices_status.return_value = batch(
        status_payload, isScheduled="NotAvailable"
    )

    data = await coordinator._async_update_data()
    assert api.async_schedule.await_count == 0
    assert data.devices[MASTER].schedule is None
    assert not coordinator._schedule_due

    api.async_house_devices_status.return_value = batch(
        status_payload, isScheduled="Off"
    )
    data = await coordinator._async_update_data()
    api.async_schedule.assert_awaited_once_with(101)
    assert data.devices[MASTER].schedule.schedule_id == 5

    await coordinator._async_update_data()
    assert api.async_schedule.await_count == 1

    api.async_house_devices_status.return_value = batch(
        status_payload, isScheduled="NotAvailable"
    )
    data = await coordinator._async_update_data()
    assert data.devices[MASTER].schedule is None


async def test_missing_or_failing_schedules_are_not_requested_every_poll(
    hass, houses_payload, status_payload
) -> None:
    """404 means no schedule; temporary errors wait for the next discovery."""
    coordinator, api = polling_coordinator(hass, houses_payload)
    coordinator._feature_flags = {}
    api.async_house_devices_status.return_value = batch(
        status_payload, isScheduled="On"
    )
    coordinator._schedule_due = {MASTER}
    api.async_schedule.side_effect = AmbientikaNoScheduleError(status=404)

    for _ in range(3):
        data = await coordinator._async_update_data()
    assert api.async_schedule.await_count == 1
    assert data.devices[MASTER].schedule is None

    coordinator._schedule_due = {MASTER}
    api.async_schedule.side_effect = AmbientikaServerError(status=503)
    for _ in range(2):
        await coordinator._async_update_data()
    assert api.async_schedule.await_count == 2

    coordinator._schedule_due = {MASTER}
    api.async_schedule.side_effect = AmbientikaAuthError
    with pytest.raises(ConfigEntryAuthFailed):
        await coordinator._async_update_data()


async def test_schedules_stay_off_when_the_server_disables_them(
    hass, houses_payload, status_payload
) -> None:
    """The weeklyScheduler flag still switches schedule requests off."""
    coordinator, api = polling_coordinator(hass, houses_payload)
    coordinator._feature_flags = {"weeklyScheduler": False}
    coordinator._schedule_due = {MASTER}
    api.async_house_devices_status.return_value = batch(
        status_payload, isScheduled="On"
    )

    await coordinator._async_update_data()

    api.async_schedule.assert_not_awaited()
    assert not coordinator._schedule_due


async def test_discovery_marks_controllable_devices_for_a_schedule_request(
    hass, houses_payload
) -> None:
    """After discovery, schedules are loaded with the next status update."""
    coordinator, api = polling_coordinator(hass, houses_payload)

    await coordinator._async_discover()

    assert coordinator._schedule_due == {MASTER, GEMINI}
    api.async_schedule.assert_not_awaited()


# --- small paths ------------------------------------------------------------


async def test_topology_signature_without_data_is_empty(hass) -> None:
    """Listeners are not notified before the first data arrives."""
    coordinator, _api = polling_coordinator(hass, [])
    changed = MagicMock()
    coordinator.async_add_topology_listener(changed)

    coordinator.async_update_listeners()

    changed.assert_not_called()
    assert coordinator._topology_signature() is None


async def test_status_fetch_errors_fail_the_poll_with_their_cause(
    hass, houses_payload
) -> None:
    """An API error that escapes the status fetch is reported as a failed poll."""
    coordinator, _api = polling_coordinator(hass, houses_payload)

    with (
        patch.object(
            coordinator,
            "_async_fetch_statuses",
            side_effect=AmbientikaServerError(status=502),
        ),
        pytest.raises(UpdateFailed, match="HTTP 502"),
    ):
        await coordinator._async_update_data()

    assert coordinator.last_failure_causes == ("http_5xx, HTTP 502",)


async def test_publishing_a_status_without_data_is_ignored(hass) -> None:
    """Late read-back results are dropped when no data exists."""
    coordinator, _api = polling_coordinator(hass, [])

    coordinator._apply_status(MASTER, AmbientikaStatus(serial_number=MASTER))

    assert coordinator.data is None
