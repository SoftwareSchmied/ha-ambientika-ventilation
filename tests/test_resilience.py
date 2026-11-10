"""Regression tests for the 0.9.3 write, error, and outage behavior."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import ClientSession
from custom_components.ambientika_ventilation.api import (
    AmbientikaApiClient,
    AmbientikaAuthError,
    AmbientikaConnectionError,
    AmbientikaDeviceNotFoundError,
    AmbientikaDeviceOfflineError,
    AmbientikaHouseNotConfiguredError,
    AmbientikaHouseNotFoundError,
    AmbientikaRateLimitError,
    AmbientikaResponseError,
    AmbientikaServerError,
)
from custom_components.ambientika_ventilation.const import DISCOVERY_INTERVAL, DOMAIN
from custom_components.ambientika_ventilation.coordinator import (
    AmbientikaCoordinator,
    mask_serial,
)
from custom_components.ambientika_ventilation.fan import AmbientikaFan
from custom_components.ambientika_ventilation.models import (
    AmbientikaData,
    AmbientikaDevice,
    AmbientikaDeviceData,
    AmbientikaStatus,
    parse_houses,
)
from custom_components.ambientika_ventilation.select import (
    SELECTS,
    AmbientikaSelect,
)
from custom_components.ambientika_ventilation.switch import AmbientikaScheduleSwitch
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    HomeAssistantError,
    ServiceValidationError,
)
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import MockConfigEntry

from .test_api import FakeResponse, future_expiry

MASTER = "AABBCCDDEEFF"
GEMINI = "112233445566"
NIGHT_STATUS = AmbientikaStatus(
    serial_number=MASTER,
    operating_mode="Auto",
    fan_speed="Night",
    humidity_level="Normal",
    light_sensor_level="Low",
    night_alarm=True,
    schedule_state="Off",
)


def write_coordinator(
    hass, status: AmbientikaStatus
) -> tuple[AmbientikaCoordinator, AsyncMock]:
    """Return a coordinator with one controllable master in the given state."""
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    api = AsyncMock()
    api.async_device_status.return_value = {
        "deviceSerialNumber": MASTER,
        "operatingMode": "Off",
        "fanSpeed": "Low",
    }
    coordinator = AmbientikaCoordinator(hass, entry, api)
    coordinator.data = AmbientikaData(
        devices={
            MASTER: AmbientikaDeviceData(
                device=AmbientikaDevice(
                    serial_number=MASTER, name="Unit", role="Master", house_id=10
                ),
                status=status,
            )
        }
    )
    return coordinator, api


def sent(api: AsyncMock) -> dict[str, Any]:
    """Return the keyword arguments of the last change-mode call."""
    return dict(api.async_change_mode.await_args.kwargs)


async def test_turn_off_with_night_fan_speed_sends_writable_speed(hass) -> None:
    """Switching off at night no longer fails on the automatic Night speed."""
    coordinator, api = write_coordinator(hass, NIGHT_STATUS)

    await AmbientikaFan(coordinator, MASTER).async_turn_off()

    assert sent(api)["operating_mode"] == "Off"
    assert sent(api)["fan_speed"] == "Low"


async def test_humidity_change_at_night_reuses_last_writable_speed(hass) -> None:
    """The last speed seen before the automatic Night speed is written back."""
    coordinator, api = write_coordinator(hass, NIGHT_STATUS)
    coordinator._remember_writable(
        MASTER, AmbientikaStatus(serial_number=MASTER, fan_speed="Medium")
    )
    humidity = next(item for item in SELECTS if item.key == "humidity_level")

    await AmbientikaSelect(coordinator, MASTER, humidity).async_select_option("dry")

    assert sent(api)["humidity_level"] == "Dry"
    assert sent(api)["fan_speed"] == "Medium"


async def test_light_sensor_change_at_night_succeeds(hass) -> None:
    """Light sensitivity in Auto mode is writable while the unit runs at Night."""
    coordinator, api = write_coordinator(hass, NIGHT_STATUS)
    light = next(item for item in SELECTS if item.key == "light_sensor_level")

    await AmbientikaSelect(coordinator, MASTER, light).async_select_option("medium")

    assert sent(api)["light_sensor_level"] == "Medium"
    assert sent(api)["fan_speed"] == "Low"


async def test_schedule_switch_at_night_succeeds(hass) -> None:
    """Disabling an active schedule at night sends a writable fan speed."""
    coordinator, api = write_coordinator(
        hass,
        AmbientikaStatus(
            serial_number=MASTER,
            operating_mode="Auto",
            fan_speed="Night",
            humidity_level="Normal",
            light_sensor_level="Low",
            schedule_state="On",
        ),
    )

    await AmbientikaScheduleSwitch(coordinator, MASTER).async_turn_off()

    assert sent(api)["schedule_mode"] is False
    assert sent(api)["fan_speed"] == "Low"


async def test_turn_on_from_off_with_night_speed_uses_writable_speed(hass) -> None:
    """A mode with an effective speed never receives the Night speed."""
    coordinator, api = write_coordinator(
        hass,
        AmbientikaStatus(
            serial_number=MASTER,
            operating_mode="Off",
            fan_speed="Night",
            humidity_level="Normal",
            light_sensor_level="Low",
            last_operating_mode="ManualHeatRecovery",
        ),
    )

    await AmbientikaFan(coordinator, MASTER).async_turn_on()

    assert sent(api)["operating_mode"] == "ManualHeatRecovery"
    assert sent(api)["fan_speed"] == "Low"


async def test_unknown_reported_values_are_replaced_or_rejected(hass) -> None:
    """Unknown humidity uses the app default; unknown light values are not guessed."""
    coordinator, api = write_coordinator(
        hass,
        AmbientikaStatus(
            serial_number=MASTER,
            operating_mode="Auto",
            fan_speed="Low",
            humidity_level="Wet",
            light_sensor_level="Low",
        ),
    )
    await coordinator.async_write_state(MASTER, operating_mode="Off")
    assert sent(api)["humidity_level"] == "Normal"

    coordinator, api = write_coordinator(
        hass,
        AmbientikaStatus(
            serial_number=MASTER,
            operating_mode="Auto",
            fan_speed="Low",
            humidity_level="Normal",
            light_sensor_level="Bright",
        ),
    )
    with pytest.raises(ServiceValidationError) as error:
        await coordinator.async_write_state(MASTER, operating_mode="Off")
    assert error.value.translation_key == "incomplete_writable_state"
    api.async_change_mode.assert_not_awaited()


async def test_unknown_current_mode_requires_an_explicit_mode(hass) -> None:
    """A legacy mode cannot be written back, but switching off still works."""
    coordinator, api = write_coordinator(
        hass,
        AmbientikaStatus(
            serial_number=MASTER,
            operating_mode="Turbo",
            fan_speed="Low",
            humidity_level="Normal",
            light_sensor_level="Low",
        ),
    )

    with pytest.raises(ServiceValidationError) as error:
        await coordinator.async_write_state(MASTER, schedule_mode=None)
    assert error.value.translation_key == "incomplete_writable_state"

    await coordinator.async_write_state(MASTER, operating_mode="Off")
    assert sent(api)["operating_mode"] == "Off"


async def test_polled_writable_values_are_remembered(hass) -> None:
    """Polling remembers writable speeds but never Turbo or Night."""
    coordinator, _api = write_coordinator(hass, NIGHT_STATUS)

    coordinator._remember_writable(MASTER, None)
    coordinator._remember_writable(
        MASTER, AmbientikaStatus(serial_number=MASTER, fan_speed="High")
    )
    coordinator._remember_writable(
        MASTER, AmbientikaStatus(serial_number=MASTER, fan_speed="Turbo")
    )
    coordinator._remember_writable(
        MASTER, AmbientikaStatus(serial_number=MASTER, fan_speed="Night")
    )

    assert coordinator._last_writable[MASTER]["fan_speed"] == "High"


async def test_device_offline_and_auth_errors_are_translated(hass) -> None:
    """Commands report offline devices and start reauthentication on 401."""
    coordinator, api = write_coordinator(
        hass,
        AmbientikaStatus(
            serial_number=MASTER,
            operating_mode="Auto",
            fan_speed="Low",
            humidity_level="Normal",
            light_sensor_level="Low",
            filter_status="Bad",
        ),
    )

    api.async_reset_filter.side_effect = AmbientikaDeviceOfflineError
    with pytest.raises(HomeAssistantError) as reset_error:
        await coordinator.async_reset_filter(MASTER)
    assert reset_error.value.translation_key == "device_offline"

    coordinator, api = write_coordinator(hass, NIGHT_STATUS)
    api.async_change_mode.side_effect = AmbientikaDeviceOfflineError
    with pytest.raises(HomeAssistantError) as offline:
        await coordinator.async_write_state(MASTER, operating_mode="Off")
    assert offline.value.translation_key == "device_offline"

    api.async_change_mode.side_effect = AmbientikaAuthError
    with (
        patch.object(coordinator.config_entry, "async_start_reauth") as start_reauth,
        pytest.raises(HomeAssistantError) as auth,
    ):
        await coordinator.async_write_state(MASTER, operating_mode="Off")
    assert auth.value.translation_key == "auth_failed"
    start_reauth.assert_called_once_with(hass)


def polling_coordinator(
    hass, houses_payload
) -> tuple[AmbientikaCoordinator, AsyncMock]:
    """Return a coordinator with two known devices of one house."""
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    api = AsyncMock()
    api.async_houses_info.return_value = houses_payload
    api.async_houses.return_value = []
    api.async_feature_flags.return_value = {"weeklyScheduler": False}
    coordinator = AmbientikaCoordinator(hass, entry, api)
    coordinator._known_devices = parse_houses(houses_payload)
    coordinator._last_discovery = datetime.now(UTC)
    return coordinator, api


@pytest.mark.parametrize(
    "error",
    [
        AmbientikaServerError(status=503),
        AmbientikaRateLimitError(status=429),
        AmbientikaConnectionError(kind="timeout"),
    ],
)
async def test_batch_outage_probes_only_one_device(hass, houses_payload, error) -> None:
    """A failing house batch does not multiply load with per-device requests."""
    coordinator, api = polling_coordinator(hass, houses_payload)
    api.async_house_devices_status.side_effect = error
    api.async_device_status.side_effect = AmbientikaServerError(status=503)

    with pytest.raises(UpdateFailed) as failure:
        await coordinator._async_update_data()

    assert api.async_device_status.await_count == 1
    assert "house status" in str(failure.value)
    assert "device …" in str(failure.value)
    assert MASTER not in str(failure.value)
    assert coordinator.last_failure_causes


async def test_batch_outage_with_working_probe_reads_all_devices(
    hass, houses_payload, status_payload
) -> None:
    """When only the batch endpoint fails, all devices are still read."""
    coordinator, api = polling_coordinator(hass, houses_payload)
    api.async_house_devices_status.side_effect = AmbientikaServerError(status=500)
    api.async_device_status.side_effect = lambda serial: {
        **status_payload,
        "deviceSerialNumber": serial,
    }

    data = await coordinator._async_update_data()

    assert api.async_device_status.await_count == 2
    assert not data.failed_devices


async def test_non_transient_batch_failure_keeps_individual_fallback(
    hass, houses_payload, status_payload
) -> None:
    """Parse failures and unconfigured houses still use per-device reads."""
    coordinator, api = polling_coordinator(hass, houses_payload)
    api.async_house_devices_status.side_effect = AmbientikaHouseNotConfiguredError
    api.async_device_status.side_effect = lambda serial: {
        **status_payload,
        "deviceSerialNumber": serial,
    }

    data = await coordinator._async_update_data()

    assert api.async_device_status.await_count == 2
    assert not data.failed_devices


async def test_skipped_devices_keep_last_values(
    hass, houses_payload, status_payload
) -> None:
    """Devices not probed during a batch outage keep their previous status."""
    coordinator, api = polling_coordinator(hass, houses_payload)
    api.async_house_devices_status.return_value = {
        "zoneDevicesInfo": [{"statusPacket": status_payload}],
        "geminiDevicesInfo": [
            {"statusPacket": {**status_payload, "deviceSerialNumber": GEMINI}}
        ],
    }
    coordinator.data = await coordinator._async_update_data()
    api.async_house_devices_status.side_effect = AmbientikaServerError(status=502)
    api.async_device_status.side_effect = AmbientikaServerError(status=502)

    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()

    assert api.async_device_status.await_count == 1
    assert coordinator.data.devices[MASTER].status.temperature == 21


async def test_house_not_found_requests_early_rediscovery(
    hass, houses_payload, status_payload
) -> None:
    """A deleted house triggers discovery before the six-hour interval."""
    coordinator, api = polling_coordinator(hass, houses_payload)
    api.async_house_devices_status.side_effect = AmbientikaHouseNotFoundError
    api.async_device_status.side_effect = lambda serial: {
        **status_payload,
        "deviceSerialNumber": serial,
    }
    await coordinator._async_update_data()
    api.async_houses_info.assert_not_awaited()
    assert coordinator._rediscovery_requested

    api.async_house_devices_status.side_effect = None
    api.async_house_devices_status.return_value = {}
    await coordinator._async_update_data()

    api.async_houses_info.assert_awaited_once()
    assert not coordinator._rediscovery_requested


async def test_removed_device_is_not_polled_every_cycle(
    hass, houses_payload, status_payload
) -> None:
    """An unknown serial is skipped and rediscovery is requested."""
    coordinator, api = polling_coordinator(hass, houses_payload)
    api.async_house_devices_status.return_value = {
        "zoneDevicesInfo": [{"statusPacket": status_payload}]
    }
    api.async_device_status.side_effect = AmbientikaDeviceNotFoundError
    houses_payload[0]["geminiDevices"] = []

    data = await coordinator._async_update_data()

    assert data.failed_devices == frozenset({GEMINI})
    assert coordinator._rediscovery_requested
    api.async_device_status.assert_awaited_once_with(GEMINI)

    data = await coordinator._async_update_data()

    api.async_houses_info.assert_awaited_once()
    assert GEMINI not in data.devices
    api.async_device_status.assert_awaited_once()


async def test_discovery_failures_back_off_and_log_once(
    hass, houses_payload, status_payload, caplog
) -> None:
    """Failed discovery is retried after 5, then 10 minutes and logged once."""
    caplog.set_level(logging.DEBUG)
    coordinator, api = polling_coordinator(hass, houses_payload)
    coordinator._last_discovery -= DISCOVERY_INTERVAL
    api.async_houses_info.side_effect = AmbientikaServerError(status=503)
    api.async_device_status.side_effect = lambda serial: {
        **status_payload,
        "deviceSerialNumber": serial,
    }

    await coordinator._async_update_data()
    await coordinator._async_update_data()

    assert api.async_houses_info.await_count == 1
    assert coordinator.discovery_failures == 1
    first_delay = coordinator.next_discovery_attempt - datetime.now(UTC)
    assert timedelta(minutes=4) < first_delay <= timedelta(minutes=5)

    coordinator.next_discovery_attempt = datetime.now(UTC) - timedelta(seconds=1)
    await coordinator._async_update_data()

    assert api.async_houses_info.await_count == 2
    second_delay = coordinator.next_discovery_attempt - datetime.now(UTC)
    assert timedelta(minutes=9) < second_delay <= timedelta(minutes=10)
    warnings = [
        record
        for record in caplog.records
        if record.levelno == logging.WARNING and "discovery" in record.getMessage()
    ]
    assert len(warnings) == 1
    assert "http_5xx, HTTP 503" in warnings[0].getMessage()

    api.async_houses_info.side_effect = None
    coordinator.next_discovery_attempt = datetime.now(UTC) - timedelta(seconds=1)
    await coordinator._async_update_data()

    assert coordinator.discovery_failures == 0
    assert coordinator.next_discovery_attempt is None
    assert "discovery recovered" in caplog.text


def test_discovery_backoff_schedule_is_capped(hass) -> None:
    """Retry delays grow from 5 minutes and never exceed six hours."""
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    coordinator = AmbientikaCoordinator(hass, entry, AsyncMock())
    delays = []
    for _ in range(9):
        coordinator._discovery_failed(AmbientikaServerError(status=500))
        delays.append(
            round(
                (coordinator.next_discovery_attempt - datetime.now(UTC)).total_seconds()
                / 60
            )
        )

    assert delays == [5, 10, 30, 60, 120, 240, 360, 360, 360]


async def test_total_outage_is_logged_once_with_causes(
    hass, houses_payload, status_payload, caplog
) -> None:
    """Home Assistant logs one outage line that names the failure causes."""
    coordinator, api = polling_coordinator(hass, houses_payload)
    api.async_house_devices_status.return_value = {
        "zoneDevicesInfo": [{"statusPacket": status_payload}],
        "geminiDevicesInfo": [
            {"statusPacket": {**status_payload, "deviceSerialNumber": GEMINI}}
        ],
    }
    await coordinator.async_refresh()
    assert coordinator.last_update_success

    api.async_house_devices_status.side_effect = AmbientikaConnectionError(
        kind="timeout"
    )
    api.async_device_status.side_effect = AmbientikaConnectionError(kind="timeout")
    await coordinator.async_refresh()
    await coordinator.async_refresh()

    errors = [
        record.getMessage()
        for record in caplog.records
        if record.levelno >= logging.WARNING
    ]
    assert len(errors) == 1
    assert "house status: timeout" in errors[0]
    assert f"device {mask_serial(GEMINI)}: timeout" in errors[0]
    assert MASTER not in caplog.text and GEMINI not in caplog.text


async def test_partial_failure_warning_names_the_cause(
    hass, houses_payload, status_payload, caplog
) -> None:
    """A newly failing device is logged once together with its cause."""
    coordinator, api = polling_coordinator(hass, houses_payload)
    api.async_house_devices_status.return_value = {
        "zoneDevicesInfo": [{"statusPacket": status_payload}]
    }
    api.async_device_status.side_effect = AmbientikaResponseError(
        "bad", kind="http_4xx", status=400, trace_id="tid"
    )

    await coordinator._async_update_data()

    assert "temporarily unavailable" in caplog.text
    assert "http_4xx, HTTP 400, traceId tid" in caplog.text


async def test_real_client_outage_profile_bounds_http_requests(
    hass, houses_payload
) -> None:
    """With the cloud returning 503, a poll cycle sends two requests."""
    session = MagicMock(spec=ClientSession)
    session.request = AsyncMock(return_value=FakeResponse(503))
    client = AmbientikaApiClient(
        cast(ClientSession, session),
        "https://ambientika.test",
        "user@example.com",
        "password",
        token="token",
        user_id=1,
        expires_at=future_expiry(),
    )
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    coordinator = AmbientikaCoordinator(hass, entry, client)
    coordinator._known_devices = parse_houses(houses_payload)
    coordinator._last_discovery = datetime.now(UTC) - DISCOVERY_INTERVAL

    with patch(
        "custom_components.ambientika_ventilation.api.asyncio.sleep", new=AsyncMock()
    ):
        for _ in range(3):
            with pytest.raises(UpdateFailed):
                await coordinator._async_update_data()

    paths = [
        call.args[1].removeprefix("https://ambientika.test")
        for call in (session.request.await_args_list)
    ]
    # Discovery: 3 attempts once (then backed off). Each cycle: one house
    # batch request plus one probing device read, both with a single attempt.
    assert paths.count("/House/houses-info") == 3
    assert paths.count("/Device/house-devices-status") == 3
    assert paths.count("/Device/device-status") == 3
    assert len(paths) == 9
    assert client.metrics.error_kinds == {"http_5xx": 9}
    # Failed cycles stretch the polling interval up to ten minutes.
    assert coordinator.consecutive_failures == 3
    assert coordinator.update_interval == timedelta(minutes=8)


async def test_batch_auth_failure_requires_reauth(hass, houses_payload) -> None:
    """An expired token on the house batch starts reauthentication."""
    coordinator, api = polling_coordinator(hass, houses_payload)
    api.async_house_devices_status.side_effect = AmbientikaAuthError

    with pytest.raises(ConfigEntryAuthFailed):
        await coordinator._async_update_data()


async def test_initial_discovery_failure_records_cause(hass) -> None:
    """A failing first discovery keeps its cause for diagnostics."""
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    api = AsyncMock()
    api.async_houses_info.side_effect = AmbientikaServerError(status=500)
    coordinator = AmbientikaCoordinator(hass, entry, api)

    with pytest.raises(UpdateFailed, match="http_5xx"):
        await coordinator._async_update_data()

    assert coordinator.last_failure_causes == ("discovery: http_5xx, HTTP 500",)
