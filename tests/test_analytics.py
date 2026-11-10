"""Tests for the derived-data engine (hand-computed formulas, fake clocks)."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import timedelta
from enum import StrEnum
from typing import Any

import pytest

from custom_components.ambientika_ventilation.analytics import (
    SNAPSHOT_KEYS,
    AnalyticsConfig,
    AnalyticsEngine,
    Snapshot,
    comfort_class,
    effective_stage,
    is_cyclic,
    snapshot_from_values,
    ventilation_phase,
)
from custom_components.ambientika_ventilation.analytics_descriptions import (
    BY_KEY,
    SCOPE_KEYS,
)
from custom_components.ambientika_ventilation.weather_input import OutdoorConditions

from .analytics_helpers import WALL0, Clock, run, snap, step


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def engine() -> AnalyticsEngine:
    return AnalyticsEngine()


def settled_engine(**kwargs: Any) -> AnalyticsEngine:
    return AnalyticsEngine(AnalyticsConfig(settle_s=0.0, **kwargs))


def val(out: Any, key: str) -> Any:
    return out.values[key]


STOPPED = {"power": False, "running": False, "direction": None, "coupling": None}

# ------------------------------------------------------------------ helpers


class _Dir(StrEnum):
    EXHAUST = "exhaust"
    UNKNOWN = "unknown"


def test_snapshot_from_values_normalises() -> None:
    values: dict[str, Any] = {
        "power": 1,
        "running": None,
        "physical_stage": "low",
        "direction": _Dir.EXHAUST,
        "coupling": _Dir.UNKNOWN,
        "operating_mode": "Auto",
        "temperature": 21,
        "humidity": "bad",
        "night_alarm": False,
        "air_quality": "unavailable",
    }
    result = snapshot_from_values(values, zone="z", reversals=3)
    assert result.power is True and result.running is None
    assert result.direction == "exhaust" and result.coupling is None
    assert result.temperature == 21.0 and result.humidity is None
    assert result.night_alarm is False and result.air_quality is None
    assert result.zone == "z" and result.reversals == 3
    assert "power" in SNAPSHOT_KEYS and "humidity_alarm" in SNAPSHOT_KEYS
    assert snapshot_from_values({"temperature": float("nan")}).temperature is None
    assert snapshot_from_values({"temperature": True}).temperature is None


def test_effective_stage() -> None:
    assert effective_stage(Snapshot(physical_stage="high", fan_speed="Low")) == "high"
    night = Snapshot(operating_mode="Night", fan_speed="High")
    assert effective_stage(night) == "night"
    assert effective_stage(Snapshot(fan_speed="Medium")) == "medium"
    assert effective_stage(Snapshot(fan_speed="Turbo")) == "turbo"
    assert effective_stage(Snapshot(fan_speed="Weird")) is None
    assert effective_stage(Snapshot()) is None


def test_is_cyclic() -> None:
    assert is_cyclic(Snapshot(coupling="opposite")) is True
    assert is_cyclic(Snapshot(coupling="same", operating_mode="Night")) is False
    assert is_cyclic(Snapshot(coupling="idle", operating_mode="Night")) is False
    assert is_cyclic(Snapshot(operating_mode="ManualHeatRecovery")) is True
    assert is_cyclic(Snapshot(operating_mode="Auto")) is True
    assert is_cyclic(Snapshot(operating_mode="Auto", humidity_alarm=True)) is False
    assert is_cyclic(Snapshot(operating_mode="Expulsion")) is False
    assert is_cyclic(Snapshot()) is False


def test_ventilation_phase() -> None:
    assert ventilation_phase(Snapshot(direction="intake")) == "intake"
    idle = Snapshot(direction="stopped", coupling="idle")
    assert ventilation_phase(idle) == "stopped"
    assert ventilation_phase(Snapshot(direction="stopped", power=False)) == "stopped"
    reversing = Snapshot(direction="stopped", coupling="opposite")
    assert ventilation_phase(reversing) == "reversing"
    assert ventilation_phase(Snapshot(running=True)) == "running"
    assert ventilation_phase(Snapshot(running=False, power=True)) == "stopped"
    assert ventilation_phase(Snapshot(power=True)) == "running"
    assert ventilation_phase(Snapshot(power=False)) == "stopped"
    assert ventilation_phase(Snapshot()) is None


def test_comfort_class() -> None:
    assert comfort_class(29.9) == "too_dry"
    assert comfort_class(30) == "dry"
    assert comfort_class(40) == "comfortable"
    assert comfort_class(60) == "comfortable"
    assert comfort_class(60.1) == "humid"
    assert comfort_class(70) == "humid"
    assert comfort_class(71) == "too_humid"


# ------------------------------------------------------------------ F4 / F5 / F6


def test_hour_of_low_stage(engine: AnalyticsEngine, clock: Clock) -> None:
    # 1 h at 20 m3/h and 4.2 W: 20 m3, 4.2 Wh, 1 operating hour
    values = run(engine, clock, snap(), 3600).values
    assert values["operating_hours"] == 1.0
    assert values["on_hours"] == 1.0
    assert values["operating_hours_today"] == 1.0
    assert values["airflow"] == 20
    assert values["air_volume"] == 20.0
    assert values["energy_estimate"] == 0.004  # 4.2 Wh rounded to 3 decimals
    assert values["power_estimate"] == 4.2
    assert values["runtime_stage_low"] == 1.0
    assert values["runtime_mode_heat_recovery"] == 1.0
    assert values["runtime_direction_exhaust"] == 1.0
    assert values["runtime_direction_intake"] == 0.0


@pytest.mark.parametrize(
    ("stage", "flow", "power"),
    [("night", 10, 3.9), ("low", 20, 4.2), ("medium", 40, 5.5), ("high", 60, 6.7)],
)
def test_stage_table(
    engine: AnalyticsEngine, clock: Clock, stage: str, flow: int, power: float
) -> None:
    out = step(engine, clock, snap(physical_stage=stage))
    assert val(out, "airflow") == flow
    assert val(out, "power_estimate") == power
    # a unit supplies fresh air half of the time in heat recovery
    assert val(out, "fresh_air_flow") == round(flow * 0.5)


def test_ten_hours_high_energy(engine: AnalyticsEngine, clock: Clock) -> None:
    out = run(engine, clock, snap(physical_stage="high"), 36000, every=300)
    assert val(out, "energy_estimate") == 0.067  # 6.7 W * 10 h
    assert val(out, "air_volume") == 600.0


def test_standby_and_calibration(engine: AnalyticsEngine, clock: Clock) -> None:
    out = run(engine, clock, snap(**STOPPED), 36000, every=300)
    assert val(out, "power_estimate") == 0.5
    assert val(out, "energy_estimate") == 0.005  # 0.5 W * 10 h
    assert val(out, "airflow") == 0
    assert val(out, "operating_hours") == 0.0
    assert val(out, "on_hours") == 0.0
    assert val(out, "runtime_mode_heat_recovery") == 10.0
    engine.configure(AnalyticsConfig(power_scale=2.0, standby_w=1.0))
    assert val(step(engine, clock, snap()), "power_estimate") == 8.4
    assert val(step(engine, clock, snap(**STOPPED)), "power_estimate") == 1.0


def test_gap_is_not_credited(engine: AnalyticsEngine, clock: Clock) -> None:
    step(engine, clock, snap())
    clock.advance(3600)  # Home Assistant was down; only max_gap_s counts
    out = step(engine, clock, snap())
    assert val(out, "operating_hours") == pytest.approx(300 / 3600, abs=0.005)
    clock.mono -= 100  # a backwards step is ignored
    out2 = step(engine, clock, snap())
    assert val(out2, "operating_hours") == val(out, "operating_hours")


def test_cloud_only_duty_and_unknown_phase(
    engine: AnalyticsEngine, clock: Clock
) -> None:
    cloud = snap(direction=None, coupling=None, physical_stage=None, running=None)
    out = run(engine, clock, cloud, 3600)
    # fan_speed Low in heat recovery: 20 m3/h * 60/70 duty over 1 h
    assert val(out, "air_volume") == 17.1
    assert val(out, "airflow") == 20
    # continuous exhaust has no duty loss
    ext = replace(cloud, operating_mode="Expulsion")
    assert val(run(AnalyticsEngine(), Clock(), ext, 3600), "air_volume") == 20.0
    values = step(engine, clock, Snapshot()).values
    assert values["airflow"] is None and values["power_estimate"] is None
    assert values["measurement_context"] == "unknown"
    assert values["damper_state"] is None


def test_missing_stage_gives_no_flow(engine: AnalyticsEngine, clock: Clock) -> None:
    out = step(engine, clock, snap(physical_stage=None, fan_speed=None))
    assert val(out, "airflow") is None and val(out, "power_estimate") is None
    assert val(out, "air_change_rate") is None


def test_unknown_stage_and_mode_are_not_credited(clock: Clock) -> None:
    engine = AnalyticsEngine(AnalyticsConfig(airflow={"low": 20.0}))
    blind = snap(physical_stage=None, fan_speed=None, operating_mode=None)
    out = run(engine, clock, blind, 120)
    assert val(out, "operating_hours") == pytest.approx(120 / 3600, abs=0.01)
    assert val(out, "air_volume") == 0.0 and val(out, "energy_estimate") == 0.0
    # a stage missing from a custom airflow table gives no flow either
    out = step(engine, clock, snap(physical_stage="high"))
    assert val(out, "airflow") is None
    out = step(AnalyticsEngine(), clock, Snapshot(), serial="NEW")
    assert val(out, "damper_state") is None


def test_air_change_rate_needs_volume(clock: Clock) -> None:
    engine = AnalyticsEngine(AnalyticsConfig(room_volumes={"S1": 50.0}))
    out = step(engine, clock, snap(physical_stage="high"))
    # fresh air 60 * 0.5 = 30 m3/h over 50 m3
    assert val(out, "air_change_rate") == 0.6
    other = step(engine, clock, snap(), serial="OTHER")
    assert val(other, "air_change_rate") is None


def test_one_way_mode_supplies_full_flow(engine: AnalyticsEngine, clock: Clock) -> None:
    out = step(engine, clock, snap(coupling="same", operating_mode="Expulsion"))
    assert val(out, "fresh_air_flow") == 20


# ------------------------------------------------------------------ cycles


def test_reversals_and_half_cycle(engine: AnalyticsEngine, clock: Clock) -> None:
    sequence = [
        ("exhaust", 0),
        ("stopped", 60),
        ("intake", 10),
        ("stopped", 60),
        ("exhaust", 10),
    ]
    out = None
    for direction, advance in sequence:
        clock.advance(advance)
        out = step(engine, clock, snap(direction=direction))
    assert out is not None
    assert val(out, "reversals") == 2
    assert val(out, "heat_recovery_cycles") == 1
    assert val(out, "half_cycle_duration") == 70
    seconds = engine.export_state()["devices"]["S1"]["direction_s"]
    assert seconds == {"exhaust": 60.0, "stopped": 20.0, "intake": 60.0}


def test_external_reversal_counter(engine: AnalyticsEngine, clock: Clock) -> None:
    step(engine, clock, snap(reversals=5))
    clock.advance(60)
    assert val(step(engine, clock, snap(reversals=8)), "reversals") == 3
    clock.advance(60)
    # tracker restarted: the new counter value is added as is
    assert val(step(engine, clock, snap(reversals=2)), "reversals") == 5
    # own observation is ignored while the tracker counter is supplied
    clock.advance(60)
    out = step(engine, clock, snap(reversals=2, direction="intake"))
    assert val(out, "reversals") == 5


# ------------------------------------------------------------------ F12 validity


def test_validity_settling_and_hold(engine: AnalyticsEngine, clock: Clock) -> None:
    off = snap(**STOPPED, temperature=15, humidity=80)
    out = step(engine, clock, off)
    assert val(out, "measurement_valid") is False
    assert val(out, "measurement_context") == "stopped"
    assert val(out, "room_temperature") is None and val(out, "dew_point") is None
    assert val(out, "comfort") is None
    # the unit starts: 5 minutes settling, readings are duct readings
    clock.advance(60)
    duct = snap(temperature=14, humidity=70)
    step(engine, clock, duct)
    clock.advance(299)
    out = step(engine, clock, duct)
    assert val(out, "measurement_valid") is False
    assert val(out, "measurement_context") == "settling"
    assert val(out, "room_temperature") is None
    clock.advance(1)
    out = step(engine, clock, snap(temperature=21, humidity=50))
    assert val(out, "measurement_valid") is True
    assert val(out, "measurement_context") == "exhaust"
    assert val(out, "room_temperature") == 21
    assert val(out, "room_humidity") == 50
    # T = 21 C, RH = 50 %: e_s = 24.81 hPa, g = ln(0.5) + 1.40101 = 0.70786
    assert val(out, "dew_point") == 10.2  # 243.12 g / (17.62 - g)
    assert val(out, "absolute_humidity") == 9.1  # 216.7 * 12.40 / 294.15
    assert val(out, "comfort") == "comfortable"
    assert val(out, "measurement_last_valid") == clock.wall.replace(second=0)
    # stopped again: last valid values are held, raw duct readings ignored
    clock.advance(60)
    out = step(engine, clock, off)
    assert val(out, "measurement_valid") is False
    assert val(out, "room_temperature") == 21
    assert val(out, "dew_point") == 10.2
    assert val(out, "comfort") is None
    # held values expire
    clock.advance(13 * 3600)
    out = step(engine, clock, off)
    assert val(out, "room_temperature") is None


def test_validity_requires_a_reading_and_plausible_values(clock: Clock) -> None:
    engine = settled_engine()
    out = step(engine, clock, snap(temperature=None, humidity=None))
    assert val(out, "measurement_valid") is False
    out = step(engine, clock, snap(temperature=200, humidity=140))
    assert val(out, "measurement_valid") is True
    assert val(out, "room_temperature") is None
    assert val(out, "room_humidity") is None
    out = step(engine, clock, snap(temperature=20, humidity=None))
    assert val(out, "room_temperature") == 20
    assert val(out, "room_humidity") is None
    assert val(out, "comfort") is None
    assert val(out, "humidity_setpoint_distance") is None


def test_humidity_setpoint_and_distance(clock: Clock) -> None:
    engine = settled_engine()
    out = step(engine, clock, snap(humidity_level="Moist", humidity=80))
    assert val(out, "humidity_setpoint") == 75
    assert val(out, "humidity_setpoint_distance") == 5
    out = step(engine, clock, snap(humidity_level="Dry", humidity=35))
    assert val(out, "humidity_setpoint") == 40
    assert val(out, "humidity_setpoint_distance") == -5
    out = step(engine, clock, snap(operating_mode="AwayHome", humidity=62))
    assert val(out, "humidity_setpoint") == 60
    assert val(out, "humidity_setpoint_distance") == 2
    out = step(engine, clock, snap(humidity_level=None))
    assert val(out, "humidity_setpoint") is None
    out = step(engine, clock, snap(humidity_level="Strange"))
    assert val(out, "humidity_setpoint") is None


# ------------------------------------------------------------------ F7 heat


def test_recovered_heat_and_loss(clock: Clock) -> None:
    engine = settled_engine()
    outdoor = OutdoorConditions(1.0, 80.0)
    medium = snap(physical_stage="medium", temperature=21)
    out = step(engine, clock, medium, outdoor=outdoor)
    # fresh air 40 * 0.5 = 20 m3/h, dT = 20 K: 0.335 * 20 * 20 = 134 W total;
    # recovered 0.808 * 134 = 108.3 W, loss 0.192 * 134 = 25.7 W
    assert val(out, "heat_recovered_power") == 108
    assert val(out, "ventilation_loss_power") == 26
    # one hour: 108.272 Wh and 25.728 Wh
    out = run(engine, clock, medium, 3600, outdoor=outdoor)
    assert val(out, "heat_recovered_energy") == pytest.approx(0.108, abs=0.001)
    assert val(out, "ventilation_loss_energy") == pytest.approx(0.026, abs=0.001)
    # AH(21 C, 50 %) = 9.14, AH(1 C, 80 %) = 4.15: 0.475 * 20 * 4.98 = 47 g/h
    assert val(out, "moisture_recovered") == 47


def test_heat_without_recovery_in_one_way_mode(clock: Clock) -> None:
    engine = settled_engine()
    one_way = snap(coupling="same", operating_mode="Expulsion", physical_stage="high")
    out = step(engine, clock, one_way, outdoor=OutdoorConditions(1.0))
    # 60 m3/h, 20 K: 0.335 * 60 * 20 = 402 W, all lost
    assert val(out, "heat_recovered_power") == 0
    assert val(out, "ventilation_loss_power") == 402
    assert val(out, "moisture_recovered") is None


def test_heat_zero_when_outdoor_warmer_or_stopped(clock: Clock) -> None:
    engine = settled_engine()
    out = step(engine, clock, snap(), outdoor=OutdoorConditions(30.0, 50.0))
    assert val(out, "heat_recovered_power") == 0
    assert val(out, "ventilation_loss_power") == 0
    out = step(engine, clock, snap(**STOPPED), outdoor=OutdoorConditions(1.0))
    assert val(out, "heat_recovered_power") == 0
    assert val(out, "moisture_recovered") == 0


def test_heat_unavailable_without_inputs(clock: Clock) -> None:
    engine = settled_engine()
    out = step(engine, clock, snap())  # no outdoor source
    assert val(out, "heat_recovered_power") is None
    assert val(out, "drying_potential") is None
    # outdoor source but no valid room reading yet
    out = step(AnalyticsEngine(), clock, snap(), outdoor=OutdoorConditions(1.0))
    assert val(out, "heat_recovered_power") is None
    # unknown stage while ventilating
    unknown_stage = snap(physical_stage=None, fan_speed=None)
    out = step(engine, clock, unknown_stage, outdoor=OutdoorConditions(1.0))
    assert val(out, "heat_recovered_power") is None
    # night stage has no published moisture recovery
    night = snap(physical_stage="night")
    out = step(engine, clock, night, outdoor=OutdoorConditions(1.0, 80.0))
    assert val(out, "heat_recovered_power") is not None
    assert val(out, "moisture_recovered") is None
    # unknown ventilation state
    out = step(engine, clock, Snapshot(), outdoor=OutdoorConditions(1.0, 80.0))
    assert val(out, "heat_recovered_power") is None


def test_drying_potential_and_warning(clock: Clock) -> None:
    engine = settled_engine()
    room = snap(temperature=21, humidity=50)
    out = step(engine, clock, room, outdoor=OutdoorConditions(5.0, 80.0))
    # indoor 9.14 g/m3 - outdoor 5.43 g/m3
    assert val(out, "drying_potential") == pytest.approx(3.7, abs=0.1)
    assert val(out, "outdoor_humidifies") is False
    out = step(engine, clock, room, outdoor=OutdoorConditions(20.0, 100.0))
    # outdoor AH(20 C, 100 %) = 17.3 g/m3: wetter than indoors
    assert val(out, "drying_potential") == pytest.approx(9.14 - 17.3, abs=0.2)
    assert val(out, "outdoor_humidifies") is True


# ------------------------------------------------------------------ F9


@pytest.mark.parametrize(
    ("raw", "index"),
    [
        ("VeryGood", 1),
        ("Good", 2),
        ("Medium", 3),
        ("Poor", 4),
        ("Bad", 5),
        ("Odd", None),
        (None, None),
    ],
)
def test_air_quality_index(
    engine: AnalyticsEngine, clock: Clock, raw: str | None, index: int | None
) -> None:
    out = step(engine, clock, snap(air_quality=raw))
    assert val(out, "air_quality_index") == index
    assert val(out, "air_quality_raw") == raw


# ------------------------------------------------------------------ F10 filter


def test_filter_prediction_from_usage(clock: Clock) -> None:
    engine = AnalyticsEngine()
    today = WALL0.date()
    reset = WALL0 - timedelta(days=40)
    engine.import_state(
        {
            "version": 1,
            "devices": {
                "S1": {
                    "filter_s": 2900 * 3600,
                    "tracking_since": (WALL0 - timedelta(days=2)).isoformat(),
                    "filter_reset": reset.isoformat(),
                    "usage": {
                        today.isoformat(): 6 * 3600,
                        (today - timedelta(days=1)).isoformat(): 6 * 3600,
                        (today - timedelta(days=30)).isoformat(): 99 * 3600,
                    },
                }
            },
        }
    )
    out = step(engine, clock, snap())
    # 12 h used over 48 h tracked = 25 % duty: 100 h left -> 400 h
    assert val(out, "filter_hours") == 2900.0
    assert val(out, "filter_remaining_hours") == 100
    assert val(out, "filter_alarm_predicted") == WALL0 + timedelta(hours=400)
    assert val(out, "filter_last_reset") == reset
    assert val(out, "filter_clean_due") == reset + timedelta(days=91)
    assert val(out, "filter_replace_due") == reset + timedelta(days=730)
    usage = engine.export_state()["devices"]["S1"]["usage"]
    assert (today - timedelta(days=30)).isoformat() not in usage


def test_filter_prediction_edge_cases(engine: AnalyticsEngine, clock: Clock) -> None:
    out = step(engine, clock, snap())
    # fresh tracking: not enough data yet
    assert val(out, "filter_alarm_predicted") is None
    assert val(out, "filter_remaining_hours") == 3000
    # limit reached without Bad status: due now
    engine2 = AnalyticsEngine(AnalyticsConfig(filter_limit_h=0.0))
    assert val(step(engine2, clock, snap()), "filter_alarm_predicted") == WALL0
    # Bad status: remaining 0, no prediction
    out = step(engine, clock, snap(filter_status="Bad"))
    assert val(out, "filter_remaining_hours") == 0
    assert val(out, "filter_alarm_predicted") is None


def test_filter_reset_and_learned_hours(engine: AnalyticsEngine, clock: Clock) -> None:
    out = run(engine, clock, snap(filter_status="Good"), 3600)
    assert val(out, "filter_hours") == 1.0
    assert val(out, "filter_last_reset") is None
    out = run(engine, clock, snap(filter_status="Medium"), 1800)
    assert val(out, "filter_medium_hours") == pytest.approx(1.0, abs=0.02)
    assert val(out, "filter_alarm_hours") is None
    out = run(engine, clock, snap(filter_status="Bad"), 1800)
    assert val(out, "filter_alarm_hours") == pytest.approx(1.5, abs=0.02)
    # replacing the filter: Bad -> Good resets the counter
    out = step(engine, clock, snap(filter_status="Good"))
    assert val(out, "filter_hours") == 0.0
    assert val(out, "filter_last_reset") == clock.wall.replace(second=0)
    assert val(out, "filter_medium_hours") is None
    # own reset button
    run(engine, clock, snap(), 600)
    clock.advance(60)
    engine.reset_filter("S1", clock.wall)
    assert val(step(engine, clock, snap()), "filter_hours") == 0.0
    engine.reset_filter("NEW", clock.wall)


def test_filter_on_time_basis(clock: Clock) -> None:
    engine = AnalyticsEngine(AnalyticsConfig(filter_basis="on_time"))
    idle = snap(direction="stopped", coupling="idle")
    out = run(engine, clock, idle, 3600)
    assert val(out, "filter_hours") == 1.0
    assert val(out, "operating_hours") == 0.0


# ------------------------------------------------------------------ F11 / F19


def test_humidity_exhaust(engine: AnalyticsEngine, clock: Clock) -> None:
    alarm = snap(
        humidity_alarm=True, operating_mode="Auto", coupling="same", direction="exhaust"
    )

    def active(snapshot: Snapshot) -> Any:
        return val(step(engine, clock, snapshot), "humidity_exhaust_active")

    assert active(alarm) is True
    # alarm but the unit is not exhausting (yet)
    assert active(replace(alarm, direction="intake")) is False
    # cloud only: an alarm in Auto counts
    assert active(replace(alarm, coupling=None, direction=None)) is True
    assert active(replace(alarm, power=False)) is False
    assert active(replace(alarm, operating_mode="Night")) is False
    assert active(replace(alarm, humidity_alarm=False)) is False
    assert active(replace(alarm, humidity_alarm=None)) is None
    assert active(replace(alarm, operating_mode=None)) is None


def test_night_reduction(engine: AnalyticsEngine, clock: Clock) -> None:
    def active(**kwargs: Any) -> Any:
        return val(step(engine, clock, snap(**kwargs)), "night_reduction_active")

    assert active(operating_mode="Smart", physical_stage="night") is True
    assert active(operating_mode="Auto", physical_stage="medium") is False
    assert active(operating_mode="Night", physical_stage="night") is False
    assert active(operating_mode="Smart", physical_stage=None, fan_speed=None) is None
    assert active(operating_mode=None) is None


def test_timed_exhaust_and_end(engine: AnalyticsEngine, clock: Clock) -> None:
    timed = snap(operating_mode="TimedExpulsion", coupling="same")
    out = step(engine, clock, timed)
    assert val(out, "timed_exhaust_active") is True
    start = clock.wall.replace(second=0)
    assert val(out, "timed_exhaust_ends") == start + timedelta(minutes=20)
    clock.advance(60)
    out = step(engine, clock, timed)
    assert val(out, "timed_exhaust_ends") == start + timedelta(minutes=20)
    clock.advance(60)
    out = step(engine, clock, snap())
    assert val(out, "timed_exhaust_active") is False
    assert val(out, "timed_exhaust_ends") is None
    unknown = step(engine, clock, snap(operating_mode=None))
    assert val(unknown, "timed_exhaust_active") is None
    # powered off in timed mode: not active
    off = snap(operating_mode="TimedExpulsion", power=False)
    assert val(step(engine, clock, off), "timed_exhaust_active") is False


def test_smart_behaviour(engine: AnalyticsEngine, clock: Clock) -> None:
    smart = snap(operating_mode="Smart")
    out = step(engine, clock, smart)
    assert val(out, "smart_behaviour") == "heat_recovery"
    assert val(out, "free_cooling_active") is False
    night = replace(smart, physical_stage="night")
    assert val(step(engine, clock, night), "smart_behaviour") == "night_heat_recovery"
    same = replace(smart, coupling="same")
    assert val(step(engine, clock, same), "smart_behaviour") == "free_cooling"
    idle = replace(smart, direction="stopped", coupling="idle")
    assert val(step(engine, clock, idle), "smart_behaviour") == "idle"
    unknown = step(engine, clock, replace(smart, coupling=None))
    assert val(unknown, "smart_behaviour") is None
    assert val(unknown, "free_cooling_active") is None
    other = step(engine, clock, snap())
    assert val(other, "smart_behaviour") is None
    assert val(other, "free_cooling_active") is False
    nomode = step(engine, clock, snap(operating_mode=None))
    assert val(nomode, "free_cooling_active") is None
    blind = step(engine, clock, Snapshot(operating_mode="Smart"))
    assert val(blind, "smart_behaviour") is None


def test_free_cooling_from_constant_direction(
    engine: AnalyticsEngine, clock: Clock
) -> None:
    smart = snap(operating_mode="Smart", direction="exhaust")
    out = run(engine, clock, smart, 120, every=30)
    assert val(out, "smart_behaviour") == "heat_recovery"
    out = run(engine, clock, smart, 60, every=30)
    # exhaust for more than two half cycles: a flow pattern, not alternation
    assert val(out, "smart_behaviour") == "free_cooling"
    assert val(out, "free_cooling_active") is True


def test_damper_state(engine: AnalyticsEngine, clock: Clock) -> None:
    # first observation: a running unit is assumed open
    assert val(step(engine, clock, snap()), "damper_state") == "open"
    stopped = snap(**STOPPED)
    clock.advance(60)
    assert val(step(engine, clock, stopped), "damper_state") == "closing"
    clock.advance(299)
    assert val(step(engine, clock, stopped), "damper_state") == "closing"
    clock.advance(1)
    assert val(step(engine, clock, stopped), "damper_state") == "closed"
    clock.advance(60)
    assert val(step(engine, clock, snap()), "damper_state") == "opening"
    clock.advance(39)
    assert val(step(engine, clock, snap()), "damper_state") == "opening"
    clock.advance(1)
    assert val(step(engine, clock, snap()), "damper_state") == "open"
    # first observation of a stopped unit: closed
    assert val(step(AnalyticsEngine(), clock, stopped), "damper_state") == "closed"


# ------------------------------------------------------------------ F17


def test_alarm_durations_and_midnight(engine: AnalyticsEngine, clock: Clock) -> None:
    wet = snap(humidity_alarm=True, night_alarm=True)
    out = run(engine, clock, wet, 600, every=60)
    assert val(out, "humidity_alarm_current") == 10
    assert val(out, "humidity_alarm_today") == 10
    assert val(out, "humidity_alarm_duration") == pytest.approx(0.17, abs=0.01)
    assert val(out, "night_alarm_current") == 10
    out = run(engine, clock, snap(humidity_alarm=False, night_alarm=False), 60)
    assert val(out, "humidity_alarm_current") == 0
    assert val(out, "humidity_alarm_today") == 10
    # an unknown alarm state keeps a running episode
    run(engine, clock, wet, 60)
    out = step(engine, clock, snap(humidity_alarm=None))
    assert val(out, "humidity_alarm_current") == 1
    # midnight: the today counters restart, totals stay
    clock.wall = clock.wall.replace(hour=23, minute=59)
    run(engine, clock, wet, 120, every=60)
    out = step(engine, clock, wet)
    assert val(out, "humidity_alarm_today") <= 3
    assert val(out, "humidity_alarm_duration") > 0.17
    assert val(out, "operating_hours_today") < 0.1


# ------------------------------------------------------------------ F18


def test_command_latency_and_counters(engine: AnalyticsEngine, clock: Clock) -> None:
    engine.command_sent("S1", "a", clock.mono)
    clock.advance(2.5)
    assert engine.command_confirmed("S1", "a", clock.mono, "local") == 2.5
    clock.advance(1.5)
    assert engine.command_confirmed("S1", "a", clock.mono) == 4.0
    assert engine.command_confirmed("S1", "a", clock.mono) is None  # duplicate
    assert engine.command_confirmed("S1", "zzz", clock.mono) is None  # unknown
    engine.command_sent("S1", "b", clock.mono)
    clock.advance(1.5)
    engine.command_confirmed("S1", "b", clock.mono)
    engine.command_sent("S1", "c", clock.mono)
    engine.command_rejected("S1", "c", clock.mono)
    engine.command_rejected("S1", "unknown", clock.mono)
    engine.command_sent("S1", "d", clock.mono)
    clock.advance(61)
    values = step(engine, clock, snap()).values
    assert values["command_latency"] == 1.5
    assert values["command_latency_local"] == 2.5
    assert values["command_latency_cloud"] == 1.5
    assert values["command_latency_average"] == 2.0  # mean of 2.5 and 1.5
    assert values["commands_sent"] == 4
    assert values["commands_confirmed"] == 2
    assert values["commands_unconfirmed"] == 1
    assert values["commands_rejected"] == 2
    engine.expire_commands("nobody", clock.mono)
    engine.expire_commands("S1", clock.mono)


def test_confirmed_then_rejected_corrects_count(
    engine: AnalyticsEngine, clock: Clock
) -> None:
    engine.command_sent("S1", 1, clock.mono)
    engine.command_confirmed("S1", 1, clock.mono)
    engine.command_rejected("S1", 1, clock.mono)
    values = step(engine, clock, snap()).values
    assert values["commands_confirmed"] == 0 and values["commands_rejected"] == 1
    engine.command_sent("S1", 2, clock.mono)
    engine.command_confirmed("S1", 2, clock.mono)
    clock.advance(100)  # confirmed commands expire silently
    assert val(step(engine, clock, snap()), "commands_unconfirmed") == 0


# ------------------------------------------------------------------ F16


def test_zone_and_house_totals(clock: Clock) -> None:
    engine = AnalyticsEngine()
    step(engine, clock, snap(zone="z1", physical_stage="low"), serial="A")
    step(engine, clock, snap(zone="z1", physical_stage="high"), serial="B")
    step(engine, clock, snap(zone="z2", **STOPPED), serial="C")
    zone = engine.zone_values("z1", clock.mono).values
    assert zone["zone_airflow"] == 80
    assert zone["zone_power"] == pytest.approx(10.9)
    assert zone["zone_running_units"] == 2
    assert zone["zone_units"] == 2
    house = engine.house_values(clock.mono).values
    assert house["house_airflow"] == 80
    assert house["house_power"] == pytest.approx(11.4)  # plus 0.5 W standby
    assert house["house_running_units"] == 2
    assert house["house_units"] == 3
    assert house["house_heat_recovered_power"] is None
    assert house["outdoor_dew_point"] is None
    house = engine.house_values(clock.mono, OutdoorConditions(5.0, 80.0)).values
    assert house["outdoor_dew_point"] == 1.8
    assert house["outdoor_absolute_humidity"] == 5.4
    assert engine.zone_values("nowhere", clock.mono).values["zone_airflow"] is None
    engine.forget("A")
    assert engine.house_values(clock.mono).values["house_units"] == 2


# ------------------------------------------------------------------ recorder budget


def test_recorder_gate(engine: AnalyticsEngine, clock: Clock) -> None:
    first = step(engine, clock, snap())
    assert "power_estimate" in first.changed  # the first value is always written
    clock.advance(30)
    second = step(engine, clock, snap(physical_stage="medium"))
    # power changed but within the 60 s minimum interval
    assert "power_estimate" not in second.changed
    assert val(second, "power_estimate") == 5.5
    clock.advance(31)
    third = step(engine, clock, snap(physical_stage="medium"))
    assert third.changed["power_estimate"] == 5.5
    # an unchanged value is not reported again
    clock.advance(120)
    fourth = step(engine, clock, snap(physical_stage="medium"))
    assert "power_estimate" not in fourth.changed
    # availability changes are reported at once, ignoring the interval
    fifth = step(engine, clock, Snapshot())
    assert fifth.changed["power_estimate"] is None
    assert "power_estimate" not in step(engine, clock, Snapshot()).changed
    sixth = step(engine, clock, snap())
    assert sixth.changed["power_estimate"] == 4.2


def test_recorder_threshold_and_datetime(clock: Clock) -> None:
    engine = settled_engine()
    step(engine, clock, snap(humidity=50))
    clock.advance(120)
    # rounded to precision 0 -> 50 again, below the threshold
    out = step(engine, clock, snap(humidity=50.4))
    assert "room_humidity" not in out.changed
    out = step(engine, clock, snap(humidity=52))
    assert out.changed["room_humidity"] == 52
    # timestamps move only by their threshold of one minute
    clock.advance(120)
    out = step(engine, clock, snap())
    assert "measurement_last_valid" in out.changed
    clock.advance(30)
    out = step(engine, clock, snap())
    assert "measurement_last_valid" not in out.changed
    clock.advance(120)
    out = step(engine, clock, snap())
    assert "measurement_last_valid" in out.changed


def test_values_cover_all_descriptions(engine: AnalyticsEngine, clock: Clock) -> None:
    out = step(engine, clock, snap())
    assert set(SCOPE_KEYS["device"]) <= set(out.values)
    assert set(out.values) - set(SCOPE_KEYS["device"]) == {"air_quality_raw"}
    produced = set(engine._devices["S1"].raw)
    assert set(SCOPE_KEYS["device"]) <= produced
    assert produced <= set(BY_KEY) | {"air_quality_raw"}


# ------------------------------------------------------------------ persistence


def test_persistence_round_trip(clock: Clock) -> None:
    engine = settled_engine()
    outdoor = OutdoorConditions(1.0, 80.0)
    timed = snap(operating_mode="TimedExpulsion", humidity_alarm=True)
    run(engine, clock, timed, 1800, outdoor=outdoor)
    engine.command_sent("S1", 1, clock.mono)
    medium = snap(filter_status="Medium", direction="intake")
    run(engine, clock, medium, 600, outdoor=outdoor)
    state = engine.export_state()
    assert state["version"] == 1

    restored = settled_engine()
    restored.import_state(json.loads(json.dumps(state)))
    assert restored.export_state() == state
    # five hours later: the downtime is not counted
    clock2 = Clock(mono=5.0, wall=clock.wall + timedelta(hours=5))
    out = step(restored, clock2, medium, outdoor=outdoor)
    original = engine.update("S1", medium, clock.mono, clock.wall, outdoor)
    for key in (
        "operating_hours",
        "air_volume",
        "energy_estimate",
        "heat_recovered_energy",
        "humidity_alarm_duration",
        "filter_hours",
        "reversals",
        "room_temperature",
        "commands_sent",
    ):
        assert val(out, key) == pytest.approx(val(original, key), abs=0.01), key


def test_import_ignores_garbage(engine: AnalyticsEngine, clock: Clock) -> None:
    engine.import_state(None)
    engine.import_state({"version": 99, "devices": {}})
    engine.import_state({"version": 1, "devices": "x"})
    engine.import_state(
        {
            "version": 1,
            "devices": {
                "bad": "not a dict",
                "S1": {
                    "operating_s": -5,
                    "mode_s": "x",
                    "reversals": "many",
                    "filter_reset": "not a date",
                    "held": {"temperature": "x", "at": 5},
                    "commands": "x",
                    "today": 3,
                    "filter_status": 3,
                    "filter_medium_s": "x",
                },
            },
        }
    )
    assert "bad" not in engine.export_state()["devices"]
    exported = engine.export_state()["devices"]["S1"]
    assert exported["operating_s"] == 0.0
    assert exported["reversals"] == 0
    assert exported["filter_reset"] is None
    assert exported["today"] is None
    assert val(step(engine, clock, snap()), "operating_hours") == 0.0
