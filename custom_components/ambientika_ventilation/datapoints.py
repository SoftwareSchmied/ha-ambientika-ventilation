"""Datapoint table for the hybrid (cloud + local) data model.

Every value a device can expose is described once by a
:class:`DatapointDescription`: which sources can provide it, how each source is
extracted from the raw inputs, how fresh the source must be and how competing
values are merged. The hub (``hub.py``) evaluates this table; entity platforms
never merge sources themselves.

This module has no Home Assistant dependency.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Final

from .local.models import (
    LOCAL_LOST_AFTER,
    LOCAL_STALE_AFTER,
    Coupling,
    Direction,
    Freshness,
    LocalSpeed,
)
from .models import AmbientikaStatus

LOCAL_FRESH_S: Final = LOCAL_STALE_AFTER
"""A local frame younger than 3 heartbeats (~81 s) is fresh."""
LOCAL_LOST_S: Final = LOCAL_LOST_AFTER
"""No local frame for 5 minutes or more: the local link is lost."""
CLOUD_FRESH_S: Final = 150.0
"""A cloud poll younger than this is fresh (2.5 x the default 60 s interval)."""
EXPECTED_TTL_S: Final = 30.0
"""Default lifetime of an expected (pending command) value."""
HYSTERESIS_FRAMES: Final = 2
"""Local frames required before local replaces cloud."""
CONFLICT_CYCLES: Final = 2
"""Cloud cycles tolerated after a local change before a disagreement counts."""

SLAVE_ROLES: Final = frozenset({"SlaveEqualMaster", "SlaveOppositeMaster"})


class Source(StrEnum):
    """Origin of a datapoint value."""

    CLOUD = "cloud"
    LOCAL_UDP = "local_udp"
    LOCAL_TCP = "local_tcp"  # reserved, no extractor uses it yet
    EXPECTED = "expected"
    CLOUD_HEURISTIC = "cloud_heuristic"


SELECTION_ORDER: Final = (
    Source.LOCAL_TCP,
    Source.LOCAL_UDP,
    Source.EXPECTED,
    Source.CLOUD,
    Source.CLOUD_HEURISTIC,
)
"""Priority of sources: local, then pending command, then cloud."""

LOCAL_SOURCES: Final = frozenset({Source.LOCAL_TCP, Source.LOCAL_UDP})

DEFAULT_TTL: Final[Mapping[Source, float]] = MappingProxyType(
    {
        Source.LOCAL_UDP: LOCAL_FRESH_S,
        Source.LOCAL_TCP: 75.0,
        Source.EXPECTED: EXPECTED_TTL_S,
        Source.CLOUD: CLOUD_FRESH_S,
        Source.CLOUD_HEURISTIC: CLOUD_FRESH_S,
    }
)


class MergePolicy(StrEnum):
    """How the effective value is chosen."""

    PRIORITY = "priority"
    """First usable source in :data:`SELECTION_ORDER` (with local hysteresis)."""
    LATEST = "latest"
    """The most recent timestamp among the sources."""
    DERIVED = "derived"
    """Computed by the hub from device state; no source."""


def derive_direction(
    role: str | None, coupling: Coupling, master_direction: Direction
) -> Direction:
    """Return the airflow direction of a device from the zone master's frame.

    Only the master transmits. A master (or a device of unknown role) reports
    the broadcast direction. A slave follows the zone coupling: opposite runs
    reverse to the master, same runs alike, idle is stopped.
    """
    if role not in SLAVE_ROLES:
        return master_direction
    if coupling is Coupling.IDLE:
        return Direction.STOPPED
    if coupling is Coupling.SAME:
        return master_direction
    if coupling is Coupling.OPPOSITE:
        if master_direction is Direction.EXHAUST:
            return Direction.INTAKE
        if master_direction is Direction.INTAKE:
            return Direction.EXHAUST
        return master_direction
    return Direction.UNKNOWN


@dataclass(frozen=True, slots=True)
class LocalView:
    """Local state of one device (master values or slave derived values)."""

    freshness: Freshness
    age_s: float
    received_wall: datetime | None
    coupling: Coupling
    direction: Direction
    speed: LocalSpeed
    derived: bool
    """True for values derived from the master (slaves)."""

    @property
    def fresh(self) -> bool:
        """Return whether the local link delivers current data."""
        return self.freshness is Freshness.FRESH

    @property
    def running(self) -> bool:
        """Return whether air is physically moving."""
        return self.direction in (Direction.EXHAUST, Direction.INTAKE)


@dataclass(frozen=True, slots=True)
class DatapointInputs:
    """Raw per-device inputs handed to the extractors (built by the hub)."""

    serial: str
    now: float
    status: AmbientikaStatus | None
    """Last cloud status; None when the unit is offline or failed."""
    cloud_age_s: float | None
    cloud_wall: datetime | None
    expected: Mapping[str, Any] = field(default_factory=dict)
    local: LocalView | None = None
    cloud_fresh: bool = False
    data_source: str = "offline"


Extractor = Callable[[DatapointInputs], Any]


@dataclass(frozen=True, slots=True)
class DatapointDescription:
    """Static description of one datapoint."""

    key: str
    extractors: Mapping[Source, Extractor]
    policy: MergePolicy = MergePolicy.PRIORITY
    has_variants: bool = False
    """Whether `_cloud`/`_local` variant entities apply (>= 2 raw sources)."""
    ttl: Mapping[Source, float] = field(default_factory=dict)
    """Per-source freshness override in seconds (see :data:`DEFAULT_TTL`)."""
    stale_ok: frozenset[Source] = frozenset()
    """Sources extracted even when stale (liveness values)."""
    volatile: bool = False
    """Changes on every heartbeat; only availability and source notify."""
    stale_value: Any = None
    """Value reported when no source is usable."""
    derive: Extractor | None = None
    """Extractor for :attr:`MergePolicy.DERIVED`."""

    @property
    def sources(self) -> tuple[Source, ...]:
        """Return the sources of this datapoint in priority order."""
        return tuple(s for s in SELECTION_ORDER if s in self.extractors)

    @property
    def settable(self) -> bool:
        """Return whether pending commands (expected values) apply."""
        return Source.EXPECTED in self.extractors


def _status(attr: str) -> Extractor:
    def extract(inp: DatapointInputs) -> Any:
        return getattr(inp.status, attr) if inp.status is not None else None

    return extract


def _expected(key: str) -> Extractor:
    def extract(inp: DatapointInputs) -> Any:
        return inp.expected.get(key)

    return extract


def _local(attr: str) -> Extractor:
    def extract(inp: DatapointInputs) -> Any:
        return getattr(inp.local, attr) if inp.local is not None else None

    return extract


def _mode_on(mode: object) -> bool | None:
    return None if mode is None else mode != "Off"


def _cloud_power(inp: DatapointInputs) -> bool | None:
    return _mode_on(inp.status.operating_mode if inp.status else None)


def _expected_power(inp: DatapointInputs) -> bool | None:
    return _mode_on(inp.expected.get("operating_mode"))


def _local_power(inp: DatapointInputs) -> bool | None:
    """Claim "on" only for moving air or a non-idle coupling.

    An idle frame makes no claim: in Away mode a running unit idles locally for
    long periods, so "idle" says nothing about the setting. The effective value
    then falls back to the expected or cloud value.
    """
    local = inp.local
    if local is None:
        return None
    if local.running:
        return True
    if local.coupling in (Coupling.OPPOSITE, Coupling.SAME):
        return True
    return None


def _local_stage(inp: DatapointInputs) -> LocalSpeed | None:
    """Report the fan stage only while the unit is running or coupled.

    The unit keeps its last stage in the frame while it is off, so an idle or
    stopped frame with idle coupling reports no running stage.
    """
    local = inp.local
    if local is None:
        return None
    if local.coupling is Coupling.IDLE and not local.running:
        return None
    return local.speed


def _local_running(inp: DatapointInputs) -> bool | None:
    local = inp.local
    if local is None or local.direction is Direction.UNKNOWN:
        return None
    return local.running


def _heartbeat_age(inp: DatapointInputs) -> float | None:
    return round(inp.local.age_s, 1) if inp.local is not None else None


def _local_seen(inp: DatapointInputs) -> datetime | None:
    return inp.local.received_wall if inp.local is not None else None


def _cloud_seen(inp: DatapointInputs) -> datetime | None:
    return inp.cloud_wall


def _local_link(inp: DatapointInputs) -> bool:
    return inp.local is not None and inp.local.fresh


def _cloud_reachable(inp: DatapointInputs) -> bool:
    return inp.cloud_fresh


def _data_source(inp: DatapointInputs) -> str:
    return inp.data_source


def _cloud_only(key: str, attr: str) -> DatapointDescription:
    return DatapointDescription(key, {Source.CLOUD: _status(attr)})


def _setting(key: str, attr: str) -> DatapointDescription:
    return DatapointDescription(
        key, {Source.EXPECTED: _expected(key), Source.CLOUD: _status(attr)}
    )


def _derived(key: str, derive: Extractor) -> DatapointDescription:
    return DatapointDescription(key, {}, policy=MergePolicy.DERIVED, derive=derive)


_TABLE: tuple[DatapointDescription, ...] = (
    # Settings: cloud or pending command only, never UDP (controls follow the
    # setting, not the physical state).
    _setting("operating_mode", "operating_mode"),
    _setting("fan_speed", "fan_speed"),
    _setting("humidity_level", "humidity_level"),
    _setting("light_sensor_level", "light_sensor_level"),
    _cloud_only("last_operating_mode", "last_operating_mode"),
    _cloud_only("schedule_state", "schedule_state"),
    _cloud_only("device_role", "device_role"),
    _cloud_only("turbo_available", "turbo_available"),
    # Measurements, alarms and link quality: cloud only (UDP has none).
    _cloud_only("temperature", "temperature"),
    _cloud_only("humidity", "humidity"),
    _cloud_only("air_quality", "air_quality"),
    _cloud_only("filter_status", "filter_status"),
    _cloud_only("night_alarm", "night_alarm"),
    _cloud_only("humidity_alarm", "humidity_alarm"),
    _cloud_only("signal_strength", "signal_strength"),
    # Overlapping datapoints.
    DatapointDescription(
        "power",
        {
            Source.LOCAL_UDP: _local_power,
            Source.EXPECTED: _expected_power,
            Source.CLOUD: _cloud_power,
        },
        has_variants=True,
    ),
    DatapointDescription(
        "running",
        {Source.LOCAL_UDP: _local_running, Source.CLOUD_HEURISTIC: _cloud_power},
        has_variants=True,
    ),
    DatapointDescription(
        "last_seen",
        {Source.LOCAL_UDP: _local_seen, Source.CLOUD: _cloud_seen},
        policy=MergePolicy.LATEST,
        has_variants=True,
        stale_ok=frozenset({Source.LOCAL_UDP, Source.CLOUD}),
        volatile=True,
    ),
    # Local only.
    DatapointDescription("physical_stage", {Source.LOCAL_UDP: _local_stage}),
    DatapointDescription(
        "direction",
        {Source.LOCAL_UDP: _local("direction")},
        stale_value=Direction.UNKNOWN,
    ),
    DatapointDescription(
        "coupling",
        {Source.LOCAL_UDP: _local("coupling")},
        stale_value=Coupling.UNKNOWN,
    ),
    DatapointDescription(
        "heartbeat_age",
        {Source.LOCAL_UDP: _heartbeat_age},
        stale_ok=frozenset({Source.LOCAL_UDP}),
        volatile=True,
    ),
    # Derived by the hub.
    _derived("local_link", _local_link),
    _derived("cloud_reachable", _cloud_reachable),
    _derived("data_source", _data_source),
)

DATAPOINTS: Final[Mapping[str, DatapointDescription]] = MappingProxyType(
    {description.key: description for description in _TABLE}
)


def variant_keys() -> tuple[str, ...]:
    """Return the keys for which `_cloud`/`_local` variant entities apply."""
    return tuple(key for key, desc in DATAPOINTS.items() if desc.has_variants)
