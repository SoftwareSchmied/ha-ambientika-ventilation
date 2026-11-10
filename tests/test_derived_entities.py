"""Derived entities: creation per scope, gating by requirements, outdoor feed."""

from __future__ import annotations

import json
from collections import Counter
from typing import Any

import pytest
from custom_components.ambientika_ventilation.analytics_descriptions import (
    BY_KEY,
    DESCRIPTIONS,
)
from custom_components.ambientika_ventilation.const import DOMAIN
from custom_components.ambientika_ventilation.psychrometrics import (
    absolute_humidity,
    dew_point,
)
from homeassistant.const import STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir

from .test_analytics_wiring import use_clock
from .test_local_wiring import entity_id, setup_entry, state_of
from .wiring_helpers import (
    MASTER,
    SECOND,
    fake_listeners,
    mock_api,
    patch_session,
    two_zone_payload,
)

SCOPE_IDS = {MASTER, SECOND, "10", "10_1", "10_2"}


@pytest.fixture
def two_zones(houses_payload, status_payload):
    """One house, two zones with one master each (no Gemini unit)."""
    api = mock_api(two_zone_payload(houses_payload), status_payload)
    with patch_session(api), fake_listeners():
        yield api


def derived_counts(hass: HomeAssistant, entry) -> dict[tuple[str, bool], int]:
    """Return the derived registry entries as {(scope, enabled by default): count}."""
    counts: Counter[tuple[str, bool]] = Counter()
    for item in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id):
        for key, description in BY_KEY.items():
            prefix = item.unique_id.removesuffix(f"_{key}")
            if prefix != item.unique_id and prefix in SCOPE_IDS:
                counts[(description.scope, item.disabled_by is None)] += 1
    return dict(counts)


def unique_ids(hass: HomeAssistant, entry) -> set[str]:
    return {
        item.unique_id
        for item in er.async_entries_for_config_entry(
            er.async_get(hass), entry.entry_id
        )
    }


# --- creation per scope and requirements --------------------------------------


async def test_entities_per_scope_without_outdoor_source(
    hass: HomeAssistant, two_zones
) -> None:
    """2 units, 2 zones, 1 house; outdoor and volume values are not created."""
    entry = await setup_entry(hass)
    assert derived_counts(hass, entry) == {
        ("device", True): 44,
        ("device", False): 100,
        ("zone", True): 10,
        ("zone", False): 2,
        ("house", True): 5,
        ("house", False): 1,
    }
    ids = unique_ids(hass, entry)
    assert f"{MASTER}_airflow" in ids and f"{SECOND}_airflow" in ids
    assert "10_1_zone_airflow" in ids and "10_2_zone_airflow" in ids
    assert "10_house_airflow" in ids
    for gated in (
        "heat_recovered_power",
        "drying_potential",
        "outdoor_humidifies",
        "air_change_rate",
    ):
        assert f"{MASTER}_{gated}" not in ids
    assert "10_outdoor_dew_point" not in ids
    assert "10_1_zone_heat_recovered_power" not in ids


async def test_outdoor_source_adds_the_outdoor_entities(
    hass: HomeAssistant, two_zones
) -> None:
    """With an outdoor source the heat, humidity and outdoor values exist."""
    entry = await setup_entry(hass, {"outdoor_weather": "weather.home"})
    assert derived_counts(hass, entry) == {
        ("device", True): 56,
        ("device", False): 102,
        ("zone", True): 10,
        ("zone", False): 6,
        ("house", True): 7,
        ("house", False): 3,
    }
    ids = unique_ids(hass, entry)
    for key in (
        "heat_recovered_power",
        "heat_recovered_energy",
        "ventilation_loss_power",
        "drying_potential",
        "outdoor_humidifies",
        "moisture_recovered",
    ):
        assert f"{MASTER}_{key}" in ids
    assert "10_outdoor_dew_point" in ids
    assert "10_outdoor_absolute_humidity" in ids
    assert "10_2_zone_heat_recovered_energy" in ids
    assert "10_house_heat_recovered_power" in ids
    # The binary sensor of the outdoor group exists too.
    assert entity_id(hass, "binary_sensor", f"{MASTER}_outdoor_humidifies")


async def test_room_volume_creates_the_air_change_rate_of_that_unit_only(
    hass: HomeAssistant, two_zones
) -> None:
    """The requirement is per unit and keyed by serial."""
    entry = await setup_entry(hass, {"room_volumes": {MASTER: 30.0, "gone": 12.0}})
    ids = unique_ids(hass, entry)
    assert f"{MASTER}_air_change_rate" in ids
    assert f"{SECOND}_air_change_rate" not in ids
    assert entry.runtime_data.analytics.config.room_volumes == {
        MASTER: 30.0,
        "gone": 12.0,
    }


@pytest.mark.parametrize("stored", ["thirty", -4, 0, 5000, None])
async def test_invalid_room_volumes_are_ignored(
    hass: HomeAssistant, two_zones, stored
) -> None:
    entry = await setup_entry(hass, {"room_volumes": {MASTER: stored}})
    assert f"{MASTER}_air_change_rate" not in unique_ids(hass, entry)


async def test_malformed_room_volume_option_is_ignored(
    hass: HomeAssistant, two_zones
) -> None:
    entry = await setup_entry(hass, {"room_volumes": "30"})
    assert entry.runtime_data.analytics.config.room_volumes == {}


async def test_local_mode_off_skips_the_udp_dependent_entities(
    hass: HomeAssistant, two_zones
) -> None:
    """No direction values, local variants or local-only hub sensors."""
    entry = await setup_entry(hass, {"local_udp": "off"})
    ids = unique_ids(hass, entry)
    for key in (
        "runtime_direction_exhaust",
        "reversals",
        "half_cycle_duration",
        "smart_behaviour",
        "free_cooling_active",
        "night_reduction_active",
        "direction",
        "physical_stage",
        "coupling",
        "heartbeat_age",
        "running_local",
        "power_local",
        "last_seen_local",
    ):
        assert f"{MASTER}_{key}" not in ids, key
    for key in (
        "airflow",
        "power",
        "running",
        "running_cloud",
        "last_seen",
        "data_source",
    ):
        assert f"{MASTER}_{key}" in ids, key
    assert derived_counts(hass, entry)[("device", True)] == 2 * 19


async def test_unit_zone_and_house_entities_use_the_right_devices(
    hass: HomeAssistant, two_zones
) -> None:
    """Zone values sit on the zone master's device, house values on the house."""
    await setup_entry(hass)
    registry = er.async_get(hass)
    devices = dr.async_get(hass)

    def device_of(unique_id: str, domain: str = "sensor"):
        entry = registry.async_get(entity_id(hass, domain, unique_id))
        return devices.async_get(entry.device_id)

    assert device_of(f"{MASTER}_airflow").identifiers == {(DOMAIN, MASTER)}
    assert device_of(f"{SECOND}_airflow").identifiers == {(DOMAIN, SECOND)}
    assert device_of("10_1_zone_airflow").identifiers == {(DOMAIN, MASTER)}
    assert device_of("10_2_zone_airflow").identifiers == {(DOMAIN, SECOND)}
    house = device_of("10_house_airflow")
    assert house.identifiers == {(DOMAIN, "house_10")}
    assert house.name == "Home"
    assert device_of(f"{MASTER}_night_reduction_active", "binary_sensor")


async def test_house_id_is_not_exposed_in_names_or_attributes(
    hass: HomeAssistant, two_zones
) -> None:
    """Unique ids may carry the house id; names and attributes never do."""
    entry = await setup_entry(hass, {"outdoor_weather": "weather.home"})
    registry = er.async_get(hass)
    checked = 0
    for item in er.async_entries_for_config_entry(registry, entry.entry_id):
        if item.unique_id.removesuffix(f"_{item.unique_id.split('_', 1)[-1]}") not in (
            SCOPE_IDS
        ):
            continue
        state = hass.states.get(item.entity_id)
        if state is None:
            continue
        checked += 1
        text = json.dumps(dict(state.attributes), default=str)
        assert "10" not in state.attributes["friendly_name"], item.entity_id
        assert '"10"' not in text and ": 10," not in text, item.entity_id
        assert MASTER not in text and SECOND not in text, item.entity_id
    assert checked > 50


# --- state publishing ------------------------------------------------------------


async def test_estimates_are_marked_and_helpers_are_not_recorded(
    hass: HomeAssistant, two_zones
) -> None:
    """The estimate attribute is set; helper attributes stay out of the recorder."""
    await setup_entry(hass)
    power = state_of(hass, "sensor", f"{MASTER}_power_estimate")
    assert power.attributes["estimate"] is True
    assert power.attributes["unit_of_measurement"] == "W"
    assert power.attributes["state_class"] == "measurement"
    assert "estimate" not in state_of(hass, "sensor", f"{MASTER}_dew_point").attributes
    temperature = state_of(hass, "sensor", f"{MASTER}_room_temperature")
    assert "measurement_context" in temperature.attributes
    from custom_components.ambientika_ventilation.derived_entity import (
        AmbientikaDerivedEntity,
    )

    assert {"measurement_context", "air_quality_raw"} <= (
        AmbientikaDerivedEntity._unrecorded_attributes
    )


async def test_enum_and_total_entities_report_known_values(
    hass: HomeAssistant, two_zones
) -> None:
    """Enum states are options; the totals follow the running units."""
    await setup_entry(hass)
    comfort = state_of(hass, "sensor", f"{MASTER}_comfort")
    # Without a settled measurement the comfort is unknown, never invented.
    assert comfort.state in (*comfort.attributes["options"], STATE_UNKNOWN)
    assert state_of(hass, "sensor", "10_house_running_units").state == "2"
    assert state_of(hass, "sensor", "10_1_zone_running_units").state == "1"
    assert state_of(hass, "sensor", f"{MASTER}_filter_alarm_predicted") is not None
    assert state_of(hass, "binary_sensor", f"{MASTER}_measurement_valid") is not None


async def test_unknown_values_are_reported_as_unknown(
    hass: HomeAssistant, two_zones
) -> None:
    """Values the engine cannot compute are unknown, not invented."""
    await setup_entry(hass)
    assert state_of(hass, "sensor", f"{MASTER}_smart_behaviour").state == STATE_UNKNOWN


# --- outdoor feed -------------------------------------------------------------------


def weather_attributes(**overrides: Any) -> dict[str, Any]:
    return {
        "temperature": 10.0,
        "humidity": 80,
        "temperature_unit": "\N{DEGREE SIGN}C",
        **overrides,
    }


async def test_outdoor_values_follow_a_weather_entity(
    hass: HomeAssistant, two_zones
) -> None:
    """Dew point and absolute humidity come from the weather entity."""
    hass.states.async_set("weather.home", "sunny", weather_attributes())
    entry = await setup_entry(hass, {"outdoor_weather": "weather.home"})
    clock = use_clock(entry.runtime_data)
    entry.runtime_data.analytics.refresh()
    await hass.async_block_till_done()

    assert float(
        state_of(hass, "sensor", "10_outdoor_dew_point").state
    ) == pytest.approx(dew_point(10.0, 80), abs=0.1)
    assert float(
        state_of(hass, "sensor", "10_outdoor_absolute_humidity").state
    ) == pytest.approx(absolute_humidity(10.0, 80), abs=0.1)

    clock.now += 120
    hass.states.async_set("weather.home", "rainy", weather_attributes(temperature=14.0))
    await hass.async_block_till_done()
    assert float(
        state_of(hass, "sensor", "10_outdoor_dew_point").state
    ) == pytest.approx(dew_point(14.0, 80), abs=0.1)


@pytest.mark.parametrize(
    ("value", "unit"),
    [(50.0, "\N{DEGREE SIGN}F"), (283.15, "K"), (10.0, "\N{DEGREE SIGN}C")],
)
async def test_outdoor_temperature_units_are_converted(
    hass: HomeAssistant, two_zones, value, unit
) -> None:
    """Fahrenheit and Kelvin readings give the same dew point as 10 deg C."""
    hass.states.async_set(
        "weather.home",
        "sunny",
        weather_attributes(temperature=value, temperature_unit=unit),
    )
    entry = await setup_entry(hass, {"outdoor_weather": "weather.home"})
    assert float(
        state_of(hass, "sensor", "10_outdoor_dew_point").state
    ) == pytest.approx(dew_point(10.0, 80), abs=0.1)
    assert entry.runtime_data.analytics.weather.current(
        entry.runtime_data.analytics._clock()
    ).temperature == pytest.approx(10.0)


async def test_outdoor_values_follow_separate_sensors(
    hass: HomeAssistant, two_zones
) -> None:
    """A temperature and a humidity sensor feed the same values."""
    hass.states.async_set(
        "sensor.out_t", "50", {"unit_of_measurement": "\N{DEGREE SIGN}F"}
    )
    hass.states.async_set("sensor.out_h", "70", {"unit_of_measurement": "%"})
    entry = await setup_entry(
        hass,
        {"outdoor_temperature": "sensor.out_t", "outdoor_humidity": "sensor.out_h"},
    )
    clock = use_clock(entry.runtime_data)
    assert float(
        state_of(hass, "sensor", "10_outdoor_dew_point").state
    ) == pytest.approx(dew_point(10.0, 70), abs=0.1)

    clock.now += 120
    hass.states.async_set("sensor.out_h", "90", {"unit_of_measurement": "%"})
    await hass.async_block_till_done()
    assert float(
        state_of(hass, "sensor", "10_outdoor_dew_point").state
    ) == pytest.approx(dew_point(10.0, 90), abs=0.1)


async def test_sensor_temperature_wins_over_the_weather_entity(
    hass: HomeAssistant, two_zones
) -> None:
    """Sensors replace the matching value of the weather entity."""
    hass.states.async_set("weather.home", "sunny", weather_attributes())
    hass.states.async_set(
        "sensor.out_t", "20", {"unit_of_measurement": "\N{DEGREE SIGN}C"}
    )
    entry = await setup_entry(
        hass, {"outdoor_weather": "weather.home", "outdoor_temperature": "sensor.out_t"}
    )
    outdoor = entry.runtime_data.analytics.weather.current(
        entry.runtime_data.analytics._clock()
    )
    assert outdoor.temperature == pytest.approx(20.0)
    assert outdoor.humidity == pytest.approx(80.0)


@pytest.mark.parametrize("state", ["unavailable", "unknown", "none", ""])
async def test_unavailable_outdoor_sources_clear_the_values(
    hass: HomeAssistant, two_zones, state
) -> None:
    """An unusable source leaves the outdoor values unknown."""
    hass.states.async_set("weather.home", "sunny", weather_attributes())
    entry = await setup_entry(hass, {"outdoor_weather": "weather.home"})
    clock = use_clock(entry.runtime_data)
    assert state_of(hass, "sensor", "10_outdoor_dew_point").state != STATE_UNKNOWN

    clock.now += 120
    hass.states.async_set("weather.home", state, weather_attributes())
    await hass.async_block_till_done()
    assert state_of(hass, "sensor", "10_outdoor_dew_point").state == STATE_UNKNOWN
    assert entry.runtime_data.analytics.diagnostics()["outdoor_reading"] is False


async def test_missing_or_bad_outdoor_readings_are_tolerated(
    hass: HomeAssistant, two_zones
) -> None:
    """No entity, no temperature or an implausible value never raises."""
    entry = await setup_entry(hass, {"outdoor_weather": "weather.home"})
    analytics = entry.runtime_data.analytics
    assert analytics.weather.current(analytics._clock()) is None

    hass.states.async_set("weather.home", "sunny", {"humidity": 50})
    await hass.async_block_till_done()
    assert analytics.weather.current(analytics._clock()) is None

    hass.states.async_set(
        "weather.home", "sunny", weather_attributes(temperature="n/a")
    )
    await hass.async_block_till_done()
    assert analytics.weather.current(analytics._clock()) is None

    # Humidity unusable: the temperature is kept, humidity values stay unknown.
    hass.states.async_set("weather.home", "sunny", weather_attributes(humidity=None))
    await hass.async_block_till_done()
    outdoor = analytics.weather.current(analytics._clock())
    assert outdoor is not None and outdoor.humidity is None


async def test_old_outdoor_readings_expire(hass: HomeAssistant, two_zones) -> None:
    """A reading older than three hours is not used any more."""
    hass.states.async_set("weather.home", "sunny", weather_attributes())
    entry = await setup_entry(hass, {"outdoor_weather": "weather.home"})
    analytics = entry.runtime_data.analytics
    now = analytics._clock()
    assert analytics.weather.current(now) is not None
    assert analytics.weather.current(now + 3 * 3600 + 60) is None


async def test_no_outdoor_source_means_no_outdoor_tracking(
    hass: HomeAssistant, two_zones
) -> None:
    entry = await setup_entry(hass)
    analytics = entry.runtime_data.analytics
    assert analytics.outdoor_entity_ids == []
    assert analytics.outdoor_configured is False
    assert analytics.requirement_met("nonsense") is False
    assert ir.async_get(hass) is not None


def test_derived_sensor_reports_only_known_enum_options() -> None:
    """A value outside the options of an enum sensor is unknown."""
    from unittest.mock import MagicMock

    from custom_components.ambientika_ventilation.analytics_manager import DerivedTarget
    from custom_components.ambientika_ventilation.sensor import AmbientikaDerivedSensor

    analytics = MagicMock()
    analytics.values.return_value = {"comfort": "comfortable"}
    target = DerivedTarget(BY_KEY["comfort"], "SER", "SER_comfort", MagicMock())
    sensor = AmbientikaDerivedSensor(analytics, target)
    assert sensor.native_value == "comfortable"
    assert sensor.device_class == "enum" and sensor.options[0] == "too_dry"
    sensor._on_changed({"other": 1})
    assert sensor.native_value == "comfortable"
    sensor._value = "bogus"
    assert sensor.native_value is None
    assert sensor.extra_state_attributes is None


def test_every_description_builds_a_valid_entity_description() -> None:
    """Units, classes and categories of the table are valid Home Assistant values."""
    from custom_components.ambientika_ventilation.binary_sensor import (
        derived_binary_description,
    )
    from custom_components.ambientika_ventilation.sensor import (
        derived_sensor_description,
    )

    for description in DESCRIPTIONS:
        if description.platform == "sensor":
            built = derived_sensor_description(description)
            assert built.suggested_display_precision == description.precision
            assert built.options == (list(description.options) or None)
        else:
            built = derived_binary_description(description)
        assert built.key == built.translation_key == description.key
        assert built.entity_registry_enabled_default is description.enabled_default
        assert (built.entity_category is not None) == bool(description.entity_category)


def test_entity_documentation_lists_every_derived_entity() -> None:
    """docs/ENTITIES.md names every derived key with its scope pattern."""
    from pathlib import Path

    root = Path(__file__).parents[1]
    text = (root / "docs" / "ENTITIES.md").read_text("utf-8")
    patterns = {
        "device": "<serial>_{key}",
        "zone": "<house>_<zone>_{key}",
        "house": "<house>_{key}",
    }
    for description in DESCRIPTIONS:
        pattern = patterns[description.scope].format(key=description.key)
        assert f"`{pattern}`" in text, description.key
    for key in ("address", "latitude", "longitude", "time_zone", "time"):
        assert f"`<house>_{key}`" in text
    assert "last_local_frame" not in text.replace("former `last_local_frame`", "")
    assert "## Derived data" in (root / "README.md").read_text("utf-8")
