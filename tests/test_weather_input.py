"""Tests for the outdoor input (F13)."""

from __future__ import annotations

from typing import Any

import pytest

from custom_components.ambientika_ventilation.weather_input import (
    OutdoorConditions,
    WeatherInput,
    drying_potential,
    parse_number,
    to_celsius,
    would_humidify,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (5, 5.0),
        ("5,5", 5.5),
        (" 7 ", 7.0),
        (None, None),
        (True, None),
        ("unknown", None),
        ("unavailable", None),
        ("", None),
        ("abc", None),
        (float("nan"), None),
        (float("inf"), None),
        ([1], None),
    ],
)
def test_parse_number(raw: Any, expected: float | None) -> None:
    assert parse_number(raw) == expected


def test_to_celsius() -> None:
    assert to_celsius(50, "\N{DEGREE SIGN}F") == pytest.approx(10.0)
    assert to_celsius(283.15, "K") == pytest.approx(10.0)
    assert to_celsius("12", "\N{DEGREE SIGN}C") == 12.0
    assert to_celsius(3, None) == 3.0
    assert to_celsius("x", "C") is None


def test_outdoor_values_and_drying_potential() -> None:
    outdoor = OutdoorConditions(5.0, 80.0)
    # AH(5 C, 80 %) = 216.7 * 0.8 * 8.72 / 278.15 = 5.43 g/m3
    assert outdoor.absolute_humidity == pytest.approx(5.43, abs=0.03)
    assert outdoor.dew_point == pytest.approx(1.9, abs=0.1)
    assert drying_potential(8.6, outdoor) == pytest.approx(8.6 - 5.43, abs=0.03)
    assert drying_potential(None, outdoor) is None
    assert drying_potential(8.0, None) is None
    assert drying_potential(8.0, OutdoorConditions(5.0)) is None


def test_would_humidify() -> None:
    assert would_humidify(None) is None
    assert would_humidify(-0.5) is True
    assert would_humidify(-0.4) is False
    assert would_humidify(3.0) is False
    assert would_humidify(-1.0, margin=2.0) is False


def test_weather_input_lifecycle() -> None:
    weather = WeatherInput(max_age_s=100.0)
    assert weather.current(0.0) is None
    assert weather.set("4", "90", 10.0) == OutdoorConditions(4.0, 90.0)
    assert weather.current(100.0) == OutdoorConditions(4.0, 90.0)
    assert weather.current(111.0) is None
    # unusable humidity keeps the temperature
    assert weather.set(40, "bad", 0.0, temperature_unit="F") == OutdoorConditions(
        pytest.approx(4.444, abs=0.01), None
    )
    # implausible humidity is dropped
    cond = weather.set(4, 150, 0.0)
    assert cond is not None and cond.humidity is None
    # unusable temperature clears the input
    assert weather.set("unknown", 50, 0.0) is None
    assert weather.current(0.0) is None
    weather.set(1, 50, 0.0)
    weather.clear()
    assert weather.current(0.0) is None
