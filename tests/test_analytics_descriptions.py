"""Consistency tests for the derived-data description table."""

from __future__ import annotations

import pytest
from custom_components.ambientika_ventilation import const
from custom_components.ambientika_ventilation.analytics_descriptions import (
    AIR_QUALITY_INDEX,
    BY_KEY,
    DESCRIPTIONS,
    MODE_SLUGS,
    RAW_DESCRIPTIONS,
    SCOPE_KEYS,
    descriptions_for,
)


def test_keys_unique_and_named() -> None:
    keys = [d.key for d in DESCRIPTIONS]
    assert len(keys) == len(set(keys))
    for desc in DESCRIPTIONS:
        assert desc.name_en and desc.name_de, desc.key


def test_recorder_budget_is_declared() -> None:
    for desc in DESCRIPTIONS:
        if desc.state_class == "total_increasing":
            assert desc.min_interval_s >= 60.0, desc.key
            assert desc.min_change > 0, desc.key
        if desc.state_class is not None:
            assert desc.recorded, desc.key
        if not desc.recorded:
            assert desc.state_class is None, desc.key
            if desc.device_class != "timestamp" and desc.key != "humidity_setpoint":
                assert not desc.enabled_default, desc.key


def test_numeric_descriptions_have_precision() -> None:
    for desc in DESCRIPTIONS:
        numeric = desc.unit is not None or desc.state_class is not None
        if numeric and desc.device_class != "timestamp":
            assert desc.precision is not None, desc.key


def test_enum_options_and_attributes() -> None:
    for desc in DESCRIPTIONS:
        assert (desc.device_class == "enum") == bool(desc.options), desc.key
        for attribute in desc.attributes:
            assert attribute in BY_KEY or attribute == "air_quality_raw"


def test_power_energy_estimates_are_marked() -> None:
    for key in ("power_estimate", "energy_estimate", "airflow", "air_volume"):
        assert BY_KEY[key].estimate
    assert BY_KEY["energy_estimate"].state_class == "total_increasing"
    assert BY_KEY["energy_estimate"].device_class == "energy"


def test_scopes() -> None:
    assert set(SCOPE_KEYS) == {"device", "zone", "house"}
    assert descriptions_for("house") == tuple(
        d for d in DESCRIPTIONS if d.scope == "house"
    )
    assert "zone_power" in SCOPE_KEYS["zone"]
    assert "house_energy" in SCOPE_KEYS["house"]


def test_modes_match_integration() -> None:
    for mode in const.REPORTED_OPERATING_MODES:
        assert mode in MODE_SLUGS
        assert f"runtime_mode_{MODE_SLUGS[mode]}" in BY_KEY
    assert set(AIR_QUALITY_INDEX) == set(const.AIR_QUALITY_LEVELS)
    assert sorted(AIR_QUALITY_INDEX.values()) == [1, 2, 3, 4, 5]


def test_raw_descriptions_are_not_entities() -> None:
    assert RAW_DESCRIPTIONS
    for raw in RAW_DESCRIPTIONS:
        assert not raw.entity and not raw.recorded
        assert raw.key not in BY_KEY
    assert RAW_DESCRIPTIONS[0].key == "udp_state_histogram"


@pytest.mark.parametrize("scope", ["device", "zone", "house"])
def test_scope_not_empty(scope: str) -> None:
    assert SCOPE_KEYS[scope]
