"""Translation parity tests."""

from __future__ import annotations

import json
from pathlib import Path


def _keys(value, prefix="") -> set[str]:
    """Flatten JSON object keys for translation parity checks."""
    if not isinstance(value, dict):
        return {prefix}
    return {
        item
        for key, child in value.items()
        for item in _keys(child, f"{prefix}.{key}" if prefix else key)
    }


def test_english_and_german_translations_are_complete() -> None:
    """Both shipped user interface languages expose identical keys."""
    root = Path(__file__).parents[1] / "custom_components" / "ambientika_ventilation"
    english = json.loads((root / "translations" / "en.json").read_text("utf-8"))
    german = json.loads((root / "translations" / "de.json").read_text("utf-8"))

    assert _keys(english) == _keys(german)


ROOT = Path(__file__).parents[1] / "custom_components" / "ambientika_ventilation"


def _load(name: str) -> dict:
    return json.loads((ROOT / name).read_text("utf-8"))


def test_strings_file_matches_the_english_translation() -> None:
    """strings.json is the source of the English translation."""
    assert _load("strings.json") == _load("translations/en.json")


def _entity_keys() -> dict[str, set[str]]:
    from custom_components.ambientika_ventilation.analytics_descriptions import (
        DESCRIPTIONS,
    )
    from custom_components.ambientika_ventilation.binary_sensor import (
        BINARY_SENSORS,
        HUB_BINARY_SENSORS,
    )
    from custom_components.ambientika_ventilation.sensor import (
        CLOUD_SENSORS,
        DEVICE_SENSORS,
        HOUSE_SENSORS,
        HUB_SENSORS,
        SENSORS,
    )

    sensors = {
        item.translation_key
        for group in (
            SENSORS,
            DEVICE_SENSORS,
            HUB_SENSORS,
            HOUSE_SENSORS,
            CLOUD_SENSORS,
        )
        for item in group
    }
    sensors |= {d.key for d in DESCRIPTIONS if d.platform == "sensor"}
    binaries = {item.translation_key for item in (*BINARY_SENSORS, *HUB_BINARY_SENSORS)}
    binaries |= {d.key for d in DESCRIPTIONS if d.platform == "binary_sensor"}
    return {"sensor": sensors, "binary_sensor": binaries}


def test_every_entity_has_an_english_and_german_name() -> None:
    """All sensors and binary sensors, derived ones included, are named."""
    for language in ("en", "de"):
        entities = _load(f"translations/{language}.json")["entity"]
        for platform, keys in _entity_keys().items():
            for key in keys:
                assert entities[platform][key]["name"], (language, platform, key)


def test_derived_names_follow_the_description_table() -> None:
    """The names of the derived entities come from name_en and name_de."""
    from custom_components.ambientika_ventilation.analytics_descriptions import (
        DESCRIPTIONS,
    )

    english = _load("translations/en.json")["entity"]
    german = _load("translations/de.json")["entity"]
    for description in DESCRIPTIONS:
        assert english[description.platform][description.key]["name"] == (
            description.name_en
        )
        assert german[description.platform][description.key]["name"] == (
            description.name_de
        )


def test_every_enum_option_has_an_english_and_german_state() -> None:
    """Derived, local and data source enums translate each option."""
    from custom_components.ambientika_ventilation.analytics_descriptions import (
        DESCRIPTIONS,
    )
    from custom_components.ambientika_ventilation.sensor import HUB_SENSORS

    options: dict[str, set[str]] = {
        d.key: set(d.options) for d in DESCRIPTIONS if d.options
    }
    options.update(
        {item.key: set(item.options) for item in HUB_SENSORS if item.options}
    )
    assert {"comfort", "smart_behaviour", "damper_state", "measurement_context"} <= set(
        options
    )
    assert {"direction", "physical_stage", "coupling", "data_source"} <= set(options)
    for language in ("en", "de"):
        sensors = _load(f"translations/{language}.json")["entity"]["sensor"]
        for key, states in options.items():
            assert set(sensors[key]["state"]) >= states, (language, key)
            assert all(sensors[key]["state"][state] for state in states)


def test_options_flow_and_selectors_are_translated() -> None:
    """The new option fields, the room volume step and the filter basis."""
    for language in ("en", "de"):
        data = _load(f"translations/{language}.json")
        init = data["options"]["step"]["init"]
        for field in (
            "outdoor_weather",
            "outdoor_temperature",
            "outdoor_humidity",
            "filter_basis",
        ):
            assert init["data"][field] and init["data_description"][field]
        assert data["options"]["step"]["volumes"]["description"]
        assert set(data["selector"]["filter_basis"]["options"]) == {
            "operating",
            "on_time",
        }


def test_icons_exist_for_derived_entities_and_the_calendar() -> None:
    """Every derived entity, the house sensors and the calendar have an icon."""
    from custom_components.ambientika_ventilation.analytics_descriptions import (
        DESCRIPTIONS,
    )

    icons = _load("icons.json")["entity"]
    for description in DESCRIPTIONS:
        entry = icons[description.platform][description.key]
        assert entry["default"].startswith("mdi:")
        assert set(entry.get("state", {})) <= set(description.options)
    for key in (
        "house_address",
        "house_latitude",
        "house_longitude",
        "house_time_zone",
        "house_time",
    ):
        assert icons["sensor"][key]["default"].startswith("mdi:")
    assert icons["calendar"]["schedule"]["default"] == "mdi:calendar-clock"
    assert "last_local_frame" not in icons["sensor"]
