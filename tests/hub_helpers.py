"""Builders and a fake clock for hub tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from custom_components.ambientika_ventilation.hub import AmbientikaHub
from custom_components.ambientika_ventilation.local.models import (
    Coupling,
    Direction,
    Freshness,
    LocalSpeed,
    LocalZoneState,
    UdpFrame,
)
from custom_components.ambientika_ventilation.models import (
    AmbientikaData,
    AmbientikaDevice,
    AmbientikaDeviceData,
    AmbientikaStatus,
)

HOUSE = 4242
ZONE = 1
MASTER = "AA0000000001"
SLAVE = "AA0000000002"
EPOCH = datetime(2026, 1, 1, tzinfo=UTC)


class FakeClock:
    """Monotonic clock plus matching wall clock."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def wall(self) -> datetime:
        return EPOCH + timedelta(seconds=self.now)

    def advance(self, seconds: float) -> float:
        self.now += seconds
        return self.now


def make_hub(
    clock: FakeClock | None = None, **kwargs: float
) -> tuple[AmbientikaHub, FakeClock]:
    clock = clock or FakeClock()
    return AmbientikaHub(clock=clock, wall_clock=clock.wall, **kwargs), clock


def device(
    serial: str = MASTER,
    *,
    role: str | None = "Master",
    house: int | None = HOUSE,
    zone: int | None = ZONE,
    status: AmbientikaStatus | None = None,
    no_status: bool = False,
    mode: str = "ManualHeatRecovery",
) -> AmbientikaDeviceData:
    if status is None and not no_status:
        status = AmbientikaStatus(
            serial_number=serial,
            operating_mode=mode,
            fan_speed="Medium",
            temperature=21,
            humidity=45,
        )
    return AmbientikaDeviceData(
        device=AmbientikaDevice(
            serial_number=serial,
            name=serial,
            role=role,
            house_id=house,
            zone_index=zone,
            device_type="Ghost",
        ),
        status=status,
    )


def cloud(
    *devices: AmbientikaDeviceData, failed: frozenset[str] = frozenset()
) -> AmbientikaData:
    return AmbientikaData(
        devices={d.device.serial_number: d for d in devices}, failed_devices=failed
    )


def frame(
    *,
    coupling: Coupling = Coupling.OPPOSITE,
    direction: Direction = Direction.EXHAUST,
    speed: LocalSpeed = LocalSpeed.MEDIUM,
    house: int = HOUSE,
    zone: int = ZONE,
) -> UdpFrame:
    return UdpFrame(
        zone_index=zone,
        house_id=house,
        coupling=coupling,
        coupling_raw=0,
        direction=direction,
        speed=speed,
        state_raw=0,
        raw=b"",
        source_host="192.0.2.1",
        source_port=46000 + zone,
    )


def local(
    clock: FakeClock,
    *,
    n: int = 1,
    at: float | None = None,
    freshness: Freshness = Freshness.FRESH,
    with_frame: bool = True,
    house: int = HOUSE,
    zone: int = ZONE,
    coupling: Coupling = Coupling.OPPOSITE,
    direction: Direction = Direction.EXHAUST,
    speed: LocalSpeed = LocalSpeed.MEDIUM,
) -> LocalZoneState:
    received = clock.now if at is None else at
    return LocalZoneState(
        zone_index=zone,
        house_id=house,
        frame=(
            frame(
                house=house,
                zone=zone,
                coupling=coupling,
                direction=direction,
                speed=speed,
            )
            if with_frame
            else None
        ),
        freshness=freshness,
        received_at=received if with_frame else None,
        received_wall=clock.wall() if with_frame else None,
        last_change_at=received if with_frame else None,
        frames_total=n,
        heartbeats=0,
        changes=0,
        reversals=0,
        rejected=0,
    )


def runtime(coordinator, hub: AmbientikaHub | None = None):
    """Return runtime data shaped like the real one, with a mocked local manager."""
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    local_manager = MagicMock()
    local_manager.diagnostics.return_value = {"mode": "auto"}
    if hub is None:
        hub = AmbientikaHub()
        if isinstance(getattr(coordinator, "data", None), AmbientikaData):
            hub.update_cloud(coordinator.data)
    return SimpleNamespace(
        coordinator=coordinator,
        hub=hub,
        local=local_manager,
        raw_variants=False,
    )


def fake_entry(coordinator, hub: AmbientikaHub | None = None):
    """Return a config entry stand-in for calling platform setup functions."""
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    return SimpleNamespace(
        entry_id="entry1",
        runtime_data=runtime(coordinator, hub),
        async_on_unload=MagicMock(),
    )
