"""Derived-data engine ("Datenausbau", plan F4-F13, F16-F19).

Pure Python, no Home Assistant dependency. The engine turns the resolved
datapoints of each unit into derived values: runtimes, air volume, estimated
power and energy, recovered heat, humidity physics, filter prediction, hidden
automatic actions, measurement validity, alarm durations and command latency.

Usage (see docs/DERIVED_DATA.md for the full contract)::

    engine = AnalyticsEngine(AnalyticsConfig(room_volumes={"SN1": 30.0}))
    engine.import_state(stored)                       # once, at start

    snap = snapshot_from_values(
        {key: hub.resolve(serial, key).value for key in SNAPSHOT_KEYS},
        zone="zone-1",
        reversals=local_zone_state.reversals,         # optional
    )
    derived = engine.update(serial, snap, now=monotonic(), wall=dt_util.now(),
                            outdoor=weather.current(monotonic()))
    for key, value in derived.changed.items(): ...    # publish only these

``now`` is a monotonic clock in seconds, ``wall`` a timezone-aware local
datetime (day boundaries and timestamps). Integration is strictly live: the
time between two updates is credited to the *previous* state (zero-order hold)
and gaps longer than ``max_gap_s`` are not credited, so a Home Assistant
outage never produces phantom hours.
"""

from __future__ import annotations

import statistics
from collections import deque
from collections.abc import Hashable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from types import MappingProxyType
from typing import Any, Final

from . import psychrometrics as psy
from .analytics_descriptions import (
    AIR_QUALITY_INDEX,
    BY_KEY,
    DIRECTIONS,
    MODE_SLUGS,
    SCOPE_KEYS,
    STAGES,
)
from .weather_input import (
    DEFAULT_HUMIDIFY_MARGIN,
    OutdoorConditions,
    drying_potential,
    would_humidify,
)

STATE_VERSION: Final = 1

SNAPSHOT_KEYS: Final = (
    "power",
    "running",
    "physical_stage",
    "direction",
    "coupling",
    "operating_mode",
    "fan_speed",
    "humidity_level",
    "temperature",
    "humidity",
    "air_quality",
    "filter_status",
    "night_alarm",
    "humidity_alarm",
)
"""Datapoint keys the lead resolves for :func:`snapshot_from_values`."""

DEFAULT_AIRFLOW: Final = MappingProxyType(
    {"night": 10.0, "low": 20.0, "medium": 40.0, "high": 60.0, "turbo": 60.0}
)
"""m3/h per unit (manual p.30; DIBt confirms within 5 %). Turbo: unpublished."""
DEFAULT_POWER: Final = MappingProxyType(
    {"night": 3.9, "low": 4.2, "medium": 5.5, "high": 6.7, "turbo": 6.7}
)
"""W per unit (manual p.30). Turbo: unpublished, treated as high."""
DEFAULT_HEAT_ETA: Final = MappingProxyType(
    {"night": 0.92, "low": 0.918, "medium": 0.808, "high": 0.729, "turbo": 0.729}
)
"""Temperature ratio (DIBt 7 deg C outdoor); night is extrapolated."""
DEFAULT_MOISTURE_ETA: Final = MappingProxyType(
    {"night": None, "low": 0.58, "medium": 0.475, "high": 0.383, "turbo": 0.383}
)
"""Moisture recovery (DIBt App. G2); not published for night."""
DEFAULT_SETPOINTS: Final = MappingProxyType(
    {"Dry": 40.0, "Normal": 60.0, "Moist": 75.0}
)

FAN_SPEED_STAGES: Final = MappingProxyType(
    {
        "Night": "night",
        "Low": "low",
        "Medium": "medium",
        "High": "high",
        "Turbo": "turbo",
    }
)
CYCLIC_MODES: Final = frozenset({"ManualHeatRecovery", "Night", "AwayHome"})
"""Modes that always alternate direction (heat recovery)."""
CYCLIC_WHEN_DRY_MODES: Final = frozenset({"Smart", "Auto"})
"""Alternate unless a humidity alarm switches them to constant exhaust."""

MOVING_PHASES: Final = frozenset({"exhaust", "intake", "reversing", "running"})
HALF_CYCLE_S: Final = 70.0
CYCLE_DUTY: Final = 60.0 / 70.0
"""Moving share of a half cycle (about 10 s of 70 s stand still)."""
FLOW_PATTERN_S: Final = 150.0
"""Constant direction longer than two half cycles means a flow mode."""
COMFORT_BANDS: Final = ((30.0, "too_dry"), (40.0, "dry"))
FILTER_USAGE_DAYS: Final = 7
USAGE_KEEP_DAYS: Final = 14
LATENCY_WINDOW: Final = 20
HALF_CYCLE_WINDOW: Final = 20


@dataclass(frozen=True, slots=True)
class AnalyticsConfig:
    """Tunable model parameters; defaults are the measured/datasheet figures."""

    airflow: Mapping[str, float] = DEFAULT_AIRFLOW
    power_w: Mapping[str, float] = DEFAULT_POWER
    heat_eta: Mapping[str, float] = DEFAULT_HEAT_ETA
    moisture_eta: Mapping[str, float | None] = DEFAULT_MOISTURE_ETA
    standby_w: float = 0.5
    """Assumption (unpublished); calibrate with a measuring plug."""
    power_scale: float = 1.0
    """Calibration factor applied to the per-stage power."""
    room_volumes: Mapping[str, float] = field(default_factory=dict)
    """Serial -> room volume in m3 served by that unit (air change rate)."""
    settle_s: float = 300.0
    """Seconds of continuous ventilation before temperature/humidity count as
    room values (the sensor first reads the duct air)."""
    hold_max_age_s: float = 12 * 3600.0
    """Last-valid readings older than this are not used for derivations."""
    filter_limit_h: float = 3000.0
    filter_basis: str = "operating"
    """``operating`` (fan moving air) or ``on_time`` (powered on)."""
    max_gap_s: float = 300.0
    humidify_margin: float = DEFAULT_HUMIDIFY_MARGIN
    supply_share: float = 0.5
    """Share of time a unit supplies fresh air in heat recovery operation."""
    away_setpoint: float = 60.0
    timed_exhaust_s: float = 1200.0
    damper_open_s: float = 40.0
    damper_close_s: float = 300.0
    command_timeout_s: float = 60.0
    setpoints: Mapping[str, float] = DEFAULT_SETPOINTS


@dataclass(frozen=True, slots=True)
class Snapshot:
    """Resolved datapoint values of one unit at one moment (plain values)."""

    power: bool | None = None
    running: bool | None = None
    physical_stage: str | None = None
    direction: str | None = None
    coupling: str | None = None
    operating_mode: str | None = None
    fan_speed: str | None = None
    humidity_level: str | None = None
    temperature: float | None = None
    humidity: float | None = None
    air_quality: str | None = None
    filter_status: str | None = None
    night_alarm: bool | None = None
    humidity_alarm: bool | None = None
    reversals: int | None = None
    """Optional ``LocalZoneState.reversals`` of the unit's zone."""
    zone: str | None = None
    """Zone key for zone totals."""


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return None if text.lower() in ("unknown", "unavailable", "none", "") else text


def _flag(value: Any) -> bool | None:
    return None if value is None else bool(value)


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None


def snapshot_from_values(
    values: Mapping[str, Any], *, zone: str | None = None, reversals: int | None = None
) -> Snapshot:
    """Build a :class:`Snapshot` from ``{key: ResolvedValue.value}``.

    Enums (``Direction``, ``Coupling``, ``LocalSpeed``) are normalised to their
    string value; ``unknown`` and unavailable values become None.
    """
    return Snapshot(
        power=_flag(values.get("power")),
        running=_flag(values.get("running")),
        physical_stage=_text(values.get("physical_stage")),
        direction=_text(values.get("direction")),
        coupling=_text(values.get("coupling")),
        operating_mode=_text(values.get("operating_mode")),
        fan_speed=_text(values.get("fan_speed")),
        humidity_level=_text(values.get("humidity_level")),
        temperature=_number(values.get("temperature")),
        humidity=_number(values.get("humidity")),
        air_quality=_text(values.get("air_quality")),
        filter_status=_text(values.get("filter_status")),
        night_alarm=_flag(values.get("night_alarm")),
        humidity_alarm=_flag(values.get("humidity_alarm")),
        reversals=reversals,
        zone=zone,
    )


@dataclass(frozen=True, slots=True)
class Derived:
    """Result of an update: all values, and those the recorder budget lets through."""

    values: dict[str, Any]
    changed: dict[str, Any]


def effective_stage(snap: Snapshot) -> str | None:
    """Return the fan stage: UDP stage first, then mode Night, then fan speed."""
    if snap.physical_stage in STAGES:
        return snap.physical_stage
    if snap.operating_mode == "Night":
        return "night"
    if snap.fan_speed is None:
        return None
    return FAN_SPEED_STAGES.get(snap.fan_speed)


def is_cyclic(snap: Snapshot) -> bool:
    """Return whether the unit alternates direction (heat recovery operation)."""
    if snap.coupling == "opposite":
        return True
    if snap.coupling in ("same", "idle"):
        return False
    mode = snap.operating_mode
    if mode in CYCLIC_MODES:
        return True
    return mode in CYCLIC_WHEN_DRY_MODES and not snap.humidity_alarm


def ventilation_phase(snap: Snapshot) -> str | None:
    """Return exhaust, intake, reversing, running, stopped or None (unknown).

    ``reversing``: direction stopped while the zone is coupled (the ~10 s stop
    between two half cycles). ``running``: air moves but the direction is
    unknown (cloud only).
    """
    if snap.direction in ("exhaust", "intake"):
        return snap.direction
    if snap.direction == "stopped":
        if snap.power is False or snap.coupling == "idle":
            return "stopped"
        return "reversing"
    moving = snap.running if snap.running is not None else snap.power
    if moving is None:
        return None
    return "running" if moving else "stopped"


def comfort_class(humidity: float) -> str:
    """Classify RH: <30 too_dry, <40 dry, <=60 ok, <=70 humid, else too_humid."""
    for limit, name in COMFORT_BANDS:
        if humidity < limit:
            return name
    if humidity <= 60.0:
        return "comfortable"
    return "humid" if humidity <= 70.0 else "too_humid"


@dataclass(slots=True)
class _Moment:
    """Derived state held until the next update (what time is credited to)."""

    phase: str | None = None
    ventilating: bool | None = None
    power_on: bool | None = None
    mode: str | None = None
    stage: str | None = None
    direction: str | None = None
    flow_fraction: float = 0.0
    airflow: float | None = None
    fresh: float | None = None
    power_w: float | None = None
    recovered_w: float | None = None
    loss_w: float | None = None
    moisture_gh: float | None = None
    humidity_alarm: bool = False
    night_alarm: bool = False


@dataclass(slots=True)
class _Command:
    sent: float
    confirmed: bool = False
    sources: set[str] = field(default_factory=set)


@dataclass(slots=True)
class _Device:
    zone: str | None = None
    # runtimes (seconds)
    operating_s: float = 0.0
    on_s: float = 0.0
    mode_s: dict[str, float] = field(default_factory=dict)
    stage_s: dict[str, float] = field(default_factory=dict)
    direction_s: dict[str, float] = field(default_factory=dict)
    reversals: int = 0
    # accumulators
    volume_m3: float = 0.0
    energy_kwh: float = 0.0
    recovered_kwh: float = 0.0
    loss_kwh: float = 0.0
    # alarms
    humidity_alarm_s: float = 0.0
    night_alarm_s: float = 0.0
    humidity_today_s: float = 0.0
    night_today_s: float = 0.0
    today_op_s: float = 0.0
    today: str | None = None
    # filter
    filter_s: float = 0.0
    filter_status: str | None = None
    filter_reset: datetime | None = None
    tracking_since: datetime | None = None
    filter_medium_s: float | None = None
    filter_bad_s: float | None = None
    usage: dict[str, float] = field(default_factory=dict)
    # held measurement
    held_temperature: float | None = None
    held_humidity: float | None = None
    held_at: datetime | None = None
    timed_started: datetime | None = None
    # command statistics
    cmd_sent: int = 0
    cmd_confirmed: int = 0
    cmd_unconfirmed: int = 0
    cmd_rejected: int = 0
    # live-only state (not persisted)
    moment: _Moment | None = None
    last_now: float | None = None
    vent_since: float | None = None
    stop_since: float | None = None
    damper_open_since: float | None = None
    hum_alarm_since: float | None = None
    night_alarm_since: float | None = None
    dir_since: float | None = None
    last_dir: str | None = None
    last_moving_dir: str | None = None
    last_reversal: float | None = None
    ext_reversals: int | None = None
    half_cycles: deque[float] = field(
        default_factory=lambda: deque(maxlen=HALF_CYCLE_WINDOW)
    )
    pending: dict[Hashable, _Command] = field(default_factory=dict)
    latencies: deque[float] = field(
        default_factory=lambda: deque(maxlen=LATENCY_WINDOW)
    )
    latency_last: float | None = None
    latency_cloud: float | None = None
    latency_local: float | None = None
    raw: dict[str, Any] = field(default_factory=dict)


def _differs(key: str, value: Any, last: Any) -> bool:
    desc = BY_KEY[key]
    if isinstance(value, bool) or isinstance(last, bool):
        return bool(value != last)
    if isinstance(value, datetime) and isinstance(last, datetime):
        seconds = abs((value - last).total_seconds())
        return seconds >= desc.min_change - 1e-9 if desc.min_change > 0 else seconds > 0
    if isinstance(value, int | float) and isinstance(last, int | float):
        diff = abs(value - last)
        return diff >= desc.min_change - 1e-9 if desc.min_change > 0 else diff > 0
    return bool(value != last)


def _round(key: str, value: Any) -> Any:
    desc = BY_KEY.get(key)
    if desc is None or value is None or isinstance(value, bool):
        return value
    if isinstance(value, datetime):
        return value.replace(second=0, microsecond=0)
    if isinstance(value, int | float) and desc.precision is not None:
        return round(value) if desc.precision == 0 else round(value, desc.precision)
    return value


class _Gate:
    """Recorder budget (C6): report a value only on a meaningful change."""

    def __init__(self) -> None:
        self._last: dict[tuple[str, str], tuple[Any, float]] = {}

    def filter(
        self, scope_id: str, values: Mapping[str, Any], now: float
    ) -> dict[str, Any]:
        changed: dict[str, Any] = {}
        for key, value in values.items():
            desc = BY_KEY.get(key)
            if desc is None:
                continue
            record = self._last.get((scope_id, key))
            if record is None:
                write = True
            else:
                last, at = record
                if value is None or last is None:
                    write = value is not last
                else:
                    write = now - at >= desc.min_interval_s and _differs(
                        key, value, last
                    )
            if write:
                self._last[(scope_id, key)] = (value, now)
                changed[key] = value
        return changed

    def forget(self, scope_id: str) -> None:
        for key in [k for k in self._last if k[0] == scope_id]:
            del self._last[key]


def _dt(raw: Any) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _fl(raw: Any) -> float:
    number = _number(raw)
    return max(number, 0.0) if number is not None else 0.0


def _fl_map(raw: Any) -> dict[str, float]:
    if not isinstance(raw, dict):
        return {}
    return {str(k): _fl(v) for k, v in raw.items()}


def _opt_fl(raw: Any) -> float | None:
    number = _number(raw)
    return None if number is None else max(number, 0.0)


def _int(raw: Any) -> int:
    number = _number(raw)
    return max(int(number), 0) if number is not None else 0


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


class AnalyticsEngine:
    """Computes derived values for all units; keeps counters across restarts."""

    def __init__(self, config: AnalyticsConfig | None = None) -> None:
        """Create an engine with the given (or default) model parameters."""
        self.config = config or AnalyticsConfig()
        self._devices: dict[str, _Device] = {}
        self._gate = _Gate()

    # ------------------------------------------------------------ public API

    def configure(self, config: AnalyticsConfig) -> None:
        """Replace the model parameters (options changed); counters are kept."""
        self.config = config

    def forget(self, serial: str) -> None:
        """Drop a unit's live state and counters (device removed)."""
        self._devices.pop(serial, None)
        self._gate.forget(serial)

    def update(
        self,
        serial: str,
        snapshot: Snapshot,
        now: float,
        wall: datetime,
        outdoor: OutdoorConditions | None = None,
    ) -> Derived:
        """Ingest one snapshot and return the derived values of the unit.

        Call at every coordinator update, on every local direction change (for
        exact cycle statistics) and at least once per minute while a unit runs.
        """
        dev = self._devices.setdefault(serial, _Device())
        dev.zone = snapshot.zone
        self._expire(dev, now)
        self._roll_day(dev, wall)
        self._credit(dev, now)
        if dev.tracking_since is None:
            dev.tracking_since = wall

        phase = ventilation_phase(snapshot)
        self._track_phase(dev, snapshot, phase, now)
        settled, valid = self._validity(dev, snapshot, now, wall)
        self._events(dev, snapshot, now, wall)
        held_t, held_h = self._held(dev, wall)
        moment = self._moment(snapshot, phase, outdoor, held_t, held_h)
        dev.moment = moment
        dev.last_now = now

        context = self._context(phase, settled)
        raw = self._values(
            dev, snapshot, moment, (valid, context), serial, now, wall, outdoor
        )
        dev.raw = raw
        return self._finish(serial, "device", raw, now)

    def zone_values(self, zone: str, now: float) -> Derived:
        """Return the totals of one zone from the units' latest values."""
        devices = [d for d in self._devices.values() if d.zone == zone]
        return self._finish(f"zone:{zone}", "zone", self._totals("zone", devices), now)

    def house_values(
        self, now: float, outdoor: OutdoorConditions | None = None
    ) -> Derived:
        """Return the house totals plus the outdoor derivations."""
        raw = self._totals("house", list(self._devices.values()))
        raw["outdoor_dew_point"] = outdoor.dew_point if outdoor else None
        raw["outdoor_absolute_humidity"] = (
            outdoor.absolute_humidity if outdoor else None
        )
        return self._finish("house", "house", raw, now)

    def reset_filter(self, serial: str, wall: datetime) -> None:
        """Mark the filter as replaced/cleaned now (own reset button)."""
        dev = self._devices.setdefault(serial, _Device())
        self._reset_filter(dev, wall)

    # --------------------------------------------------- command statistics

    def command_sent(self, serial: str, token: Hashable, now: float) -> None:
        """Record that a command left for the cloud."""
        dev = self._devices.setdefault(serial, _Device())
        self._expire(dev, now)
        dev.pending[token] = _Command(sent=now)
        dev.cmd_sent += 1

    def command_confirmed(
        self, serial: str, token: Hashable, now: float, source: str = "cloud"
    ) -> float | None:
        """Record a confirmation (``cloud`` or ``local``); return the latency in s.

        The first confirmation counts the command as confirmed; the second
        source only adds its own latency. Unknown or expired tokens return None.
        """
        dev = self._devices.setdefault(serial, _Device())
        self._expire(dev, now)
        command = dev.pending.get(token)
        if command is None or source in command.sources:
            return None
        command.sources.add(source)
        latency = max(now - command.sent, 0.0)
        if source == "local":
            dev.latency_local = latency
        else:
            dev.latency_cloud = latency
        if not command.confirmed:
            command.confirmed = True
            dev.cmd_confirmed += 1
            dev.latency_last = latency
            dev.latencies.append(latency)
        return latency

    def command_rejected(self, serial: str, token: Hashable, now: float) -> None:
        """Record a rejected command (for example HTTP 406)."""
        dev = self._devices.setdefault(serial, _Device())
        self._expire(dev, now)
        command = dev.pending.pop(token, None)
        if command is not None and command.confirmed:
            dev.cmd_confirmed -= 1
        dev.cmd_rejected += 1

    def expire_commands(self, serial: str, now: float) -> None:
        """Count commands unconfirmed after the timeout (also run by update)."""
        dev = self._devices.get(serial)
        if dev is not None:
            self._expire(dev, now)

    def _expire(self, dev: _Device, now: float) -> None:
        timeout = self.config.command_timeout_s
        for token in [t for t, c in dev.pending.items() if now - c.sent >= timeout]:
            command = dev.pending.pop(token)
            if not command.confirmed:
                dev.cmd_unconfirmed += 1

    # ------------------------------------------------------------ persistence

    def export_state(self) -> dict[str, Any]:
        """Return the persistent counters as a JSON-serialisable dict."""
        devices: dict[str, Any] = {}
        for serial, dev in self._devices.items():
            devices[serial] = {
                "operating_s": dev.operating_s,
                "on_s": dev.on_s,
                "mode_s": dict(dev.mode_s),
                "stage_s": dict(dev.stage_s),
                "direction_s": dict(dev.direction_s),
                "reversals": dev.reversals,
                "volume_m3": dev.volume_m3,
                "energy_kwh": dev.energy_kwh,
                "recovered_kwh": dev.recovered_kwh,
                "loss_kwh": dev.loss_kwh,
                "humidity_alarm_s": dev.humidity_alarm_s,
                "night_alarm_s": dev.night_alarm_s,
                "humidity_today_s": dev.humidity_today_s,
                "night_today_s": dev.night_today_s,
                "today_op_s": dev.today_op_s,
                "today": dev.today,
                "filter_s": dev.filter_s,
                "filter_status": dev.filter_status,
                "filter_reset": _iso(dev.filter_reset),
                "tracking_since": _iso(dev.tracking_since),
                "filter_medium_s": dev.filter_medium_s,
                "filter_bad_s": dev.filter_bad_s,
                "usage": dict(dev.usage),
                "held": {
                    "temperature": dev.held_temperature,
                    "humidity": dev.held_humidity,
                    "at": _iso(dev.held_at),
                },
                "timed_started": _iso(dev.timed_started),
                "commands": {
                    "sent": dev.cmd_sent,
                    "confirmed": dev.cmd_confirmed,
                    "unconfirmed": dev.cmd_unconfirmed,
                    "rejected": dev.cmd_rejected,
                },
            }
        return {"version": STATE_VERSION, "devices": devices}

    def import_state(self, data: Mapping[str, Any] | None) -> None:
        """Restore counters from :meth:`export_state`; bad data is ignored."""
        if not isinstance(data, Mapping) or data.get("version") != STATE_VERSION:
            return
        devices = data.get("devices")
        if not isinstance(devices, Mapping):
            return
        for serial, stored in devices.items():
            if isinstance(stored, Mapping):
                self._devices[str(serial)] = self._import_device(stored)

    @staticmethod
    def _import_device(stored: Mapping[str, Any]) -> _Device:
        dev = _Device()
        dev.operating_s = _fl(stored.get("operating_s"))
        dev.on_s = _fl(stored.get("on_s"))
        dev.mode_s = _fl_map(stored.get("mode_s"))
        dev.stage_s = _fl_map(stored.get("stage_s"))
        dev.direction_s = _fl_map(stored.get("direction_s"))
        dev.reversals = _int(stored.get("reversals"))
        dev.volume_m3 = _fl(stored.get("volume_m3"))
        dev.energy_kwh = _fl(stored.get("energy_kwh"))
        dev.recovered_kwh = _fl(stored.get("recovered_kwh"))
        dev.loss_kwh = _fl(stored.get("loss_kwh"))
        dev.humidity_alarm_s = _fl(stored.get("humidity_alarm_s"))
        dev.night_alarm_s = _fl(stored.get("night_alarm_s"))
        dev.humidity_today_s = _fl(stored.get("humidity_today_s"))
        dev.night_today_s = _fl(stored.get("night_today_s"))
        dev.today_op_s = _fl(stored.get("today_op_s"))
        today = stored.get("today")
        dev.today = today if isinstance(today, str) else None
        dev.filter_s = _fl(stored.get("filter_s"))
        status = stored.get("filter_status")
        dev.filter_status = status if isinstance(status, str) else None
        dev.filter_reset = _dt(stored.get("filter_reset"))
        dev.tracking_since = _dt(stored.get("tracking_since"))
        dev.filter_medium_s = _opt_fl(stored.get("filter_medium_s"))
        dev.filter_bad_s = _opt_fl(stored.get("filter_bad_s"))
        dev.usage = _fl_map(stored.get("usage"))
        held = stored.get("held")
        if isinstance(held, Mapping):
            dev.held_temperature = _number(held.get("temperature"))
            dev.held_humidity = _number(held.get("humidity"))
            dev.held_at = _dt(held.get("at"))
        dev.timed_started = _dt(stored.get("timed_started"))
        commands = stored.get("commands")
        if isinstance(commands, Mapping):
            dev.cmd_sent = _int(commands.get("sent"))
            dev.cmd_confirmed = _int(commands.get("confirmed"))
            dev.cmd_unconfirmed = _int(commands.get("unconfirmed"))
            dev.cmd_rejected = _int(commands.get("rejected"))
        return dev

    # ------------------------------------------------------------ integration

    def _roll_day(self, dev: _Device, wall: datetime) -> None:
        today = wall.date().isoformat()
        if dev.today != today:
            dev.today = today
            dev.today_op_s = 0.0
            dev.humidity_today_s = 0.0
            dev.night_today_s = 0.0
        cutoff = (wall.date() - timedelta(days=USAGE_KEEP_DAYS)).isoformat()
        for key in [k for k in dev.usage if k < cutoff]:
            del dev.usage[key]

    def _credit(self, dev: _Device, now: float) -> None:
        """Credit the time since the last update to the previous state."""
        moment, last = dev.moment, dev.last_now
        if moment is None or last is None:
            return
        dt = now - last
        if dt <= 0:
            return
        dt = min(dt, self.config.max_gap_s)
        today = dev.today or ""
        if moment.ventilating:
            dev.operating_s += dt
            dev.today_op_s += dt
            if moment.stage:
                dev.stage_s[moment.stage] = dev.stage_s.get(moment.stage, 0.0) + dt
        if moment.power_on:
            dev.on_s += dt
        if moment.mode:
            dev.mode_s[moment.mode] = dev.mode_s.get(moment.mode, 0.0) + dt
        if moment.direction:
            dev.direction_s[moment.direction] = (
                dev.direction_s.get(moment.direction, 0.0) + dt
            )
        basis = (
            moment.power_on
            if self.config.filter_basis == "on_time"
            else moment.ventilating
        )
        if basis:
            dev.filter_s += dt
            dev.usage[today] = dev.usage.get(today, 0.0) + dt
        fraction = moment.flow_fraction
        if moment.airflow is not None:
            dev.volume_m3 += moment.airflow * fraction * dt / 3600.0
        if moment.power_w is not None:
            dev.energy_kwh += moment.power_w * dt / 3.6e6
        if moment.recovered_w is not None:
            dev.recovered_kwh += moment.recovered_w * fraction * dt / 3.6e6
        if moment.loss_w is not None:
            dev.loss_kwh += moment.loss_w * fraction * dt / 3.6e6
        if moment.humidity_alarm:
            dev.humidity_alarm_s += dt
            dev.humidity_today_s += dt
        if moment.night_alarm:
            dev.night_alarm_s += dt
            dev.night_today_s += dt

    # ------------------------------------------------------------ live state

    def _track_phase(
        self, dev: _Device, snap: Snapshot, phase: str | None, now: float
    ) -> None:
        moving = phase in MOVING_PHASES
        if moving:
            if dev.vent_since is None:
                dev.vent_since = now
                if dev.damper_open_since is None:
                    dev.damper_open_since = now
            dev.stop_since = None
        else:
            dev.vent_since = None
            if phase == "stopped":
                dev.damper_open_since = None
                if dev.stop_since is None:
                    dev.stop_since = now
        # first observation fixes the damper at its steady state
        if dev.last_now is None:
            if moving:
                dev.damper_open_since = now - self.config.damper_open_s
            elif phase == "stopped":
                dev.stop_since = now - self.config.damper_close_s

        direction = snap.direction
        if direction != dev.last_dir:
            dev.dir_since = now
            dev.last_dir = direction
        if direction in ("exhaust", "intake"):
            if dev.last_moving_dir is not None and direction != dev.last_moving_dir:
                if snap.reversals is None:
                    dev.reversals += 1
                if dev.last_reversal is not None:
                    interval = now - dev.last_reversal
                    if 0 < interval <= 4 * HALF_CYCLE_S:
                        dev.half_cycles.append(interval)
                dev.last_reversal = now
            dev.last_moving_dir = direction
        if snap.reversals is not None:
            if dev.ext_reversals is not None:
                delta = snap.reversals - dev.ext_reversals
                dev.reversals += delta if delta >= 0 else snap.reversals
            dev.ext_reversals = snap.reversals

    def _validity(
        self, dev: _Device, snap: Snapshot, now: float, wall: datetime
    ) -> tuple[bool, bool]:
        settled = dev.vent_since is not None and (
            now - dev.vent_since >= self.config.settle_s
        )
        has_reading = snap.temperature is not None or snap.humidity is not None
        valid = settled and has_reading
        if valid:
            if psy.plausible(snap.temperature, 0.0):
                dev.held_temperature = snap.temperature
            if psy.plausible(0.0, snap.humidity):
                dev.held_humidity = snap.humidity
            dev.held_at = wall
        return settled, valid

    def _held(self, dev: _Device, wall: datetime) -> tuple[float | None, float | None]:
        if dev.held_at is None:
            return None, None
        if (wall - dev.held_at).total_seconds() > self.config.hold_max_age_s:
            return None, None
        return dev.held_temperature, dev.held_humidity

    def _events(self, dev: _Device, snap: Snapshot, now: float, wall: datetime) -> None:
        status = snap.filter_status
        if status is not None:
            previous = dev.filter_status
            if previous in ("Medium", "Bad") and status == "Good":
                self._reset_filter(dev, wall)
            if previous is not None:
                if status == "Medium" and dev.filter_medium_s is None:
                    dev.filter_medium_s = dev.filter_s
                if status == "Bad" and dev.filter_bad_s is None:
                    dev.filter_bad_s = dev.filter_s
            dev.filter_status = status
        dev.hum_alarm_since = self._episode(
            dev.hum_alarm_since, snap.humidity_alarm, now
        )
        dev.night_alarm_since = self._episode(
            dev.night_alarm_since, snap.night_alarm, now
        )
        if snap.operating_mode == "TimedExpulsion" and snap.power is not False:
            if dev.timed_started is None:
                dev.timed_started = wall
        elif snap.operating_mode is not None:
            dev.timed_started = None

    @staticmethod
    def _episode(since: float | None, active: bool | None, now: float) -> float | None:
        if active:
            return now if since is None else since
        return None if active is False else since

    @staticmethod
    def _reset_filter(dev: _Device, wall: datetime) -> None:
        dev.filter_s = 0.0
        dev.filter_reset = wall
        dev.filter_medium_s = None
        dev.filter_bad_s = None

    def _moment(
        self,
        snap: Snapshot,
        phase: str | None,
        outdoor: OutdoorConditions | None,
        held_t: float | None,
        held_h: float | None,
    ) -> _Moment:
        cfg = self.config
        ventilating = None if phase is None else phase in MOVING_PHASES
        stage = effective_stage(snap)
        cyclic = is_cyclic(snap)
        moment = _Moment(
            phase=phase,
            ventilating=ventilating,
            power_on=snap.power
            if snap.power is not None
            else (True if ventilating else None),
            mode=snap.operating_mode,
            stage=stage,
            humidity_alarm=bool(snap.humidity_alarm),
            night_alarm=bool(snap.night_alarm),
        )
        if phase in ("exhaust", "intake"):
            moment.direction = phase
        elif snap.direction == "stopped":
            moment.direction = "stopped"
        if phase in ("exhaust", "intake"):
            moment.flow_fraction = 1.0
        elif phase == "running":
            moment.flow_fraction = CYCLE_DUTY if cyclic else 1.0

        if ventilating is False:
            moment.airflow = 0.0
            moment.fresh = 0.0
        elif ventilating and stage is not None:
            moment.airflow = cfg.airflow.get(stage)
            if moment.airflow is not None:
                moment.fresh = moment.airflow * (cfg.supply_share if cyclic else 1.0)
        # power
        if ventilating is False:
            moment.power_w = cfg.standby_w
        elif ventilating and stage is not None and stage in cfg.power_w:
            moment.power_w = cfg.power_w[stage] * cfg.power_scale
        # heat and moisture need an outdoor source and a valid room reading
        if outdoor is not None and held_t is not None:
            self._heat(moment, outdoor, held_t, held_h, cyclic)
        return moment

    def _heat(
        self,
        moment: _Moment,
        outdoor: OutdoorConditions,
        held_t: float,
        held_h: float | None,
        cyclic: bool,
    ) -> None:
        if moment.ventilating is False:
            moment.recovered_w = moment.loss_w = moment.moisture_gh = 0.0
            return
        if not moment.ventilating or moment.fresh is None or moment.stage is None:
            return
        delta = held_t - outdoor.temperature
        eta = self.config.heat_eta.get(moment.stage, 0.0) if cyclic else 0.0
        if delta <= 0:
            moment.recovered_w = moment.loss_w = 0.0
        else:
            total = psy.sensible_power(moment.fresh, delta)
            moment.recovered_w = eta * total
            moment.loss_w = (1.0 - eta) * total
        eps = self.config.moisture_eta.get(moment.stage) if cyclic else None
        indoor_ah = psy.absolute_humidity(held_t, held_h)
        outdoor_ah = outdoor.absolute_humidity
        if eps is not None and indoor_ah is not None and outdoor_ah is not None:
            moment.moisture_gh = eps * moment.fresh * max(indoor_ah - outdoor_ah, 0.0)

    # ------------------------------------------------------------ outputs

    def _values(
        self,
        dev: _Device,
        snap: Snapshot,
        moment: _Moment,
        validity: tuple[bool, str],
        serial: str,
        now: float,
        wall: datetime,
        outdoor: OutdoorConditions | None,
    ) -> dict[str, Any]:
        cfg = self.config
        valid, context = validity
        out: dict[str, Any] = {}
        hours = 1.0 / 3600.0
        # F4
        out["operating_hours"] = dev.operating_s * hours
        out["on_hours"] = dev.on_s * hours
        out["operating_hours_today"] = dev.today_op_s * hours
        for mode, slug in MODE_SLUGS.items():
            out[f"runtime_mode_{slug}"] = dev.mode_s.get(mode, 0.0) * hours
        for stage in STAGES:
            out[f"runtime_stage_{stage}"] = dev.stage_s.get(stage, 0.0) * hours
        for direction in DIRECTIONS:
            out[f"runtime_direction_{direction}"] = (
                dev.direction_s.get(direction, 0.0) * hours
            )
        out["reversals"] = dev.reversals
        out["heat_recovery_cycles"] = dev.reversals // 2
        out["half_cycle_duration"] = (
            statistics.fmean(dev.half_cycles) if dev.half_cycles else None
        )
        # F5 / F6
        out["airflow"] = moment.airflow
        out["fresh_air_flow"] = moment.fresh
        out["air_volume"] = dev.volume_m3
        volume = cfg.room_volumes.get(serial)
        out["air_change_rate"] = (
            moment.fresh / volume if volume and moment.fresh is not None else None
        )
        out["power_estimate"] = moment.power_w
        out["energy_estimate"] = dev.energy_kwh
        # F7
        out["heat_recovered_power"] = moment.recovered_w
        out["heat_recovered_energy"] = dev.recovered_kwh
        out["ventilation_loss_power"] = moment.loss_w
        out["ventilation_loss_energy"] = dev.loss_kwh
        out["moisture_recovered"] = moment.moisture_gh
        # F8 / F12
        held_t, held_h = self._held(dev, wall)
        indoor_ah = psy.absolute_humidity(held_t, held_h)
        out["dew_point"] = psy.dew_point(held_t, held_h)
        out["absolute_humidity"] = indoor_ah
        setpoint = self._setpoint(snap)
        out["humidity_setpoint"] = setpoint
        out["humidity_setpoint_distance"] = (
            held_h - setpoint if held_h is not None and setpoint is not None else None
        )
        out["comfort"] = comfort_class(held_h) if valid and held_h is not None else None
        out["room_temperature"] = held_t
        out["room_humidity"] = held_h
        out["measurement_valid"] = valid
        out["measurement_context"] = context
        out["measurement_last_valid"] = dev.held_at
        potential = drying_potential(indoor_ah, outdoor)
        out["drying_potential"] = potential
        out["outdoor_humidifies"] = would_humidify(potential, cfg.humidify_margin)
        # F9
        quality = snap.air_quality
        out["air_quality_index"] = AIR_QUALITY_INDEX.get(quality) if quality else None
        out["air_quality_raw"] = quality
        # F10
        out.update(self._filter_values(dev, snap, wall))
        # F11 / F19
        out.update(self._automatics(dev, snap, moment, now, wall))
        # F17
        out.update(self._alarms(dev, now))
        # F18
        out.update(self._commands(dev))
        return out

    def _setpoint(self, snap: Snapshot) -> float | None:
        if snap.operating_mode == "AwayHome":
            return self.config.away_setpoint
        if snap.humidity_level is None:
            return None
        return self.config.setpoints.get(snap.humidity_level)

    @staticmethod
    def _context(phase: str | None, settled: bool) -> str:
        if phase is None:
            return "unknown"
        if phase == "stopped":
            return "stopped"
        return phase if settled else "settling"

    def _filter_values(
        self, dev: _Device, snap: Snapshot, wall: datetime
    ) -> dict[str, Any]:
        limit_s = self.config.filter_limit_h * 3600.0
        hours = dev.filter_s / 3600.0
        remaining_s = max(limit_s - dev.filter_s, 0.0)
        bad = snap.filter_status == "Bad" or dev.filter_status == "Bad"
        base = dev.filter_reset or dev.tracking_since
        predicted: datetime | None = None
        if not bad and dev.tracking_since is not None:
            predicted = self._predict(dev, wall, remaining_s)
        return {
            "filter_hours": hours,
            "filter_remaining_hours": 0.0 if bad else remaining_s / 3600.0,
            "filter_alarm_predicted": predicted,
            "filter_clean_due": base + timedelta(days=91) if base else None,
            "filter_replace_due": base + timedelta(days=730) if base else None,
            "filter_last_reset": dev.filter_reset,
            "filter_medium_hours": (
                None if dev.filter_medium_s is None else dev.filter_medium_s / 3600.0
            ),
            "filter_alarm_hours": (
                None if dev.filter_bad_s is None else dev.filter_bad_s / 3600.0
            ),
        }

    @staticmethod
    def _predict(dev: _Device, wall: datetime, remaining_s: float) -> datetime | None:
        assert dev.tracking_since is not None
        if remaining_s <= 0:
            return wall
        first = wall.date() - timedelta(days=FILTER_USAGE_DAYS - 1)
        used = sum(
            seconds for day, seconds in dev.usage.items() if first.isoformat() <= day
        )
        covered = min(
            (wall - dev.tracking_since).total_seconds(), FILTER_USAGE_DAYS * 86400.0
        )
        if covered < 3600.0 or used <= 0:
            return None
        return wall + timedelta(seconds=remaining_s / (used / covered))

    def _automatics(
        self,
        dev: _Device,
        snap: Snapshot,
        moment: _Moment,
        now: float,
        wall: datetime,
    ) -> dict[str, Any]:
        cfg = self.config
        mode = snap.operating_mode
        ventilating = moment.ventilating
        out: dict[str, Any] = {}
        # humidity exhaust: alarm in Auto/Surveillance, constant exhaust
        if snap.humidity_alarm is None or mode is None:
            humidity_exhaust: bool | None = None
        elif snap.power is False or not (
            snap.humidity_alarm and mode in ("Auto", "Surveillance")
        ):
            humidity_exhaust = False
        elif snap.coupling is not None and snap.direction is not None:
            humidity_exhaust = snap.coupling == "same" and snap.direction == "exhaust"
        else:
            humidity_exhaust = True
        out["humidity_exhaust_active"] = humidity_exhaust
        # night reduction: the automatic modes dropped to the night stage
        if mode is None:
            out["night_reduction_active"] = None
        elif mode not in ("Smart", "Auto"):
            out["night_reduction_active"] = False
        elif ventilating is None or moment.stage is None:
            out["night_reduction_active"] = None
        else:
            out["night_reduction_active"] = ventilating and moment.stage == "night"
        # timed exhaust
        if mode is None:
            out["timed_exhaust_active"] = None
        else:
            out["timed_exhaust_active"] = (
                mode == "TimedExpulsion" and snap.power is not False
            )
        out["timed_exhaust_ends"] = (
            dev.timed_started + timedelta(seconds=cfg.timed_exhaust_s)
            if dev.timed_started is not None and out["timed_exhaust_active"]
            else None
        )
        # Smart behaviour
        behaviour = self._smart(dev, snap, moment, now)
        out["smart_behaviour"] = behaviour
        if mode is None:
            out["free_cooling_active"] = None
        elif mode != "Smart":
            out["free_cooling_active"] = False
        else:
            out["free_cooling_active"] = (
                None if behaviour is None else behaviour == "free_cooling"
            )
        out["damper_state"] = self._damper(dev, moment, now)
        return out

    def _smart(
        self, dev: _Device, snap: Snapshot, moment: _Moment, now: float
    ) -> str | None:
        if snap.operating_mode != "Smart" or moment.ventilating is None:
            return None
        if not moment.ventilating:
            return "idle"
        if snap.coupling == "same":
            return "free_cooling"
        if snap.coupling != "opposite":
            return None
        if (
            snap.direction in ("exhaust", "intake")
            and dev.dir_since is not None
            and now - dev.dir_since > FLOW_PATTERN_S
        ):
            return "free_cooling"
        return "night_heat_recovery" if moment.stage == "night" else "heat_recovery"

    def _damper(self, dev: _Device, moment: _Moment, now: float) -> str | None:
        if moment.ventilating is None:
            return None
        if moment.ventilating:
            since = dev.damper_open_since if dev.damper_open_since is not None else now
            return "opening" if now - since < self.config.damper_open_s else "open"
        since = dev.stop_since if dev.stop_since is not None else now
        return "closing" if now - since < self.config.damper_close_s else "closed"

    def _alarms(self, dev: _Device, now: float) -> dict[str, Any]:
        def current(since: float | None) -> float:
            return 0.0 if since is None else max(now - since, 0.0) / 60.0

        return {
            "humidity_alarm_duration": dev.humidity_alarm_s / 3600.0,
            "humidity_alarm_today": dev.humidity_today_s / 60.0,
            "humidity_alarm_current": current(dev.hum_alarm_since),
            "night_alarm_duration": dev.night_alarm_s / 3600.0,
            "night_alarm_today": dev.night_today_s / 60.0,
            "night_alarm_current": current(dev.night_alarm_since),
        }

    @staticmethod
    def _commands(dev: _Device) -> dict[str, Any]:
        return {
            "command_latency": dev.latency_last,
            "command_latency_cloud": dev.latency_cloud,
            "command_latency_local": dev.latency_local,
            "command_latency_average": (
                statistics.fmean(dev.latencies) if dev.latencies else None
            ),
            "commands_sent": dev.cmd_sent,
            "commands_confirmed": dev.cmd_confirmed,
            "commands_unconfirmed": dev.cmd_unconfirmed,
            "commands_rejected": dev.cmd_rejected,
        }

    # ------------------------------------------------------------ totals

    @staticmethod
    def _totals(scope: str, devices: list[_Device]) -> dict[str, Any]:
        def total(key: str) -> float | None:
            values = [d.raw[key] for d in devices if d.raw.get(key) is not None]
            return sum(values) if values else None

        return {
            f"{scope}_airflow": total("airflow"),
            f"{scope}_power": total("power_estimate"),
            f"{scope}_energy": total("energy_estimate"),
            f"{scope}_air_volume": total("air_volume"),
            f"{scope}_running_units": sum(
                1 for d in devices if d.moment is not None and d.moment.ventilating
            ),
            f"{scope}_units": len(devices),
            f"{scope}_heat_recovered_power": total("heat_recovered_power"),
            f"{scope}_heat_recovered_energy": total("heat_recovered_energy"),
        }

    def _finish(
        self, scope_id: str, scope: str, raw: Mapping[str, Any], now: float
    ) -> Derived:
        values: dict[str, Any] = {}
        for key in SCOPE_KEYS[scope]:
            values[key] = _round(key, raw.get(key))
        for key, value in raw.items():
            if key not in BY_KEY:
                values[key] = value
        changed = self._gate.filter(scope_id, values, now)
        return Derived(values, changed)
