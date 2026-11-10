"""Tests for Turbo handling, unknown values, set_state, options and diagnostics."""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import voluptuous as vol
from custom_components.ambientika_ventilation import (
    async_setup_entry,
    create_session,
)
from custom_components.ambientika_ventilation.api import (
    AmbientikaApiClient,
    AmbientikaServerError,
    RequestMetrics,
)
from custom_components.ambientika_ventilation.binary_sensor import (
    AmbientikaCloudConnection,
)
from custom_components.ambientika_ventilation.binary_sensor import (
    async_setup_entry as setup_binary_sensors,
)
from custom_components.ambientika_ventilation.config_flow import (
    AmbientikaOptionsFlow,
)
from custom_components.ambientika_ventilation.const import DOMAIN
from custom_components.ambientika_ventilation.diagnostics import (
    async_get_config_entry_diagnostics,
    async_get_device_diagnostics,
)
from custom_components.ambientika_ventilation.fan import (
    SET_STATE_SCHEMA,
    AmbientikaFan,
)
from custom_components.ambientika_ventilation.fan import (
    async_setup_entry as setup_fans,
)
from custom_components.ambientika_ventilation.hub import AmbientikaHub
from custom_components.ambientika_ventilation.models import (
    AmbientikaData,
    AmbientikaDevice,
    AmbientikaDeviceData,
    AmbientikaStatus,
    parse_house_status_metadata,
    parse_status,
    turbo_usable,
)
from custom_components.ambientika_ventilation.select import SELECTS, AmbientikaSelect
from custom_components.ambientika_ventilation.sensor import (
    DEVICE_SENSORS,
    SENSORS,
    AmbientikaDeviceSensor,
    AmbientikaSensor,
)
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import ServiceValidationError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from .hub_helpers import fake_entry, runtime
from .test_entities import SERIAL, coordinator_with_status

SENSOR = {description.key: description for description in SENSORS}


# --- parsing ----------------------------------------------------------------


def test_status_fields_are_read_regardless_of_capitalization() -> None:
    """The cloud has delivered both camelCase and PascalCase names."""
    status = parse_status(
        {
            "DeviceSerialNumber": SERIAL,
            "OperatingMode": "Auto",
            "FanSpeed": "High",
            "IsTurboAvailable": True,
            "FiltersStatus": "Good",
            "SignalStrenght": 70,
        },
        "fallback",
    )

    assert status.serial_number == SERIAL
    assert (status.operating_mode, status.fan_speed) == ("Auto", "High")
    assert status.turbo_available is True
    assert status.filter_status == "Good"
    assert status.signal_strength == 70


def test_legacy_turbo_mode_is_a_read_only_state() -> None:
    """Numeric mode 12 is the retired Turbo mode; other numbers stay unknown."""
    legacy = parse_status({"operatingMode": 12, "lastOperatingMode": 12}, SERIAL)
    unknown = parse_status({"operatingMode": 99, "fanSpeed": 42}, SERIAL)
    named = parse_status({"operatingMode": "Turbo"}, SERIAL)

    assert legacy.operating_mode == legacy.last_operating_mode == "Turbo"
    assert named.operating_mode == "Turbo"
    assert unknown.operating_mode is None
    assert unknown.fan_speed is None


@pytest.mark.parametrize(
    ("signal", "light", "expected"),
    [
        (0, "NotAvailable", None),
        (0, "Low", 0),
        (73, "NotAvailable", 73),
        (None, "NotAvailable", None),
    ],
)
def test_signal_zero_is_unknown_for_firmware_without_a_signal_field(
    signal, light, expected
) -> None:
    """Firmware 0.0.11 reports 0 together with an unavailable light sensor."""
    status = parse_status({"signalStrenght": signal, "lightSensorLevel": light}, SERIAL)

    assert status.signal_strength == expected


@pytest.mark.parametrize(
    ("available", "flags", "expected"),
    [
        (True, {}, True),
        (True, {"turboMode": True}, True),
        (True, {"turboMode": False}, False),
        (False, {"turboMode": True}, False),
        (False, {}, False),
    ],
)
def test_turbo_needs_the_unit_and_the_server(available, flags, expected) -> None:
    """A missing server flag counts as on; the unit must report Turbo."""
    status = AmbientikaStatus(serial_number=SERIAL, turbo_available=available)

    assert turbo_usable(status, flags) is expected
    assert turbo_usable(None, flags) is False


def test_house_status_metadata_is_parsed_tolerantly() -> None:
    """The zone tree yields metadata and ignores malformed entries."""
    payload = {
        "zoneDevicesInfo": [
            {
                "zone": {
                    "rooms": [
                        {
                            "devices": [
                                {
                                    "serialNumber": SERIAL,
                                    "name": "Unit",
                                    "role": -1,
                                    "microFwVersion": "1.0",
                                    "radioFwVersion": "2.0",
                                    "radioAtCommandsFwVersion": "3.0",
                                    "zoneIndex": 2,
                                },
                                {"name": "no serial"},
                                "garbage",
                            ]
                        },
                        "garbage",
                    ]
                }
            },
            "garbage",
        ],
        "geminiDevicesInfo": [{"device": {"serialNumber": "G1", "role": "Master"}}],
    }

    metadata = parse_house_status_metadata(payload)

    assert set(metadata) == {SERIAL, "G1"}
    assert metadata[SERIAL].role == "NotConfigured"
    assert metadata[SERIAL].micro_firmware == "1.0"
    assert metadata[SERIAL].zone_index == 2
    assert metadata["G1"].name is None
    assert parse_house_status_metadata(None) == {}
    assert parse_house_status_metadata({"zoneDevicesInfo": "x"}) == {}


# --- fan --------------------------------------------------------------------


def test_turbo_speed_follows_the_server_feature_flag() -> None:
    """Switching Turbo off on the server removes the fourth speed."""
    coordinator = coordinator_with_status()
    fan = AmbientikaFan(coordinator, SERIAL)
    assert fan.speed_count == 4

    coordinator.data.feature_flags["turboMode"] = False
    assert fan.speed_count == 3
    assert fan._speed_for_percentage(100) == "High"


async def test_set_state_writes_all_values_at_once() -> None:
    """The action maps Home Assistant option names to API values."""
    coordinator = coordinator_with_status()
    fan = AmbientikaFan(coordinator, SERIAL)

    await fan.async_set_state(
        operating_mode="heat_recovery",
        fan_speed="turbo",
        humidity_level="moist",
        light_sensor_level="off",
    )

    coordinator.async_write_state.assert_awaited_once_with(
        SERIAL,
        operating_mode="ManualHeatRecovery",
        fan_speed="Turbo",
        humidity_level="Moist",
        light_sensor_level="Off",
    )


async def test_set_state_rejects_empty_unknown_and_unavailable_values() -> None:
    """Bad input never reaches the cloud."""
    coordinator = coordinator_with_status()
    fan = AmbientikaFan(coordinator, SERIAL)

    with pytest.raises(ServiceValidationError) as error:
        await fan.async_set_state()
    assert error.value.translation_key == "set_state_empty"

    with pytest.raises(ServiceValidationError) as error:
        await fan.async_set_state(fan_speed="warp")
    assert error.value.translation_key == "unsupported_select_option"

    coordinator.data.feature_flags["turboMode"] = False
    with pytest.raises(ServiceValidationError) as error:
        await fan.async_set_state(fan_speed="turbo")
    assert error.value.translation_key == "unsupported_select_option"
    coordinator.async_write_state.assert_not_awaited()


async def test_set_state_action_is_registered_with_a_schema(hass, fan_platform) -> None:
    """The fan platform registers the entity service on setup."""
    entry = fake_entry(coordinator_with_status())

    await setup_fans(hass, entry, MagicMock())

    name, schema, method = fan_platform.async_register_entity_service.call_args.args
    assert (name, method) == ("set_state", "async_set_state")
    assert schema({"fan_speed": "high"}) == {"fan_speed": "high"}
    assert SET_STATE_SCHEMA({}) == {}
    with pytest.raises(vol.Invalid):
        schema({"fan_speed": "extreme"})
    with pytest.raises(vol.Invalid):
        schema({"unknown_field": "x"})


# --- select and sensors -----------------------------------------------------


def test_operating_mode_options_follow_the_current_device_type() -> None:
    """Options are computed from live data, not frozen at creation."""
    coordinator = coordinator_with_status()
    select = AmbientikaSelect(coordinator, SERIAL, SELECTS[0])
    assert "away" in select.options

    device_data = coordinator.data.devices[SERIAL]
    coordinator.data.devices[SERIAL] = replace(
        device_data, device=replace(device_data.device, device_type="Gemini")
    )

    assert "away" not in select.options
    assert "smart" in select.options


def test_select_shows_unknown_for_values_it_cannot_set() -> None:
    """The retired Turbo mode and unknown values are not selectable options."""
    coordinator = coordinator_with_status()
    select = AmbientikaSelect(coordinator, SERIAL, SELECTS[0])

    for value in ("Turbo", "Mystery"):
        device_data = coordinator.data.devices[SERIAL]
        coordinator.data.devices[SERIAL] = replace(
            device_data, status=replace(device_data.status, operating_mode=value)
        )
        assert select.current_option is None


def test_enum_sensors_report_unknown_values_with_the_raw_text() -> None:
    """Unknown enum values become unknown and keep the raw value as attribute."""
    coordinator = coordinator_with_status()
    last_mode = AmbientikaSensor(coordinator, SERIAL, SENSOR["last_operating_mode"])
    air = AmbientikaSensor(coordinator, SERIAL, SENSOR["air_quality"])
    temperature = AmbientikaSensor(coordinator, SERIAL, SENSOR["temperature"])

    def set_status(**values: object) -> None:
        device_data = coordinator.data.devices[SERIAL]
        coordinator.data.devices[SERIAL] = replace(
            device_data, status=replace(device_data.status, **values)
        )

    set_status(last_operating_mode="Turbo", air_quality="Foggy")
    assert last_mode.native_value == "turbo"
    assert last_mode.extra_state_attributes is None
    assert air.native_value is None
    assert air.extra_state_attributes == {"raw_value": "Foggy"}
    assert temperature.extra_state_attributes is None

    set_status(air_quality=None)
    assert air.extra_state_attributes is None

    coordinator.data.devices[SERIAL] = replace(
        coordinator.data.devices[SERIAL], status=None
    )
    assert air.extra_state_attributes is None


def test_device_enum_sensor_ignores_unknown_roles() -> None:
    """A role the integration does not know yields an unknown state."""
    coordinator = coordinator_with_status()
    description = next(item for item in DEVICE_SENSORS if item.key == "device_role")
    sensor = AmbientikaDeviceSensor(coordinator, SERIAL, description)
    assert sensor.native_value == "master"

    device_data = coordinator.data.devices[SERIAL]
    coordinator.data.devices[SERIAL] = replace(
        device_data, device=replace(device_data.device, role="Overlord")
    )
    assert sensor.native_value is None


# --- cloud connection -------------------------------------------------------


def test_cloud_connection_stays_available_while_the_unit_is_offline() -> None:
    """The sensor reports disconnected instead of unavailable."""
    coordinator = coordinator_with_status()
    sensor = AmbientikaCloudConnection(coordinator, SERIAL)
    assert sensor.unique_id == f"{SERIAL}_cloud_connection"
    assert sensor.available
    assert sensor.is_on is True

    coordinator.data = replace(coordinator.data, offline_devices=frozenset({SERIAL}))
    assert sensor.available
    assert sensor.is_on is False

    coordinator.data = replace(
        coordinator.data,
        offline_devices=frozenset(),
        failed_devices=frozenset({SERIAL}),
    )
    assert sensor.is_on is False

    coordinator.data = replace(coordinator.data, failed_devices=frozenset())
    coordinator.last_update_success = False
    assert sensor.is_on is False

    coordinator.last_update_success = True
    coordinator.data = AmbientikaData()
    assert not sensor.available


async def test_cloud_connection_is_created_for_configured_devices(hass) -> None:
    """Devices that never reported a status still get a connection sensor."""
    coordinator = coordinator_with_status()
    coordinator.data.devices["SECOND000001"] = AmbientikaDeviceData(
        device=AmbientikaDevice(serial_number="SECOND000001", name="B", role="Master")
    )
    coordinator.data.devices["SPARE0000001"] = AmbientikaDeviceData(
        device=AmbientikaDevice(
            serial_number="SPARE0000001", name="C", role="NotConfigured"
        )
    )
    entry = fake_entry(coordinator)
    entities: list[object] = []

    await setup_binary_sensors(hass, entry, entities.extend)

    connection = [e for e in entities if isinstance(e, AmbientikaCloudConnection)]
    assert sorted(e._serial for e in connection) == [SERIAL, "SECOND000001"]


# --- setup, options, diagnostics -------------------------------------------


async def test_session_keeps_connections_alive_and_bounds_requests(hass) -> None:
    """The dedicated session reuses TLS connections for two minutes."""
    session = create_session(hass)
    try:
        assert session.connector._keepalive_timeout == 120
        assert (session.timeout.total, session.timeout.connect) == (10, 5)
    finally:
        await session.close()


async def test_failed_token_renewal_is_logged_and_does_not_block_setup(
    hass, caplog
) -> None:
    """A temporary error while renewing early is a warning only."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"username": "u@example.com", "password": "p"},
    )
    entry.add_to_hass(hass)
    api = MagicMock()
    api.async_renew_token_if_due = AsyncMock(
        side_effect=AmbientikaServerError(status=503)
    )
    coordinator = MagicMock()
    coordinator.base_interval = timedelta(seconds=60)
    coordinator.async_config_entry_first_refresh = AsyncMock()
    session = MagicMock()
    session.close = AsyncMock()

    with (
        patch(
            "custom_components.ambientika_ventilation.create_session",
            return_value=session,
        ),
        patch(
            "custom_components.ambientika_ventilation.AmbientikaApiClient",
            return_value=api,
        ),
        patch(
            "custom_components.ambientika_ventilation.AmbientikaCoordinator",
            return_value=coordinator,
        ),
        patch.object(hass.config_entries, "async_forward_entry_setups", AsyncMock()),
        caplog.at_level(logging.WARNING),
    ):
        assert await async_setup_entry(hass, entry) is True
        await entry._async_process_on_unload(hass)

    assert "Unable to renew the Ambientika token" in caplog.text
    assert "http_5xx, HTTP 503" in caplog.text


async def test_successful_early_renewal_is_logged(hass, caplog) -> None:
    """A renewed token is noted once."""
    entry = MockConfigEntry(
        domain=DOMAIN, data={"username": "u@example.com", "password": "p"}
    )
    entry.add_to_hass(hass)
    api = MagicMock(spec=AmbientikaApiClient)
    api.async_renew_token_if_due = AsyncMock(return_value=True)
    coordinator = MagicMock()
    coordinator.base_interval = timedelta(seconds=60)
    coordinator.async_config_entry_first_refresh = AsyncMock()
    session = MagicMock()
    session.close = AsyncMock()

    with (
        patch(
            "custom_components.ambientika_ventilation.create_session",
            return_value=session,
        ),
        patch(
            "custom_components.ambientika_ventilation.AmbientikaApiClient",
            return_value=api,
        ),
        patch(
            "custom_components.ambientika_ventilation.AmbientikaCoordinator",
            return_value=coordinator,
        ),
        patch.object(hass.config_entries, "async_forward_entry_setups", AsyncMock()),
        caplog.at_level(logging.INFO),
    ):
        assert await async_setup_entry(hass, entry) is True
        await entry._async_process_on_unload(hass)

    assert "Renewed the Ambientika token ahead of its expiry" in caplog.text


async def test_options_flow_stores_the_polling_interval(hass) -> None:
    """The options flow validates and saves the interval and reloads the entry."""
    entry = MockConfigEntry(domain=DOMAIN, data={}, options={"scan_interval": 90})
    entry.add_to_hass(hass)

    with patch.object(hass.config_entries, "async_schedule_reload") as reload:
        result = await hass.config_entries.options.async_init(entry.entry_id)
        assert result["type"] is FlowResultType.FORM
        schema_key = next(iter(result["data_schema"].schema))
        assert schema_key.default() == 90

        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"scan_interval": 45}
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["scan_interval"] == 45
    reload.assert_called_once_with(entry.entry_id)


async def test_options_flow_defaults_to_sixty_seconds(hass) -> None:
    """Without a stored value the form proposes the default interval."""
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)

    assert next(iter(result["data_schema"].schema)).default() == 60
    assert isinstance(AmbientikaOptionsFlow(), AmbientikaOptionsFlow)


async def test_device_diagnostics_mask_the_serial_and_include_history(hass) -> None:
    """Per-device diagnostics expose state and firmware history only."""
    coordinator = coordinator_with_status()
    coordinator.offline_devices = {SERIAL}
    coordinator.firmware_history = {SERIAL: [{"micro": "1.0"}]}
    coordinator._status_errors = {SERIAL: 2}
    entry = SimpleNamespace(runtime_data=runtime(coordinator))
    device = SimpleNamespace(identifiers={(DOMAIN, SERIAL)})

    result = await async_get_device_diagnostics(hass, entry, device)

    assert result["known"] is True
    assert result["redacted_id"] == "***EEFF"
    assert result["offline"] is True
    assert result["status_error_streak"] == 2
    assert result["firmware"]["history"] == [{"micro": "1.0"}]
    assert result["status"]["operating_mode"] == "ManualHeatRecovery"
    assert SERIAL not in repr(result)
    assert "Living room" not in repr(result)

    coordinator.data.devices[SERIAL] = replace(
        coordinator.data.devices[SERIAL], status=None
    )
    result = await async_get_device_diagnostics(hass, entry, device)
    assert result["status"] is None

    stranger = SimpleNamespace(identifiers={(DOMAIN, "OTHER"), ("other", SERIAL)})
    assert await async_get_device_diagnostics(hass, entry, stranger) == {"known": False}


async def test_config_entry_diagnostics_include_token_and_poll_history(hass) -> None:
    """The entry diagnostics show the pace and the token expiry, not the token."""
    coordinator = coordinator_with_status()
    coordinator.last_successful_update = None
    coordinator.last_update_duration_ms = 5
    coordinator.last_partial_failures = 0
    coordinator.last_failure_causes = ()
    coordinator.discovery_failures = 0
    coordinator.next_discovery_attempt = None
    coordinator.offline_devices = set()
    coordinator.offline_transitions = 0
    coordinator.update_interval = None
    coordinator.base_interval = timedelta(seconds=60)
    coordinator.consecutive_failures = 3
    coordinator.cycle_durations_ms = deque([5, 6], maxlen=20)
    coordinator.discarded_polls = 1
    coordinator.device_changes = 2
    metrics = RequestMetrics()
    metrics.last_response_shapes["/Device/device-status"] = {"temperature": "int"}
    entry = SimpleNamespace(
        runtime_data=SimpleNamespace(
            coordinator=coordinator,
            api=SimpleNamespace(metrics=metrics, token_expires_at="2027-02-18"),
            hub=AmbientikaHub(),
            local=SimpleNamespace(diagnostics=lambda: {"mode": "auto"}),
            analytics=SimpleNamespace(diagnostics=lambda: {"units": 0}),
        ),
        version=1,
        minor_version=1,
        data={},
    )
    result = await async_get_config_entry_diagnostics(hass, entry)

    assert result["coordinator"]["poll_interval_s"] is None
    assert result["coordinator"]["consecutive_failures"] == 3
    assert result["coordinator"]["discarded_polls"] == 1
    assert result["token"] == {"expires_at": "2027-02-18", "refreshes": 0}
    assert result["request_metrics"]["last_response_shapes"] == {
        "/Device/device-status": {"temperature": "int"}
    }
    assert result["devices"][0]["capabilities"]["turbo"] is True


# --- platform setup paths ---------------------------------------------------


async def test_platform_callbacks_ignore_devices_they_already_know(hass) -> None:
    """Running the topology callback again adds nothing twice."""
    from custom_components.ambientika_ventilation.models import (
        AmbientikaSchedule,
    )
    from custom_components.ambientika_ventilation.select import (
        async_setup_entry as setup_selects,
    )
    from custom_components.ambientika_ventilation.sensor import (
        AmbientikaScheduleSensor,
    )
    from custom_components.ambientika_ventilation.sensor import (
        async_setup_entry as setup_sensors,
    )

    coordinator = coordinator_with_status()
    coordinator.data.devices[SERIAL] = replace(
        coordinator.data.devices[SERIAL], schedule=AmbientikaSchedule(schedule_id=3)
    )
    entry = fake_entry(coordinator)
    for setup in (setup_selects, setup_sensors):
        entities: list[object] = []
        await setup(hass, entry, entities.extend)
        callback = coordinator.async_add_topology_listener.call_args.args[0]
        before = len(entities)
        callback()
        assert len(entities) == before
        if setup is setup_sensors:
            assert any(isinstance(e, AmbientikaScheduleSensor) for e in entities)


def test_entities_without_status_report_unknown_values() -> None:
    """Select and fan stay unknown when the unit has no status."""
    coordinator = coordinator_with_status()
    select = AmbientikaSelect(coordinator, SERIAL, SELECTS[1])
    fan = AmbientikaFan(coordinator, SERIAL)
    device_data = coordinator.data.devices[SERIAL]

    coordinator.data.devices[SERIAL] = replace(
        device_data, status=replace(device_data.status, fan_speed="Night")
    )
    assert fan.percentage is None

    coordinator.data.devices[SERIAL] = replace(device_data, status=None)
    assert select.current_option is None
