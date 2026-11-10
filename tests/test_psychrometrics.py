"""Tests for the psychrometric helpers (hand-computed reference values)."""

from __future__ import annotations

import pytest

from custom_components.ambientika_ventilation import psychrometrics as psy


def test_saturation_pressure_at_20c() -> None:
    # 6.112 * exp(17.62 * 20 / 263.12) = 23.33 hPa
    assert psy.saturation_vapor_pressure(20.0) == pytest.approx(23.33, abs=0.02)


def test_dew_point_20c_50pct() -> None:
    # g = ln(0.5) + 1.33931 = 0.64616; Td = 243.12 * g / (17.62 - g) = 9.255
    assert psy.dew_point(20.0, 50.0) == pytest.approx(9.26, abs=0.02)


def test_dew_point_at_saturation_equals_temperature() -> None:
    assert psy.dew_point(15.0, 100.0) == pytest.approx(15.0, abs=1e-9)


def test_absolute_humidity_20c_50pct() -> None:
    # 216.7 * 0.5 * 23.33 / 293.15 = 8.62 g/m3
    assert psy.absolute_humidity(20.0, 50.0) == pytest.approx(8.62, abs=0.02)


def test_absolute_humidity_zero_rh_is_zero() -> None:
    assert psy.absolute_humidity(20.0, 0.0) == 0.0


@pytest.mark.parametrize(
    ("temperature", "humidity"),
    [(None, 50.0), (20.0, None), (-100.0, 50.0), (20.0, 120.0), (20.0, -1.0)],
)
def test_invalid_inputs(temperature: float | None, humidity: float | None) -> None:
    assert psy.dew_point(temperature, humidity) is None
    assert psy.absolute_humidity(temperature, humidity) is None


def test_dew_point_needs_positive_humidity() -> None:
    assert psy.dew_point(20.0, 0.0) is None


def test_sensible_power() -> None:
    # 0.335 Wh/(m3 K) * 40 m3/h * 20 K = 268 W
    assert psy.sensible_power(40.0, 20.0) == pytest.approx(268.0)
