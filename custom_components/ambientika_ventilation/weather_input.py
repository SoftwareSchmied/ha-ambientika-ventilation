"""Outdoor conditions input for the derived-data engine (F13).

The lead (Home Assistant glue) reads a user-selected weather or sensor entity
and passes plain numbers in; this module validates them, converts units and
derives the outdoor dew point, absolute humidity, the drying potential and the
"outdoor air would humidify" warning. The vendor's embedded weather key is
never used. No Home Assistant dependency.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Final

from .psychrometrics import absolute_humidity, dew_point, plausible

DEFAULT_MAX_AGE_S: Final = 3 * 3600.0
"""Outdoor readings older than this are ignored (weather entities update slowly)."""
DEFAULT_HUMIDIFY_MARGIN: Final = 0.5
"""g/m3; below the AH error caused by the 1 deg C temperature resolution."""

_UNKNOWN_STATES: Final = frozenset({"", "unknown", "unavailable", "none"})


def parse_number(raw: Any) -> float | None:
    """Return a finite float from a number or numeric string, else None."""
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, str):
        text = raw.strip().replace(",", ".")
        if text.lower() in _UNKNOWN_STATES:
            return None
        raw = text
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def to_celsius(value: Any, unit: str | None = "C") -> float | None:
    """Convert a temperature in deg C, deg F or K to deg C (None if invalid)."""
    number = parse_number(value)
    if number is None:
        return None
    normalized = (unit or "C").strip().upper().replace("\N{DEGREE SIGN}", "")
    if normalized == "F":
        return (number - 32.0) * 5.0 / 9.0
    if normalized == "K":
        return number - 273.15
    return number


@dataclass(frozen=True, slots=True)
class OutdoorConditions:
    """Validated outdoor temperature (deg C) and optional relative humidity (%)."""

    temperature: float
    humidity: float | None = None

    @property
    def dew_point(self) -> float | None:
        """Return the outdoor dew point in deg C."""
        return dew_point(self.temperature, self.humidity)

    @property
    def absolute_humidity(self) -> float | None:
        """Return the outdoor absolute humidity in g/m3."""
        return absolute_humidity(self.temperature, self.humidity)


def drying_potential(
    indoor_absolute_humidity: float | None, outdoor: OutdoorConditions | None
) -> float | None:
    """Return indoor minus outdoor absolute humidity in g/m3.

    Positive: outdoor air is drier, ventilation dries the room.
    """
    if indoor_absolute_humidity is None or outdoor is None:
        return None
    outdoor_ah = outdoor.absolute_humidity
    if outdoor_ah is None:
        return None
    return indoor_absolute_humidity - outdoor_ah


def would_humidify(
    potential: float | None, margin: float = DEFAULT_HUMIDIFY_MARGIN
) -> bool | None:
    """Return True when outdoor air is wetter than indoor air by ``margin`` g/m3."""
    if potential is None:
        return None
    return potential <= -margin


class WeatherInput:
    """Holds the latest outdoor reading and drops it when it gets too old."""

    def __init__(self, max_age_s: float = DEFAULT_MAX_AGE_S) -> None:
        """Create an empty input."""
        self._max_age_s = max_age_s
        self._conditions: OutdoorConditions | None = None
        self._updated: float | None = None

    def set(
        self,
        temperature: Any,
        humidity: Any,
        now: float,
        *,
        temperature_unit: str | None = "C",
    ) -> OutdoorConditions | None:
        """Store a reading (plain numbers or numeric strings) and return it.

        An unusable temperature clears the input. An unusable humidity keeps the
        temperature and leaves the humidity-based values unavailable.
        """
        temp = to_celsius(temperature, temperature_unit)
        if temp is None or not plausible(temp, 0.0):
            self.clear()
            return None
        hum = parse_number(humidity)
        if hum is not None and not plausible(temp, hum):
            hum = None
        self._conditions = OutdoorConditions(temp, hum)
        self._updated = now
        return self._conditions

    def clear(self) -> None:
        """Forget the reading (entity removed or option cleared)."""
        self._conditions = None
        self._updated = None

    def current(self, now: float) -> OutdoorConditions | None:
        """Return the reading, or None when missing or older than the max age."""
        if self._conditions is None or self._updated is None:
            return None
        if now - self._updated > self._max_age_s:
            return None
        return self._conditions
