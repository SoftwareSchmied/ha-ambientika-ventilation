"""Tests for source selection, hysteresis, expected values and notifications."""

from __future__ import annotations

import logging

import pytest
from custom_components.ambientika_ventilation.datapoints import Source
from custom_components.ambientika_ventilation.hub import (
    EVALUATE_INTERVAL_S,
    ResolvedValue,
)
from custom_components.ambientika_ventilation.local.models import (
    Coupling,
    Direction,
    Freshness,
    LocalSpeed,
)

from .hub_helpers import (
    HOUSE,
    MASTER,
    SLAVE,
    FakeClock,
    cloud,
    device,
    local,
    make_hub,
)


def start(mode: str = "ManualHeatRecovery"):
    hub, clock = make_hub()
    hub.update_cloud(cloud(device(mode=mode)))
    return hub, clock


def engage(hub, clock, **kwargs) -> int:
    """Feed two frames so local replaces cloud; return the next frame number."""
    hub.update_local(local(clock, n=1, **kwargs))
    clock.advance(27)
    hub.update_local(local(clock, n=2, **kwargs))
    return 3


def test_evaluate_interval() -> None:
    assert EVALUATE_INTERVAL_S == 10


def test_cloud_only_values_and_heuristic() -> None:
    hub, _clock = start()
    power = hub.resolve(MASTER, "power")
    assert (power.value, power.source) == (True, Source.CLOUD)
    assert power.variants == {
        Source.LOCAL_UDP: None,
        Source.EXPECTED: None,
        Source.CLOUD: True,
    }
    running = hub.resolve(MASTER, "running")
    assert (running.value, running.source) == (True, Source.CLOUD_HEURISTIC)
    assert hub.resolve(MASTER, "temperature").value == 21
    assert hub.resolve(MASTER, "fan_speed").value == "Medium"
    direction = hub.resolve(MASTER, "direction")
    assert direction.value is Direction.UNKNOWN
    assert direction.source is None
    assert hub.resolve(MASTER, "physical_stage").value is None
    assert hub.resolve(MASTER, "local_link").value is False
    assert hub.resolve(MASTER, "cloud_reachable").value is True
    assert hub.data_source(MASTER) == "cloud"
    assert hub.cloud_received


def test_hysteresis_cloud_to_local_after_two_frames() -> None:
    hub, clock = start("Off")
    seen: list[ResolvedValue] = []
    hub.subscribe(MASTER, "power", seen.append)
    hub.update_local(local(clock, n=1))
    # One frame: cloud is still effective, local variant is already visible.
    power = hub.resolve(MASTER, "power")
    assert (power.value, power.source) == (False, Source.CLOUD)
    assert power.variants[Source.LOCAL_UDP] is True
    assert hub.data_source(MASTER) == "cloud"
    # Local-only values do not wait for a second frame (nothing to switch from).
    assert hub.resolve(MASTER, "direction").value is Direction.EXHAUST
    clock.advance(27)
    hub.update_local(local(clock, n=2))
    power = hub.resolve(MASTER, "power")
    assert (power.value, power.source) == (True, Source.LOCAL_UDP)
    assert power.age_s == 0
    assert hub.data_source(MASTER) == "local_udp"
    assert [(r.value, r.source) for r in seen] == [
        (False, Source.CLOUD),  # variant change from the first frame
        (True, Source.LOCAL_UDP),
    ]


def test_idle_local_frame_makes_no_power_claim() -> None:
    """Away mode idles locally for long periods while the unit is on."""
    hub, clock = start("AwayHome")
    engage(hub, clock, coupling=Coupling.IDLE, direction=Direction.STOPPED)
    power = hub.resolve(MASTER, "power")
    assert (power.value, power.source) == (True, Source.CLOUD)
    assert power.variants[Source.LOCAL_UDP] is None
    assert hub.data_source(MASTER) == "local_udp"
    # The idle frame still tells the physical state.
    assert hub.resolve(MASTER, "running").value is False
    assert hub.resolve(MASTER, "physical_stage").value is None
    hub.set_expected(MASTER, {"operating_mode": "Off"})
    assert hub.resolve(MASTER, "power").source is Source.EXPECTED


def test_repeated_state_object_does_not_count_as_frame() -> None:
    hub, clock = start()
    state = local(clock, n=1)
    hub.update_local(state)
    clock.advance(1)
    hub.update_local(state)
    assert not hub.liveness(MASTER).local_engaged  # type: ignore[union-attr]


def test_local_to_cloud_immediately_when_stale() -> None:
    hub, clock = start()
    engage(hub, clock)
    seen: list[ResolvedValue] = []
    hub.subscribe(MASTER, "power", seen.append)
    assert hub.resolve(MASTER, "power").source is Source.LOCAL_UDP
    clock.advance(60)
    hub.update_cloud(cloud(device()))  # cloud stays fresh
    clock.advance(20)  # 80 s since last frame: still fresh
    hub.evaluate()
    assert hub.resolve(MASTER, "power").source is Source.LOCAL_UDP
    clock.advance(2)  # 82 s: three heartbeats missed
    hub.evaluate()
    power = hub.resolve(MASTER, "power")
    assert (power.value, power.source) == (True, Source.CLOUD)
    assert hub.resolve(MASTER, "local_link").value is False
    assert hub.data_source(MASTER) == "degraded"
    assert seen[-1].source is Source.CLOUD
    # Local-only values fall back to unknown.
    assert hub.resolve(MASTER, "direction").value is Direction.UNKNOWN


def test_streak_resets_after_gap_so_cloud_returns_first() -> None:
    hub, clock = start()
    n = engage(hub, clock)
    clock.advance(150)
    hub.update_cloud(cloud(device()))
    hub.update_local(local(clock, n=n))  # gap >= 81 s without evaluate
    assert hub.resolve(MASTER, "power").source is Source.CLOUD
    clock.advance(27)
    hub.update_local(local(clock, n=n + 1))
    assert hub.resolve(MASTER, "power").source is Source.LOCAL_UDP


def test_streak_resets_by_evaluate() -> None:
    hub, clock = start()
    n = engage(hub, clock)
    clock.advance(100)
    hub.evaluate()
    hub.update_cloud(cloud(device()))
    hub.update_local(local(clock, n=n))
    assert hub.resolve(MASTER, "power").source is Source.CLOUD
    clock.advance(27)
    hub.update_local(local(clock, n=n + 1))
    assert hub.resolve(MASTER, "power").source is Source.LOCAL_UDP


def test_local_used_at_once_when_cloud_has_nothing() -> None:
    hub, clock = make_hub()
    hub.update_cloud(cloud(device(no_status=True)))
    assert hub.data_source(MASTER) == "offline"
    hub.update_local(local(clock, n=1))
    power = hub.resolve(MASTER, "power")
    assert (power.value, power.source) == (True, Source.LOCAL_UDP)
    assert hub.data_source(MASTER) == "degraded"


def test_tracker_snapshot_freshness_is_respected() -> None:
    hub, clock = start()
    hub.update_local(local(clock, n=1, freshness=Freshness.STALE))
    assert hub.liveness(MASTER).local_freshness is Freshness.STALE  # type: ignore[union-attr]
    assert hub.resolve(MASTER, "local_link").value is False


def test_local_freshness_classes() -> None:
    hub, clock = start()
    hub.update_local(local(clock, n=1))
    assert hub.liveness(MASTER).local_freshness is Freshness.FRESH  # type: ignore[union-attr]
    clock.advance(200)
    assert hub.liveness(MASTER).local_freshness is Freshness.STALE  # type: ignore[union-attr]
    clock.advance(100)
    assert hub.liveness(MASTER).local_freshness is Freshness.LOST  # type: ignore[union-attr]
    hub.update_local(local(clock, with_frame=False))
    liveness = hub.liveness(MASTER)
    assert liveness is not None
    assert liveness.local_freshness is Freshness.NEVER
    assert liveness.local_age_s is None
    assert liveness.local_last_seen is None


def test_settings_ignore_udp() -> None:
    hub, clock = start()
    engage(hub, clock, coupling=Coupling.IDLE, direction=Direction.STOPPED)
    # Physically idle, yet the setting stays what the cloud says (E6).
    assert hub.resolve(MASTER, "operating_mode").value == "ManualHeatRecovery"
    assert hub.resolve(MASTER, "operating_mode").source is Source.CLOUD
    assert hub.resolve(MASTER, "fan_speed").value == "Medium"
    # UDP stage and cloud setting are separate, non-conflicting datapoints; an
    # idle unit reports no running stage.
    assert hub.resolve(MASTER, "physical_stage").value is None
    engage_more = local(clock, n=9, coupling=Coupling.OPPOSITE)
    hub.update_local(engage_more)
    assert hub.resolve(MASTER, "physical_stage").value is LocalSpeed.MEDIUM
    assert hub.liveness(MASTER).conflicts == 0  # type: ignore[union-attr]


def test_expected_overrides_cloud_until_ttl() -> None:
    hub, clock = start()
    seen: list[ResolvedValue] = []
    hub.subscribe(MASTER, "operating_mode", seen.append)
    accepted = hub.set_expected(
        MASTER, {"operating_mode": "Off", "bogus": 1, "fan_speed": None}
    )
    assert accepted == ("operating_mode",)
    mode = hub.resolve(MASTER, "operating_mode")
    assert (mode.value, mode.source) == ("Off", Source.EXPECTED)
    assert hub.resolve(MASTER, "power").value is False
    assert hub.resolve(MASTER, "power").source is Source.EXPECTED
    clock.advance(10)
    assert hub.resolve(MASTER, "operating_mode").age_s == 10
    clock.advance(10)
    hub.evaluate()
    assert hub.resolve(MASTER, "operating_mode").source is Source.EXPECTED
    clock.advance(11)  # 31 s after the command
    assert hub.resolve(MASTER, "operating_mode").source is Source.CLOUD
    hub.evaluate()
    assert [(r.value, r.source) for r in seen] == [
        ("Off", Source.EXPECTED),
        ("ManualHeatRecovery", Source.CLOUD),
    ]


def test_expected_custom_ttl_and_local_wins_over_it() -> None:
    hub, clock = start()
    hub.set_expected(MASTER, {"operating_mode": "Off"}, ttl=5)
    clock.advance(6)
    assert hub.resolve(MASTER, "operating_mode").source is Source.CLOUD
    hub.set_expected(MASTER, {"operating_mode": "Off"}, at=clock.now)
    engage(hub, clock)
    # Selection order is local -> expected -> cloud.
    power = hub.resolve(MASTER, "power")
    assert power.source is Source.LOCAL_UDP
    assert power.variants[Source.EXPECTED] is False


def test_expected_cleared_by_cloud_confirmation() -> None:
    hub, clock = start()
    hub.set_expected(MASTER, {"operating_mode": "Off", "fan_speed": "High"})
    clock.advance(5)
    hub.update_cloud(cloud(device(mode="Off")))
    # operating_mode confirmed and cleared; fan_speed still pending.
    assert hub.resolve(MASTER, "operating_mode").source is Source.CLOUD
    assert hub.resolve(MASTER, "fan_speed").source is Source.EXPECTED
    assert hub.resolve(MASTER, "fan_speed").value == "High"


def test_clear_expected() -> None:
    hub, _clock = start()
    seen: list[ResolvedValue] = []
    hub.subscribe(MASTER, "fan_speed", seen.append)
    hub.set_expected(MASTER, {"operating_mode": "Off", "fan_speed": "High"})
    hub.clear_expected(MASTER, ["fan_speed", "missing"])
    assert hub.resolve(MASTER, "fan_speed").source is Source.CLOUD
    assert hub.resolve(MASTER, "operating_mode").source is Source.EXPECTED
    hub.clear_expected(MASTER)
    assert hub.resolve(MASTER, "operating_mode").source is Source.CLOUD
    assert [r.source for r in seen] == [Source.EXPECTED, Source.CLOUD]
    assert hub.set_expected(MASTER, {"bogus": 1}) == ()
    # Unknown devices are ignored.
    assert hub.set_expected("nope", {"operating_mode": "Off"}) == ()
    hub.clear_expected("nope")


def test_notifications_only_on_change() -> None:
    hub, clock = start()
    counts: dict[str, int] = {}

    def watch(key: str) -> None:
        counts[key] = 0

        def bump(_resolved: ResolvedValue) -> None:
            counts[key] += 1

        hub.subscribe(MASTER, key, bump)

    keys = (
        "power",
        "running",
        "direction",
        "physical_stage",
        "coupling",
        "heartbeat_age",
        "last_seen",
        "local_link",
        "data_source",
        "temperature",
    )
    for key in keys:
        watch(key)
    n = engage(hub, clock)
    after_engage = dict(counts)
    assert after_engage["direction"] == 1
    assert after_engage["power"] >= 1
    assert after_engage["data_source"] == 1
    # Heartbeats with identical content change nothing observable.
    for i in range(5):
        clock.advance(27)
        hub.update_local(local(clock, n=n + i))
        hub.update_cloud(cloud(device()))
        hub.evaluate()
    assert counts == after_engage
    # A real change notifies exactly the affected datapoints.
    clock.advance(27)
    hub.update_local(local(clock, n=n + 5, direction=Direction.INTAKE))
    assert counts["direction"] == after_engage["direction"] + 1
    assert counts["power"] == after_engage["power"]
    assert counts["physical_stage"] == after_engage["physical_stage"]
    # Cloud change notifies the cloud-backed datapoint only.
    hub.update_cloud(cloud(device(status=_status(temperature=19))))
    assert counts["temperature"] == after_engage["temperature"] + 1


def _status(**kwargs):
    from custom_components.ambientika_ventilation.models import AmbientikaStatus

    base = {"operating_mode": "ManualHeatRecovery", "fan_speed": "Medium"}
    base.update(kwargs)
    return AmbientikaStatus(serial_number=MASTER, **base)


def test_cloud_variant_change_notifies_even_if_effective_is_local() -> None:
    hub, clock = start()
    engage(hub, clock)
    seen: list[ResolvedValue] = []
    hub.subscribe(MASTER, "power", seen.append)
    clock.advance(1)
    hub.update_cloud(cloud(device(mode="Off")))
    assert len(seen) == 1
    assert seen[0].source is Source.LOCAL_UDP
    assert seen[0].variants[Source.CLOUD] is False


def test_volatile_notifies_on_availability_and_source_only() -> None:
    hub, clock = start()
    ages: list[ResolvedValue] = []
    seen: list[ResolvedValue] = []
    hub.subscribe(MASTER, "heartbeat_age", ages.append)
    hub.subscribe(MASTER, "last_seen", seen.append)
    assert len(seen) == 0
    hub.update_local(local(clock, n=1))
    assert len(ages) == 1  # became available
    assert seen[-1].source is Source.LOCAL_UDP
    before = (len(ages), len(seen))
    clock.advance(30)
    hub.update_local(local(clock, n=2))
    clock.advance(100)
    hub.evaluate()  # stale, still reports the age
    assert hub.resolve(MASTER, "heartbeat_age").value == 100.0
    assert (len(ages), len(seen)) == before


def test_last_seen_picks_latest_source() -> None:
    hub, clock = start()
    hub.update_local(local(clock, n=1))
    clock.advance(30)
    hub.update_cloud(cloud(device()))
    seen = hub.resolve(MASTER, "last_seen")
    assert seen.source is Source.CLOUD
    assert seen.value == clock.wall()
    assert seen.age_s == 0
    clock.advance(30)
    hub.update_local(local(clock, n=2))
    seen = hub.resolve(MASTER, "last_seen")
    assert seen.source is Source.LOCAL_UDP
    assert set(seen.variants) == {Source.LOCAL_UDP, Source.CLOUD}
    # Still available while both sources are stale.
    clock.advance(1000)
    assert hub.resolve(MASTER, "last_seen").value is not None


def test_last_seen_unavailable_without_data() -> None:
    hub, _clock = make_hub()
    hub.update_cloud(cloud(device(no_status=True)))
    seen = hub.resolve(MASTER, "last_seen")
    assert seen.value is None
    assert not seen.available
    assert seen.source is None


def test_cloud_outage_with_local_flowing() -> None:
    hub, clock = start()
    n = engage(hub, clock)
    for i in range(6):  # 180 s of local heartbeats without a cloud poll
        clock.advance(30)
        hub.update_local(local(clock, n=n + i))
        hub.evaluate()
    liveness = hub.liveness(MASTER)
    assert liveness is not None
    assert liveness.data_source == "degraded"
    assert not liveness.cloud_reachable
    assert liveness.local_engaged
    assert hub.resolve(MASTER, "power").source is Source.LOCAL_UDP
    assert hub.resolve(MASTER, "temperature").value is None  # cloud value expired
    assert hub.resolve(MASTER, "cloud_reachable").value is False
    assert not hub.cloud_reachable()
    # The cloud comes back.
    hub.update_cloud(cloud(device()))
    assert hub.data_source(MASTER) == "local_udp"
    assert hub.resolve(MASTER, "temperature").value == 21
    assert hub.cloud_reachable()


def test_cloud_failed_device_is_offline_but_local_still_works() -> None:
    hub, clock = make_hub()
    hub.update_cloud(cloud(device(), failed=frozenset({MASTER})))
    assert hub.resolve(MASTER, "temperature").value is None
    assert hub.data_source(MASTER) == "offline"
    assert hub.cloud_reachable()  # the poll itself worked
    hub.update_local(local(clock, n=1))
    assert hub.data_source(MASTER) == "degraded"
    assert hub.resolve(MASTER, "power").source is Source.LOCAL_UDP
    # Recovery.
    hub.update_cloud(cloud(device()))
    assert hub.resolve(MASTER, "temperature").value == 21


def test_cloud_ttl_is_configurable() -> None:
    hub, clock = make_hub(cloud_ttl_s=30)
    hub.update_cloud(cloud(device()))
    clock.advance(29)
    assert hub.resolve(MASTER, "temperature").value == 21
    clock.advance(2)
    assert hub.resolve(MASTER, "temperature").value is None


def test_slave_direction_is_derived_from_coupling() -> None:
    hub, clock = make_hub()
    hub.update_cloud(cloud(device(MASTER), device(SLAVE, role="SlaveOppositeMaster")))
    assert hub.update_local(local(clock, n=1)) == (MASTER, SLAVE)
    assert hub.resolve(MASTER, "direction").value is Direction.EXHAUST
    slave = hub.resolve(SLAVE, "direction")
    assert slave.value is Direction.INTAKE
    assert hub.resolve(SLAVE, "physical_stage").value is LocalSpeed.MEDIUM
    assert hub.resolve(SLAVE, "running").value is True
    assert hub.devices_in_zone(HOUSE, 1) == (MASTER, SLAVE)

    hub.update_cloud(cloud(device(MASTER), device(SLAVE, role="SlaveEqualMaster")))
    clock.advance(1)
    hub.update_local(
        local(clock, n=2, coupling=Coupling.SAME, direction=Direction.INTAKE)
    )
    assert hub.resolve(SLAVE, "direction").value is Direction.INTAKE
    assert hub.resolve(SLAVE, "coupling").value is Coupling.SAME

    clock.advance(1)
    hub.update_local(
        local(clock, n=3, coupling=Coupling.IDLE, direction=Direction.EXHAUST)
    )
    assert hub.resolve(SLAVE, "direction").value is Direction.STOPPED
    assert hub.resolve(SLAVE, "power").source is Source.CLOUD
    assert hub.resolve(SLAVE, "running").value is False
    assert hub.resolve(SLAVE, "physical_stage").value is None


def test_not_configured_device_has_no_local_view() -> None:
    hub, clock = make_hub()
    hub.update_cloud(cloud(device(MASTER, role="NotConfigured")))
    assert hub.update_local(local(clock, n=1)) == (MASTER,)
    assert hub.resolve(MASTER, "direction").value is Direction.UNKNOWN
    assert hub.resolve(MASTER, "local_link").value is False


def test_unknown_zone_and_device_without_zone() -> None:
    hub, clock = make_hub()
    hub.update_cloud(cloud(device(MASTER, zone=None), device(SLAVE, house=None)))
    assert hub.update_local(local(clock, n=1)) == ()
    assert hub.unmapped_zones() == {(HOUSE, 1)}
    assert hub.resolve(MASTER, "direction").value is Direction.UNKNOWN
    assert hub.resolve(SLAVE, "direction").value is Direction.UNKNOWN
    # The zone appears later and the stored frames apply.
    hub.update_cloud(cloud(device(MASTER)))
    assert hub.unmapped_zones() == frozenset()
    assert hub.resolve(MASTER, "direction").value is Direction.EXHAUST


def test_conflict_counted_after_more_than_two_cloud_cycles() -> None:
    hub, clock = make_hub()
    hub.update_cloud(cloud(device(mode="Off")))
    n = engage(hub, clock)  # local power True, cloud power False
    assert hub.liveness(MASTER).conflicts == 0  # type: ignore[union-attr]

    def cycle(mode: str, **local_kwargs) -> None:
        nonlocal n
        clock.advance(30)
        hub.update_local(local(clock, n=n, **local_kwargs))
        n += 1
        hub.update_cloud(cloud(device(mode=mode)))

    cycle("Off")
    cycle("Off")
    assert hub.liveness(MASTER).conflicts == 0  # type: ignore[union-attr]
    cycle("Off")  # third cycle: the cloud still disagrees
    liveness = hub.liveness(MASTER)
    assert liveness is not None
    assert (liveness.conflicts, liveness.in_conflict) == (1, True)
    cycle("Off")
    assert hub.liveness(MASTER).conflicts == 1  # type: ignore[union-attr]  # one episode
    cycle("ManualHeatRecovery")  # cloud caught up
    liveness = hub.liveness(MASTER)
    assert liveness is not None
    assert (liveness.conflicts, liveness.in_conflict) == (1, False)
    # An idle frame makes no power claim, so it never disagrees with the cloud.
    for _ in range(4):
        cycle("Off", coupling=Coupling.IDLE, direction=Direction.STOPPED)
    liveness = hub.liveness(MASTER)
    assert liveness is not None
    assert (liveness.conflicts, liveness.in_conflict) == (1, False)


def test_no_conflict_without_local_or_cloud_value() -> None:
    hub, clock = make_hub()
    hub.update_cloud(cloud(device(no_status=True)))
    engage(hub, clock)
    for _ in range(5):
        clock.advance(30)
        hub.update_cloud(cloud(device(no_status=True)))
    assert hub.liveness(MASTER).conflicts == 0  # type: ignore[union-attr]


def test_subscribe_unsubscribe_lifecycle() -> None:
    hub, clock = make_hub()
    seen: list[ResolvedValue] = []
    unsubscribe = hub.subscribe(MASTER, "temperature", seen.append)
    assert seen == []
    hub.update_cloud(cloud(device()))
    assert [r.value for r in seen] == [21]
    unsubscribe()
    unsubscribe()  # idempotent
    hub.update_cloud(cloud(device(status=_status(temperature=5))))
    assert len(seen) == 1
    # Re-subscribing seeds the baseline from the current value.
    other: list[ResolvedValue] = []
    stay = hub.subscribe(MASTER, "temperature", other.append)
    second: list[ResolvedValue] = []
    drop = hub.subscribe(MASTER, "temperature", second.append)
    drop()
    clock.advance(1)
    hub.update_cloud(cloud(device(status=_status(temperature=6))))
    assert [r.value for r in other] == [6]
    assert second == []
    stay()
    other_key = hub.subscribe(MASTER, "humidity", seen.append)
    keep = hub.subscribe(MASTER, "air_quality", seen.append)
    other_key()
    keep()
    with pytest.raises(KeyError):
        hub.subscribe(MASTER, "nope", seen.append)


def test_failing_subscriber_does_not_break_others(
    caplog: pytest.LogCaptureFixture,
) -> None:
    hub, _clock = make_hub()
    seen: list[ResolvedValue] = []

    def boom(_resolved: ResolvedValue) -> None:
        raise RuntimeError("boom")

    hub.subscribe(MASTER, "temperature", boom)
    hub.subscribe(MASTER, "temperature", seen.append)
    with caplog.at_level(logging.ERROR):
        hub.update_cloud(cloud(device()))
    assert len(seen) == 1
    assert "Subscriber callback failed" in caplog.text


def test_resolve_unknown_serial_and_key() -> None:
    hub, _clock = start()
    unknown = hub.resolve("missing", "power")
    assert unknown.value is None
    assert unknown.source is None
    assert unknown.variants == {}
    assert hub.liveness("missing") is None
    assert hub.data_source("missing") == "offline"
    with pytest.raises(KeyError):
        hub.resolve(MASTER, "nope")


def test_fake_clock_default_uses_monotonic() -> None:
    from custom_components.ambientika_ventilation.hub import AmbientikaHub

    hub = AmbientikaHub()
    hub.update_cloud(cloud(device()))
    assert hub.resolve(MASTER, "power").value is True
    assert isinstance(FakeClock().wall().year, int)
