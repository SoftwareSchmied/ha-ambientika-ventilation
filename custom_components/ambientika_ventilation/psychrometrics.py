"""Psychrometric helpers for the derived-data engine.

Pure functions without Home Assistant dependency. Formulas (documented in
docs/DERIVED_DATA.md):

* Saturation vapour pressure (Magnus, Sonntag 1990):
  ``e_s = 6.112 hPa * exp(17.62 T / (243.12 + T))``
* Dew point: ``Td = 243.12 g / (17.62 - g)`` with
  ``g = ln(RH/100) + 17.62 T / (243.12 + T)``
* Absolute humidity: ``AH = 216.7 * (RH/100 * e_s) / (273.15 + T)`` in g/m3
* Sensible heat of an air flow: ``P = 0.335 Wh/(m3 K) * q * dT`` with
  ``0.335 = 1.2 kg/m3 * 1005 J/(kg K) / 3600``.
"""

from __future__ import annotations

import math
from typing import Final

MAGNUS_A: Final = 17.62
MAGNUS_B: Final = 243.12
SATURATION_PRESSURE_0C_HPA: Final = 6.112
ABSOLUTE_HUMIDITY_FACTOR: Final = 216.7
KELVIN_OFFSET: Final = 273.15
VOLUMETRIC_HEAT_CAPACITY: Final = 0.335
"""Heat capacity of air in Wh per m3 and K (density 1.2 kg/m3, cp 1005 J/kgK)."""

MIN_TEMPERATURE_C: Final = -60.0
MAX_TEMPERATURE_C: Final = 80.0


def plausible(temperature: float | None, humidity: float | None) -> bool:
    """Return whether a temperature (deg C) and relative humidity (%) are usable."""
    return (
        temperature is not None
        and humidity is not None
        and MIN_TEMPERATURE_C <= temperature <= MAX_TEMPERATURE_C
        and 0.0 <= humidity <= 100.0
    )


def saturation_vapor_pressure(temperature: float) -> float:
    """Return the saturation vapour pressure over water in hPa."""
    return SATURATION_PRESSURE_0C_HPA * math.exp(
        MAGNUS_A * temperature / (MAGNUS_B + temperature)
    )


def dew_point(temperature: float | None, humidity: float | None) -> float | None:
    """Return the dew point in deg C, or None for unusable input or RH of 0."""
    if temperature is None or humidity is None:
        return None
    if not plausible(temperature, humidity) or humidity <= 0.0:
        return None
    gamma = math.log(humidity / 100.0) + MAGNUS_A * temperature / (
        MAGNUS_B + temperature
    )
    return MAGNUS_B * gamma / (MAGNUS_A - gamma)


def absolute_humidity(
    temperature: float | None, humidity: float | None
) -> float | None:
    """Return the absolute humidity in g/m3, or None for unusable input."""
    if temperature is None or humidity is None:
        return None
    if not plausible(temperature, humidity):
        return None
    vapor = humidity / 100.0 * saturation_vapor_pressure(temperature)
    return ABSOLUTE_HUMIDITY_FACTOR * vapor / (KELVIN_OFFSET + temperature)


def sensible_power(flow_m3h: float, delta_t: float) -> float:
    """Return the heat flow in W of ``flow_m3h`` cooled/heated by ``delta_t`` K."""
    return VOLUMETRIC_HEAT_CAPACITY * flow_m3h * delta_t
