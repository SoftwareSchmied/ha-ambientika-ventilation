"""Tests for the datapoint table."""

from __future__ import annotations

import pytest
from custom_components.ambientika_ventilation.datapoints import (
    DATAPOINTS,
    DatapointInputs,
    LocalView,
    MergePolicy,
    Source,
    derive_direction,
    variant_keys,
)
from custom_components.ambientika_ventilation.local.models import (
    Coupling,
    Direction,
    Freshness,
    LocalSpeed,
)
from custom_components.ambientika_ventilation.models import AmbientikaStatus

CLOUD_FIELDS = {
    "operating_mode",
    "fan_speed",
    "humidity_level",
    "light_sensor_level",
    "last_operating_mode",
    "schedule_state",
    "device_role",
    "turbo_available",
    "temperature",
    "humidity",
    "air_quality",
    "filter_status",
    "night_alarm",
    "humidity_alarm",
    "signal_strength",
}


def view(
    coupling: Coupling = Coupling.OPPOSITE,
    direction: Direction = Direction.EXHAUST,
    freshness: Freshness = Freshness.FRESH,
) -> LocalView:
    return LocalView(
        freshness, 5.04, None, coupling, direction, LocalSpeed.LOW, derived=False
    )


def inputs(**kwargs: object) -> DatapointInputs:
    base: dict[str, object] = {
        "serial": "X",
        "now": 0.0,
        "status": None,
        "cloud_age_s": None,
        "cloud_wall": None,
    }
    base.update(kwargs)
    return DatapointInputs(**base)  # type: ignore[arg-type]


def test_table_covers_every_cloud_field() -> None:
    for key in CLOUD_FIELDS:
        desc = DATAPOINTS[key]
        assert Source.CLOUD in desc.extractors
        assert Source.LOCAL_UDP not in desc.extractors
        assert not desc.has_variants


def test_settings_follow_cloud_or_expected_never_udp() -> None:
    for key in ("operating_mode", "fan_speed", "humidity_level", "light_sensor_level"):
        desc = DATAPOINTS[key]
        assert desc.sources == (Source.EXPECTED, Source.CLOUD)
        assert desc.settable
    assert not DATAPOINTS["temperature"].settable


def test_overlap_and_local_only_sources() -> None:
    assert DATAPOINTS["power"].sources == (
        Source.LOCAL_UDP,
        Source.EXPECTED,
        Source.CLOUD,
    )
    assert DATAPOINTS["running"].sources == (
        Source.LOCAL_UDP,
        Source.CLOUD_HEURISTIC,
    )
    assert DATAPOINTS["last_seen"].policy is MergePolicy.LATEST
    for key in ("physical_stage", "direction", "coupling", "heartbeat_age"):
        assert DATAPOINTS[key].sources == (Source.LOCAL_UDP,)
        assert not DATAPOINTS[key].has_variants
    for key in ("local_link", "cloud_reachable", "data_source"):
        assert DATAPOINTS[key].policy is MergePolicy.DERIVED
    assert variant_keys() == ("power", "running", "last_seen")


def test_cloud_extractors() -> None:
    status = AmbientikaStatus(
        "X", operating_mode="Off", temperature=0, night_alarm=False
    )
    inp = inputs(status=status)
    assert DATAPOINTS["power"].extractors[Source.CLOUD](inp) is False
    assert DATAPOINTS["running"].extractors[Source.CLOUD_HEURISTIC](inp) is False
    assert DATAPOINTS["temperature"].extractors[Source.CLOUD](inp) == 0
    assert DATAPOINTS["night_alarm"].extractors[Source.CLOUD](inp) is False
    assert DATAPOINTS["fan_speed"].extractors[Source.CLOUD](inp) is None
    empty = inputs()
    assert DATAPOINTS["power"].extractors[Source.CLOUD](empty) is None
    assert DATAPOINTS["temperature"].extractors[Source.CLOUD](empty) is None
    on = inputs(status=AmbientikaStatus("X", operating_mode="Smart"))
    assert DATAPOINTS["power"].extractors[Source.CLOUD](on) is True


def test_expected_extractors() -> None:
    inp = inputs(expected={"operating_mode": "Off", "fan_speed": "High"})
    assert DATAPOINTS["fan_speed"].extractors[Source.EXPECTED](inp) == "High"
    assert DATAPOINTS["power"].extractors[Source.EXPECTED](inp) is False
    assert DATAPOINTS["power"].extractors[Source.EXPECTED](inputs()) is None


@pytest.mark.parametrize(
    ("coupling", "direction", "expected"),
    [
        (Coupling.IDLE, Direction.STOPPED, None),
        (Coupling.UNKNOWN, Direction.UNKNOWN, None),
        (Coupling.OPPOSITE, Direction.STOPPED, True),
        (Coupling.SAME, Direction.STOPPED, True),
        (Coupling.UNKNOWN, Direction.STOPPED, None),
        (Coupling.IDLE, Direction.EXHAUST, True),
        (Coupling.UNKNOWN, Direction.INTAKE, True),
    ],
)
def test_local_power(
    coupling: Coupling, direction: Direction, expected: bool | None
) -> None:
    extract = DATAPOINTS["power"].extractors[Source.LOCAL_UDP]
    assert extract(inputs(local=view(coupling, direction))) is expected
    assert extract(inputs()) is None


def test_local_running_and_physical_values() -> None:
    running = DATAPOINTS["running"].extractors[Source.LOCAL_UDP]
    assert running(inputs(local=view(direction=Direction.INTAKE))) is True
    assert running(inputs(local=view(direction=Direction.STOPPED))) is False
    assert running(inputs(local=view(direction=Direction.UNKNOWN))) is None
    assert running(inputs()) is None
    stage = DATAPOINTS["physical_stage"].extractors[Source.LOCAL_UDP]
    assert stage(inputs(local=view())) is LocalSpeed.LOW
    # The unit keeps its last stage in the frame while it is off.
    assert stage(inputs(local=view(Coupling.IDLE, Direction.STOPPED))) is None
    assert stage(inputs(local=view(Coupling.IDLE, Direction.UNKNOWN))) is None
    assert stage(inputs(local=view(Coupling.IDLE, Direction.EXHAUST))) is LocalSpeed.LOW
    assert stage(inputs(local=view(Coupling.OPPOSITE, Direction.STOPPED))) is (
        LocalSpeed.LOW
    )
    assert stage(inputs()) is None
    assert DATAPOINTS["direction"].stale_value is Direction.UNKNOWN
    assert DATAPOINTS["coupling"].stale_value is Coupling.UNKNOWN


def test_liveness_extractors() -> None:
    age = DATAPOINTS["heartbeat_age"].extractors[Source.LOCAL_UDP]
    assert age(inputs(local=view())) == 5.0
    assert age(inputs()) is None
    seen = DATAPOINTS["last_seen"]
    assert seen.extractors[Source.LOCAL_UDP](inputs()) is None
    assert seen.extractors[Source.CLOUD](inputs()) is None
    link = DATAPOINTS["local_link"].derive
    assert link is not None
    assert link(inputs()) is False
    assert link(inputs(local=view())) is True
    assert link(inputs(local=view(freshness=Freshness.STALE))) is False
    reachable = DATAPOINTS["cloud_reachable"].derive
    assert reachable is not None
    assert reachable(inputs(cloud_fresh=True)) is True
    source = DATAPOINTS["data_source"].derive
    assert source is not None
    assert source(inputs(data_source="cloud")) == "cloud"


@pytest.mark.parametrize(
    ("role", "coupling", "master", "expected"),
    [
        ("Master", Coupling.OPPOSITE, Direction.EXHAUST, Direction.EXHAUST),
        (None, Coupling.OPPOSITE, Direction.INTAKE, Direction.INTAKE),
        (
            "SlaveOppositeMaster",
            Coupling.OPPOSITE,
            Direction.EXHAUST,
            Direction.INTAKE,
        ),
        (
            "SlaveOppositeMaster",
            Coupling.OPPOSITE,
            Direction.INTAKE,
            Direction.EXHAUST,
        ),
        (
            "SlaveOppositeMaster",
            Coupling.OPPOSITE,
            Direction.STOPPED,
            Direction.STOPPED,
        ),
        ("SlaveEqualMaster", Coupling.SAME, Direction.EXHAUST, Direction.EXHAUST),
        ("SlaveEqualMaster", Coupling.SAME, Direction.INTAKE, Direction.INTAKE),
        ("SlaveEqualMaster", Coupling.IDLE, Direction.EXHAUST, Direction.STOPPED),
        ("SlaveEqualMaster", Coupling.UNKNOWN, Direction.EXHAUST, Direction.UNKNOWN),
    ],
)
def test_derive_direction(
    role: str | None, coupling: Coupling, master: Direction, expected: Direction
) -> None:
    assert derive_direction(role, coupling, master) is expected


def test_local_view_flags() -> None:
    assert view().fresh
    assert view().running
    assert not view(freshness=Freshness.LOST).fresh
    assert not view(direction=Direction.STOPPED).running
