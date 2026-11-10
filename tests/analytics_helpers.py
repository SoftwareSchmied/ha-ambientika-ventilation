"""Shared helpers for the analytics tests (fake clocks, snapshot builders)."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any

from custom_components.ambientika_ventilation.analytics import (
    AnalyticsEngine,
    Derived,
    Snapshot,
)
from custom_components.ambientika_ventilation.weather_input import OutdoorConditions

WALL0 = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)


@dataclass
class Clock:
    """Fake monotonic and wall clock."""

    mono: float = 1000.0
    wall: datetime = field(default_factory=lambda: WALL0)

    def advance(self, seconds: float) -> None:
        self.mono += seconds
        self.wall += timedelta(seconds=seconds)


def snap(**kwargs: Any) -> Snapshot:
    """Return a running heat-recovery snapshot (low stage, exhaust) with overrides."""
    base = Snapshot(
        power=True,
        running=True,
        physical_stage="low",
        direction="exhaust",
        coupling="opposite",
        operating_mode="ManualHeatRecovery",
        fan_speed="Low",
        humidity_level="Normal",
        temperature=21,
        humidity=50,
        air_quality="Good",
        filter_status="Good",
        night_alarm=False,
        humidity_alarm=False,
        zone="z1",
    )
    return replace(base, **kwargs)


def step(
    engine: AnalyticsEngine,
    clock: Clock,
    snapshot: Snapshot,
    *,
    serial: str = "S1",
    outdoor: OutdoorConditions | None = None,
) -> Derived:
    return engine.update(serial, snapshot, clock.mono, clock.wall, outdoor)


def run(
    engine: AnalyticsEngine,
    clock: Clock,
    snapshot: Snapshot,
    seconds: float,
    *,
    every: float = 60.0,
    serial: str = "S1",
    outdoor: OutdoorConditions | None = None,
) -> Derived:
    """Update now, then every ``every`` seconds until ``seconds`` have passed."""
    result = step(engine, clock, snapshot, serial=serial, outdoor=outdoor)
    elapsed = 0.0
    while elapsed < seconds - 1e-9:
        delta = min(every, seconds - elapsed)
        clock.advance(delta)
        elapsed += delta
        result = step(engine, clock, snapshot, serial=serial, outdoor=outdoor)
    return result
