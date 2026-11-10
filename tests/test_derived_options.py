"""Options of the derived data, house diagnostic sensors and privacy."""

from __future__ import annotations

import json
from dataclasses import replace
from unittest.mock import patch

import pytest
from custom_components.ambientika_ventilation.analytics_manager import (
    analytics_config,
    room_volumes,
)
from custom_components.ambientika_ventilation.diagnostics import (
    async_get_config_entry_diagnostics,
    async_get_device_diagnostics,
)
from custom_components.ambientika_ventilation.sensor import (
    HOUSE_SENSORS,
    AmbientikaHouseSensor,
    _house_time,
    _house_time_zone,
)
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from .test_entities import SERIAL, coordinator_with_status
from .test_local_wiring import DATA, GEMINI, entity_id, setup_entry, state_of
from .wiring_helpers import (
    MASTER,
    fake_listeners,
    mock_api,
    patch_session,
)

ADDRESS = "Example street 12, Sampletown"
LATITUDE = 47.123456
LONGITUDE = 8.654321
HOUSE_META = {
    "id": 10,
    "address": ADDRESS,
    "latitude": LATITUDE,
    "longitude": LONGITUDE,
    "timezone": 1,
    "ianaTimezone": "Europe/Berlin",
    "currentHouseTime": "2026-01-01T12:00:00",
}


@pytest.fixture
def cloud(houses_payload, status_payload):
    api = mock_api(houses_payload, status_payload)
    with patch_session(api), fake_listeners():
        yield api


@pytest.fixture
def located(houses_payload, status_payload):
    """A cloud that reports address, coordinates and time of the house."""
    api = mock_api(houses_payload, status_payload)
    api.async_houses.return_value = [HOUSE_META]
    with patch_session(api), fake_listeners():
        yield api


def house_device_of(hass: HomeAssistant, entry):
    """Return the house device of an entry."""
    return next(
        device
        for device in dr.async_entries_for_config_entry(
            dr.async_get(hass), entry.entry_id
        )
        if ("ambientika_ventilation", "house_10") in device.identifiers
    )


# --- options flow ------------------------------------------------------------


async def test_options_flow_collects_outdoor_basis_and_room_volumes(
    hass: HomeAssistant, cloud
) -> None:
    """The second step lists the units by name and stores volumes by serial."""
    entry = await setup_entry(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["step_id"] == "init"
    keys = {str(key) for key in result["data_schema"].schema}
    assert {
        "outdoor_weather",
        "outdoor_temperature",
        "outdoor_humidity",
        "filter_basis",
    } <= keys

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "scan_interval": 60,
            "local_udp": "on",
            "raw_variants": False,
            "outdoor_weather": "weather.home",
            "outdoor_temperature": "sensor.out_t",
            "filter_basis": "on_time",
        },
    )
    assert result["step_id"] == "volumes"
    assert {str(key) for key in result["data_schema"].schema} == {
        "Living room",
        "Bedroom",
    }

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"Living room": 30, "Bedroom": 22.5}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()

    assert entry.options["room_volumes"] == {MASTER: 30.0, GEMINI: 22.5}
    assert entry.options["outdoor_weather"] == "weather.home"
    assert entry.options["outdoor_temperature"] == "sensor.out_t"
    assert "outdoor_humidity" not in entry.options
    assert entry.options["filter_basis"] == "on_time"

    # The entry reloaded: the engine uses the options and the entities exist.
    analytics = entry.runtime_data.analytics
    assert analytics.config.filter_basis == "on_time"
    assert analytics.config.room_volumes == {MASTER: 30.0, GEMINI: 22.5}
    assert entity_id(hass, "sensor", f"{MASTER}_air_change_rate")
    assert entity_id(hass, "sensor", "10_outdoor_dew_point")


async def test_options_flow_shows_stored_values_and_allows_clearing(
    hass: HomeAssistant, cloud
) -> None:
    """Stored volumes and sources are suggested; an empty field removes them."""
    entry = await setup_entry(
        hass,
        {
            "room_volumes": {MASTER: 30.0},
            "outdoor_weather": "weather.home",
            "filter_basis": "on_time",
        },
    )
    result = await hass.config_entries.options.async_init(entry.entry_id)
    suggested = {
        str(key): key.description["suggested_value"]
        for key in result["data_schema"].schema
        if key.description
    }
    assert suggested["outdoor_weather"] == "weather.home"
    defaults = {
        str(key): key.default()
        for key in result["data_schema"].schema
        if callable(key.default)
    }
    assert defaults["filter_basis"] == "on_time"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "scan_interval": 60,
            "local_udp": "on",
            "raw_variants": False,
            "filter_basis": "operating",
        },
    )
    volumes_keys = {
        str(key): key.description["suggested_value"]
        for key in result["data_schema"].schema
    }
    assert volumes_keys == {"Living room": 30.0, "Bedroom": None}

    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert "room_volumes" not in entry.options
    assert "outdoor_weather" not in entry.options
    assert entry.options["filter_basis"] == "operating"


async def test_options_flow_disambiguates_equal_unit_names(
    hass: HomeAssistant, houses_payload, status_payload
) -> None:
    """Two units with one name stay selectable."""
    houses_payload[0]["geminiDevices"][0]["name"] = "Living room"
    api = mock_api(houses_payload, status_payload)
    with patch_session(api), fake_listeners():
        entry = await setup_entry(hass)
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {
                "scan_interval": 60,
                "local_udp": "on",
                "raw_variants": False,
                "filter_basis": "operating",
            },
        )
        assert {str(key) for key in result["data_schema"].schema} == {
            "Living room",
            "Living room (2)",
        }
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"Living room (2)": 15}
        )
        await hass.async_block_till_done()
    assert entry.options["room_volumes"] == {MASTER: 15.0}


async def test_options_flow_of_an_unloaded_entry_keeps_the_stored_volumes(
    hass: HomeAssistant,
) -> None:
    """Without loaded units there is no volume step and nothing is lost."""
    entry = MockConfigEntry(
        domain="ambientika_ventilation",
        data=DATA,
        options={"room_volumes": {SERIAL: 18.0}},
    )
    entry.add_to_hass(hass)
    with patch.object(hass.config_entries, "async_schedule_reload"):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {
                "scan_interval": 60,
                "local_udp": "auto",
                "raw_variants": False,
                "filter_basis": "operating",
            },
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["room_volumes"] == {SERIAL: 18.0}


def test_filter_basis_option_is_validated() -> None:
    """Only the two documented bases reach the engine."""
    entry = MockConfigEntry(domain="ambientika_ventilation", options={})
    assert analytics_config(entry).filter_basis == "operating"
    entry = MockConfigEntry(
        domain="ambientika_ventilation", options={"filter_basis": "on_time"}
    )
    assert analytics_config(entry).filter_basis == "on_time"
    entry = MockConfigEntry(
        domain="ambientika_ventilation", options={"filter_basis": "x"}
    )
    assert analytics_config(entry).filter_basis == "operating"
    assert room_volumes(entry) == {}


# --- house diagnostic sensors ----------------------------------------------------


async def test_house_sensors_are_disabled_by_default_diagnostics(
    hass: HomeAssistant, located
) -> None:
    """Address, coordinates, time zone and time are separate disabled sensors."""
    entry = await setup_entry(hass)
    registry = er.async_get(hass)
    devices = dr.async_get(hass)
    for key in ("address", "latitude", "longitude", "time_zone", "time"):
        item = registry.async_get(entity_id(hass, "sensor", f"10_{key}"))
        assert item.disabled_by is er.RegistryEntryDisabler.INTEGRATION, key
        assert item.entity_category is er.EntityCategory.DIAGNOSTIC, key
        assert hass.states.get(item.entity_id) is None
        assert devices.async_get(item.device_id).identifiers == {
            ("ambientika_ventilation", "house_10")
        }

    # Once the user enables them they report the values.
    for key in ("address", "latitude", "longitude", "time_zone", "time"):
        registry.async_update_entity(
            entity_id(hass, "sensor", f"10_{key}"), disabled_by=None
        )
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert state_of(hass, "sensor", "10_address").state == ADDRESS
    assert float(state_of(hass, "sensor", "10_latitude").state) == LATITUDE
    assert float(state_of(hass, "sensor", "10_longitude").state) == LONGITUDE
    assert state_of(hass, "sensor", "10_time_zone").state == "Europe/Berlin"
    assert state_of(hass, "sensor", "10_time").state.startswith("2026-01-01T11:00:00")


async def test_house_sensors_need_a_reported_value(hass: HomeAssistant, cloud) -> None:
    """Without house metadata no house sensor is created."""
    entry = await setup_entry(hass)
    ids = {
        item.unique_id
        for item in er.async_entries_for_config_entry(
            er.async_get(hass), entry.entry_id
        )
    }
    assert not {f"10_{d.key}" for d in HOUSE_SENSORS} & ids


async def test_location_never_appears_in_attributes_or_diagnostics(
    hass: HomeAssistant, located
) -> None:
    """The privacy rule: identifying house data stays in its own sensors."""
    entry = await setup_entry(hass)
    registry = er.async_get(hass)
    for key in ("address", "latitude", "longitude", "time"):
        registry.async_update_entity(
            entity_id(hass, "sensor", f"10_{key}"), disabled_by=None
        )
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    own = {f"10_{key}" for key in ("address", "latitude", "longitude", "time")}
    for item in er.async_entries_for_config_entry(registry, entry.entry_id):
        state = hass.states.get(item.entity_id)
        if state is None:
            continue
        attributes = json.dumps(dict(state.attributes), default=str)
        for private in (ADDRESS, str(LATITUDE), str(LONGITUDE), "2026-01-01T"):
            assert private not in attributes, item.entity_id
        if item.unique_id not in own:
            assert ADDRESS not in state.state, item.entity_id

    diagnostics = json.dumps(
        await async_get_config_entry_diagnostics(hass, entry), default=str
    )
    for private in (ADDRESS, str(LATITUDE), str(LONGITUDE), "Europe/Berlin", "2026-01"):
        assert private not in diagnostics
    assert '"analytics"' in diagnostics

    house_device = house_device_of(hass, entry)
    assert await async_get_device_diagnostics(hass, entry, house_device) == {
        "known": False
    }


async def test_house_device_is_never_offered_for_removal(
    hass: HomeAssistant, located
) -> None:
    """The house device carries no unit serial."""
    from custom_components.ambientika_ventilation import (
        async_remove_config_entry_device,
    )

    entry = await setup_entry(hass)
    device = house_device_of(hass, entry)
    assert not await async_remove_config_entry_device(hass, entry, device)


def test_house_sensor_values_handle_missing_and_odd_data() -> None:
    """Time and time zone helpers cover naive, aware, odd and missing input."""
    device = coordinator_with_status().data.devices[SERIAL].device
    base = replace(device, house_iana_timezone=None, house_timezone=None)
    assert _house_time(base) is None
    assert _house_time_zone(base) is None

    assert _house_time_zone(replace(base, house_timezone=2)) == "UTC+2"
    assert _house_time_zone(replace(base, house_timezone=-5)) == "UTC-5"
    assert _house_time_zone(replace(base, house_iana_timezone="Europe/Berlin")) == (
        "Europe/Berlin"
    )

    aware = _house_time(replace(base, house_current_time="2026-01-01T12:00:00+02:00"))
    assert aware.utcoffset().total_seconds() == 7200
    zoned = _house_time(
        replace(
            base,
            house_current_time="2026-07-01T12:00:00",
            house_iana_timezone="Europe/Berlin",
        )
    )
    assert zoned.utcoffset().total_seconds() == 7200
    assert _house_time(replace(base, house_current_time="2026-01-01T12:00:00")).tzinfo
    assert _house_time(replace(base, house_current_time="not a time")) is None


def test_house_sensor_entity_follows_the_coordinator() -> None:
    """The sensor reads its house from the shared device data."""
    coordinator = coordinator_with_status()
    data = coordinator.data.devices[SERIAL]
    coordinator.data.devices[SERIAL] = replace(
        data, device=replace(data.device, house_id=10, house_address=ADDRESS)
    )
    address = AmbientikaHouseSensor(coordinator, 10, HOUSE_SENSORS[0])
    assert address.unique_id == "10_address"
    assert address.native_value == ADDRESS
    assert address.available
    assert address.extra_state_attributes is None
    latitude = AmbientikaHouseSensor(coordinator, 10, HOUSE_SENSORS[1])
    assert latitude.native_value is None and not latitude.available
    coordinator.data.devices.clear()
    assert address.native_value is None
