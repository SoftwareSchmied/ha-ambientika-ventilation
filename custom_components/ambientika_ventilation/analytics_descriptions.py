"""Declarative description of every derived value of the analytics engine.

One :class:`DerivedDescription` per derived sensor: key, unit, classes, enabled
default, recorder budget (rounding, change threshold, minimum write interval)
and the English and German names. The Home Assistant platforms build their
entities from :data:`DESCRIPTIONS`; ``analytics.AnalyticsEngine`` produces a
value for exactly these keys. Nothing here imports Home Assistant, device
classes and units are plain strings that match the Home Assistant constants.

Recorder semantics (C6): the engine rounds a value to ``precision`` decimals and
only reports it as changed when it moved by at least ``min_change`` since the
last reported value and at least ``min_interval_s`` have passed. Availability
changes (value to None and back) are always reported at once.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, Literal

Scope = Literal["device", "zone", "house"]
Platform = Literal["sensor", "binary_sensor"]

SENSOR: Final = "sensor"
BINARY: Final = "binary_sensor"
DIAGNOSTIC: Final = "diagnostic"

MEASUREMENT: Final = "measurement"
TOTAL_INCREASING: Final = "total_increasing"

REQ_OUTDOOR: Final = "outdoor"
"""Create only when an outdoor source is configured (F13)."""
REQ_ROOM_VOLUME: Final = "room_volume"
"""Create only when a room volume is configured for the unit."""
REQ_UDP: Final = "udp"
"""Meaningful only with the local UDP source (direction/stage known)."""


@dataclass(frozen=True, slots=True)
class DerivedDescription:
    """Static description of one derived entity."""

    key: str
    name_en: str
    name_de: str
    scope: Scope = "device"
    platform: Platform = SENSOR
    unit: str | None = None
    device_class: str | None = None
    state_class: str | None = None
    enabled_default: bool = True
    recorded: bool = True
    """Intended for long-term history. False: high-churn or diagnostic, carries
    no state class (no long-term statistics) and is disabled by default."""
    precision: int | None = None
    """Decimals of the published value (None: not numeric)."""
    min_change: float = 0.0
    """Minimum change to the last reported value. Seconds for timestamps."""
    min_interval_s: float = 0.0
    """Minimum time between two reported changes of this value."""
    entity_category: str | None = None
    estimate: bool = False
    """Value is a model-based estimate; the entity must say so."""
    options: tuple[str, ...] = ()
    """Allowed states of an enum sensor."""
    attributes: tuple[str, ...] = ()
    """Extra values (engine keys) to expose as state attributes."""
    requires: tuple[str, ...] = ()
    """Preconditions (``REQ_*``) for creating the entity."""


MODE_SLUGS: Final = MappingProxyType(
    {
        "Smart": "smart",
        "Auto": "auto",
        "ManualHeatRecovery": "heat_recovery",
        "Night": "night",
        "AwayHome": "away",
        "Surveillance": "surveillance",
        "TimedExpulsion": "timed_extraction",
        "Expulsion": "extraction",
        "Intake": "intake",
        "MasterSlaveFlow": "master_slave_flow",
        "SlaveMasterFlow": "slave_master_flow",
        "Off": "off",
        "Turbo": "turbo",
    }
)
_MODE_DE: Final = {
    "smart": "Smart",
    "auto": "Auto",
    "heat_recovery": "Wärmerückgewinnung",
    "night": "Nacht",
    "away": "Abwesend",
    "surveillance": "Überwachung",
    "timed_extraction": "Zeitgesteuerte Abluft",
    "extraction": "Abluft",
    "intake": "Zuluft",
    "master_slave_flow": "Fluss Master zu Slave",
    "slave_master_flow": "Fluss Slave zu Master",
    "off": "Aus",
    "turbo": "Turbo",
}
_MODE_EN: Final = {
    "smart": "Smart",
    "auto": "Auto",
    "heat_recovery": "Heat recovery",
    "night": "Night",
    "away": "Away",
    "surveillance": "Surveillance",
    "timed_extraction": "Timed extraction",
    "extraction": "Extraction",
    "intake": "Intake",
    "master_slave_flow": "Master to slave flow",
    "slave_master_flow": "Slave to master flow",
    "off": "Off",
    "turbo": "Turbo",
}

STAGES: Final = ("night", "low", "medium", "high", "turbo")
_STAGE_NAMES: Final = {
    "night": ("Night", "Nacht"),
    "low": ("Low", "Niedrig"),
    "medium": ("Medium", "Mittel"),
    "high": ("High", "Hoch"),
    "turbo": ("Turbo", "Turbo"),
}
DIRECTIONS: Final = ("exhaust", "intake", "stopped")
_DIRECTION_NAMES: Final = {
    "exhaust": ("exhaust", "Abluft"),
    "intake": ("intake", "Zuluft"),
    "stopped": ("stopped or reversing", "Stillstand oder Umkehr"),
}

CONTEXT_OPTIONS: Final = (
    "stopped",
    "settling",
    "exhaust",
    "intake",
    "reversing",
    "running",
    "unknown",
)
COMFORT_OPTIONS: Final = ("too_dry", "dry", "comfortable", "humid", "too_humid")
SMART_OPTIONS: Final = ("idle", "heat_recovery", "night_heat_recovery", "free_cooling")
DAMPER_OPTIONS: Final = ("closed", "opening", "open", "closing")

AIR_QUALITY_INDEX: Final = MappingProxyType(
    {"VeryGood": 1, "Good": 2, "Medium": 3, "Poor": 4, "Bad": 5}
)
"""Ordinal of the air quality enum: 1 = very good ... 5 = bad (F9)."""


def _hours(
    key: str,
    en: str,
    de: str,
    *,
    enabled: bool = False,
    interval: float = 600.0,
    **extra: object,
) -> DerivedDescription:
    return DerivedDescription(
        key,
        en,
        de,
        unit="h",
        device_class="duration",
        state_class=TOTAL_INCREASING,
        enabled_default=enabled,
        precision=2,
        min_change=0.01,
        min_interval_s=interval,
        **extra,  # type: ignore[arg-type]
    )


def _counter(
    key: str, en: str, de: str, *, scope: Scope = "device", **extra: object
) -> DerivedDescription:
    fields: dict[str, object] = {
        "state_class": TOTAL_INCREASING,
        "enabled_default": False,
        "precision": 0,
        "min_change": 1,
        "min_interval_s": 60.0,
    }
    fields.update(extra)
    return DerivedDescription(key, en, de, scope=scope, **fields)  # type: ignore[arg-type]


def _binary(key: str, en: str, de: str, **extra: object) -> DerivedDescription:
    return DerivedDescription(
        key,
        en,
        de,
        platform=BINARY,
        **extra,  # type: ignore[arg-type]
    )


def _build() -> tuple[DerivedDescription, ...]:
    d: list[DerivedDescription] = []

    # F4 runtimes and cycles
    d.append(
        _hours(
            "operating_hours",
            "Operating hours",
            "Betriebsstunden",
            enabled=True,
            interval=300.0,
            entity_category=DIAGNOSTIC,
        )
    )
    d.append(
        _hours(
            "on_hours",
            "Powered-on hours",
            "Einschaltdauer",
            interval=300.0,
            entity_category=DIAGNOSTIC,
        )
    )
    d.append(
        DerivedDescription(
            "operating_hours_today",
            "Operating hours today",
            "Betriebsstunden heute",
            unit="h",
            device_class="duration",
            enabled_default=False,
            recorded=False,
            precision=2,
            min_change=0.05,
            min_interval_s=600.0,
            entity_category=DIAGNOSTIC,
        )
    )
    for mode, slug in MODE_SLUGS.items():
        if mode == "Off":
            en, de = "Off time", "Ausgeschaltet-Dauer"
        else:
            en = f"Runtime {_MODE_EN[slug]}"
            de = f"Laufzeit {_MODE_DE[slug]}"
        d.append(_hours(f"runtime_mode_{slug}", en, de, entity_category=DIAGNOSTIC))
    for stage in STAGES:
        en_n, de_n = _STAGE_NAMES[stage]
        d.append(
            _hours(
                f"runtime_stage_{stage}",
                f"Runtime stage {en_n}",
                f"Laufzeit Stufe {de_n}",
                entity_category=DIAGNOSTIC,
            )
        )
    for direction in DIRECTIONS:
        en_n, de_n = _DIRECTION_NAMES[direction]
        d.append(
            _hours(
                f"runtime_direction_{direction}",
                f"Runtime {en_n}",
                f"Laufzeit {de_n}",
                requires=(REQ_UDP,),
                entity_category=DIAGNOSTIC,
            )
        )
    d.append(
        _counter(
            "reversals",
            "Direction reversals",
            "Richtungswechsel",
            requires=(REQ_UDP,),
            min_interval_s=300.0,
            entity_category=DIAGNOSTIC,
        )
    )
    d.append(
        _counter(
            "heat_recovery_cycles",
            "Heat recovery cycles",
            "Wärmerückgewinnungs-Zyklen",
            requires=(REQ_UDP,),
            min_interval_s=300.0,
            entity_category=DIAGNOSTIC,
        )
    )
    d.append(
        DerivedDescription(
            "half_cycle_duration",
            "Half cycle duration",
            "Halbzyklus-Dauer",
            unit="s",
            device_class="duration",
            enabled_default=False,
            recorded=False,
            precision=0,
            min_change=2.0,
            min_interval_s=300.0,
            entity_category=DIAGNOSTIC,
            requires=(REQ_UDP,),
        )
    )

    # F5 air flow
    d.append(
        DerivedDescription(
            "airflow",
            "Airflow",
            "Luftmenge",
            unit="m³/h",
            device_class="volume_flow_rate",
            state_class=MEASUREMENT,
            precision=0,
            min_change=1.0,
            min_interval_s=60.0,
            estimate=True,
        )
    )
    d.append(
        DerivedDescription(
            "fresh_air_flow",
            "Fresh air flow",
            "Frischluftmenge",
            unit="m³/h",
            device_class="volume_flow_rate",
            state_class=MEASUREMENT,
            enabled_default=False,
            precision=0,
            min_change=1.0,
            min_interval_s=60.0,
            estimate=True,
        )
    )
    d.append(
        DerivedDescription(
            "air_volume",
            "Air volume moved",
            "Bewegtes Luftvolumen",
            unit="m³",
            device_class="volume",
            state_class=TOTAL_INCREASING,
            precision=1,
            min_change=0.1,
            min_interval_s=60.0,
            estimate=True,
        )
    )
    d.append(
        DerivedDescription(
            "air_change_rate",
            "Air change rate",
            "Luftwechselrate",
            unit="1/h",
            state_class=MEASUREMENT,
            precision=2,
            min_change=0.05,
            min_interval_s=60.0,
            estimate=True,
            requires=(REQ_ROOM_VOLUME,),
        )
    )

    # F6 power and energy
    d.append(
        DerivedDescription(
            "power_estimate",
            "Estimated power",
            "Geschätzte Leistung",
            unit="W",
            device_class="power",
            state_class=MEASUREMENT,
            precision=1,
            min_change=0.1,
            min_interval_s=60.0,
            estimate=True,
        )
    )
    d.append(
        DerivedDescription(
            "energy_estimate",
            "Estimated energy",
            "Geschätzte Energie",
            unit="kWh",
            device_class="energy",
            state_class=TOTAL_INCREASING,
            precision=3,
            min_change=0.001,
            min_interval_s=60.0,
            estimate=True,
        )
    )

    # F7 heat (outdoor source needed)
    for key, en, de in (
        ("heat_recovered", "Recovered heat", "Zurückgewonnene Wärme"),
        ("ventilation_loss", "Ventilation heat loss", "Lüftungswärmeverlust"),
    ):
        d.append(
            DerivedDescription(
                f"{key}_power",
                f"{en} power",
                f"{de} Leistung",
                unit="W",
                device_class="power",
                state_class=MEASUREMENT,
                precision=0,
                min_change=1.0,
                min_interval_s=60.0,
                estimate=True,
                requires=(REQ_OUTDOOR,),
            )
        )
        d.append(
            DerivedDescription(
                f"{key}_energy",
                f"{en} energy",
                f"{de} Energie",
                unit="kWh",
                device_class="energy",
                state_class=TOTAL_INCREASING,
                precision=3,
                min_change=0.001,
                min_interval_s=60.0,
                estimate=True,
                requires=(REQ_OUTDOOR,),
            )
        )
    d.append(
        DerivedDescription(
            "moisture_recovered",
            "Recovered moisture",
            "Zurückgewonnene Feuchte",
            unit="g/h",
            enabled_default=False,
            recorded=False,
            precision=0,
            min_change=5.0,
            min_interval_s=300.0,
            estimate=True,
            requires=(REQ_OUTDOOR,),
        )
    )

    # F8 humidity and room climate
    d.append(
        DerivedDescription(
            "dew_point",
            "Dew point",
            "Taupunkt",
            unit="°C",
            device_class="temperature",
            state_class=MEASUREMENT,
            precision=1,
            min_change=0.1,
            min_interval_s=60.0,
        )
    )
    d.append(
        DerivedDescription(
            "absolute_humidity",
            "Absolute humidity",
            "Absolute Feuchte",
            unit="g/m³",
            device_class="absolute_humidity",
            state_class=MEASUREMENT,
            precision=1,
            min_change=0.1,
            min_interval_s=60.0,
        )
    )
    d.append(
        DerivedDescription(
            "humidity_setpoint",
            "Humidity setpoint",
            "Feuchte-Sollwert",
            unit="%",
            device_class="humidity",
            recorded=False,
            precision=0,
            min_change=1.0,
        )
    )
    d.append(
        DerivedDescription(
            "humidity_setpoint_distance",
            "Humidity distance to setpoint",
            "Feuchte-Abstand zum Sollwert",
            unit="%",
            state_class=MEASUREMENT,
            precision=0,
            min_change=1.0,
            min_interval_s=60.0,
        )
    )
    d.append(
        DerivedDescription(
            "comfort",
            "Humidity comfort",
            "Feuchtekomfort",
            device_class="enum",
            options=COMFORT_OPTIONS,
            min_interval_s=60.0,
        )
    )
    d.append(
        DerivedDescription(
            "room_temperature",
            "Room temperature (last valid)",
            "Raumtemperatur (zuletzt gültig)",
            unit="°C",
            device_class="temperature",
            state_class=MEASUREMENT,
            precision=0,
            min_change=1.0,
            min_interval_s=60.0,
            attributes=("measurement_context",),
        )
    )
    d.append(
        DerivedDescription(
            "room_humidity",
            "Room humidity (last valid)",
            "Raumfeuchte (zuletzt gültig)",
            unit="%",
            device_class="humidity",
            state_class=MEASUREMENT,
            precision=0,
            min_change=1.0,
            min_interval_s=60.0,
            attributes=("measurement_context",),
        )
    )
    d.append(
        _binary(
            "measurement_valid",
            "Measurement representative",
            "Messung repräsentativ",
            min_interval_s=0.0,
        )
    )
    d.append(
        DerivedDescription(
            "measurement_context",
            "Measurement context",
            "Messkontext",
            device_class="enum",
            options=CONTEXT_OPTIONS,
            enabled_default=False,
            recorded=False,
            entity_category=DIAGNOSTIC,
        )
    )
    d.append(
        DerivedDescription(
            "measurement_last_valid",
            "Last valid measurement",
            "Letzte gültige Messung",
            device_class="timestamp",
            enabled_default=False,
            recorded=False,
            min_change=60.0,
            min_interval_s=60.0,
            entity_category=DIAGNOSTIC,
        )
    )
    d.append(
        DerivedDescription(
            "drying_potential",
            "Drying potential",
            "Trocknungspotenzial",
            unit="g/m³",
            state_class=MEASUREMENT,
            precision=1,
            min_change=0.1,
            min_interval_s=60.0,
            requires=(REQ_OUTDOOR,),
        )
    )
    d.append(
        _binary(
            "outdoor_humidifies",
            "Outdoor air would humidify",
            "Außenluft würde befeuchten",
            device_class="problem",
            min_interval_s=60.0,
            requires=(REQ_OUTDOOR,),
        )
    )

    # F9 air quality
    d.append(
        DerivedDescription(
            "air_quality_index",
            "Air quality index",
            "Luftqualitätsindex",
            state_class=MEASUREMENT,
            precision=0,
            min_change=1.0,
            min_interval_s=60.0,
            attributes=("air_quality_raw",),
        )
    )

    # F10 filter
    d.append(
        DerivedDescription(
            "filter_hours",
            "Filter operating hours",
            "Filter-Betriebsstunden",
            unit="h",
            device_class="duration",
            state_class=TOTAL_INCREASING,
            precision=1,
            min_change=0.1,
            min_interval_s=600.0,
            entity_category=DIAGNOSTIC,
        )
    )
    d.append(
        DerivedDescription(
            "filter_remaining_hours",
            "Filter remaining hours",
            "Filter-Restlaufzeit",
            unit="h",
            device_class="duration",
            state_class=MEASUREMENT,
            precision=0,
            min_change=1.0,
            min_interval_s=600.0,
            estimate=True,
            entity_category=DIAGNOSTIC,
        )
    )
    d.append(
        DerivedDescription(
            "filter_alarm_predicted",
            "Filter alarm expected on",
            "Filteralarm voraussichtlich am",
            device_class="timestamp",
            recorded=False,
            min_change=21600.0,
            min_interval_s=3600.0,
            estimate=True,
            entity_category=DIAGNOSTIC,
        )
    )
    for key, en, de in (
        ("filter_clean_due", "Filter cleaning due", "Filterreinigung fällig"),
        ("filter_replace_due", "Filter replacement due", "Filterwechsel fällig"),
        ("filter_last_reset", "Filter last reset", "Filter zuletzt zurückgesetzt"),
    ):
        d.append(
            DerivedDescription(
                key,
                en,
                de,
                device_class="timestamp",
                enabled_default=False,
                recorded=False,
                min_change=60.0,
                min_interval_s=60.0,
                entity_category=DIAGNOSTIC,
                estimate=key != "filter_last_reset",
            )
        )
    for key, en, de in (
        ("filter_medium_hours", "Filter hours at Medium", "Filterstunden bei Mittel"),
        ("filter_alarm_hours", "Filter hours at alarm", "Filterstunden bei Alarm"),
    ):
        d.append(
            DerivedDescription(
                key,
                en,
                de,
                unit="h",
                device_class="duration",
                enabled_default=False,
                recorded=False,
                precision=1,
                min_change=0.1,
                entity_category=DIAGNOSTIC,
            )
        )

    # F11 hidden automatics, F19 minor derivations
    d.append(
        _binary(
            "humidity_exhaust_active",
            "Humidity exhaust active",
            "Feuchte-Abluft aktiv",
        )
    )
    d.append(
        _binary(
            "night_reduction_active",
            "Night reduction active",
            "Nachtabsenkung aktiv",
            requires=(REQ_UDP,),
        )
    )
    d.append(
        _binary("timed_exhaust_active", "Timed extraction active", "Zeitabluft aktiv")
    )
    d.append(
        _binary(
            "free_cooling_active",
            "Free cooling active",
            "Freie Kühlung aktiv",
            requires=(REQ_UDP,),
        )
    )
    d.append(
        DerivedDescription(
            "smart_behaviour",
            "Smart behaviour",
            "Smart-Verhalten",
            device_class="enum",
            options=SMART_OPTIONS,
            requires=(REQ_UDP,),
        )
    )
    d.append(
        DerivedDescription(
            "timed_exhaust_ends",
            "Timed extraction ends",
            "Zeitabluft endet",
            device_class="timestamp",
            enabled_default=False,
            recorded=False,
            min_change=60.0,
            estimate=True,
        )
    )
    d.append(
        DerivedDescription(
            "damper_state",
            "Damper state",
            "Klappenzustand",
            device_class="enum",
            options=DAMPER_OPTIONS,
            enabled_default=False,
            recorded=False,
            estimate=True,
            entity_category=DIAGNOSTIC,
        )
    )

    # F17 alarm durations
    for prefix, en, de in (
        ("humidity_alarm", "Humidity alarm", "Feuchtealarm"),
        ("night_alarm", "Night alarm", "Nachtalarm"),
    ):
        d.append(
            _hours(
                f"{prefix}_duration",
                f"{en} duration",
                f"{de} Dauer",
                interval=300.0,
                entity_category=DIAGNOSTIC,
            )
        )
        for suffix, en_s, de_s in (
            ("today", "today", "heute"),
            ("current", "current", "aktuell"),
        ):
            d.append(
                DerivedDescription(
                    f"{prefix}_{suffix}",
                    f"{en} {en_s}",
                    f"{de} {de_s}",
                    unit="min",
                    device_class="duration",
                    enabled_default=False,
                    recorded=False,
                    precision=0,
                    min_change=1.0,
                    min_interval_s=60.0,
                    entity_category=DIAGNOSTIC,
                )
            )

    # F18 commands
    d.append(
        DerivedDescription(
            "command_latency",
            "Last command latency",
            "Letzte Befehlslatenz",
            unit="s",
            device_class="duration",
            enabled_default=False,
            recorded=False,
            precision=1,
            min_change=0.1,
            entity_category=DIAGNOSTIC,
        )
    )
    for key, en, de in (
        ("command_latency_cloud", "Command latency (cloud)", "Befehlslatenz (Cloud)"),
        ("command_latency_local", "Command latency (local)", "Befehlslatenz (lokal)"),
        (
            "command_latency_average",
            "Average command latency",
            "Mittlere Befehlslatenz",
        ),
    ):
        d.append(
            DerivedDescription(
                key,
                en,
                de,
                unit="s",
                device_class="duration",
                enabled_default=False,
                recorded=False,
                precision=1,
                min_change=0.1,
                entity_category=DIAGNOSTIC,
            )
        )
    for key, en, de in (
        ("commands_sent", "Commands sent", "Gesendete Befehle"),
        ("commands_confirmed", "Commands confirmed", "Bestätigte Befehle"),
        ("commands_unconfirmed", "Commands unconfirmed", "Unbestätigte Befehle"),
        ("commands_rejected", "Commands rejected", "Abgelehnte Befehle"),
    ):
        d.append(_counter(key, en, de, entity_category=DIAGNOSTIC))

    # F16 zone and house totals
    for scope, label_en, label_de in (
        ("zone", "Zone", "Zone"),
        ("house", "House", "Haus"),
    ):
        sc: Scope = scope  # type: ignore[assignment]
        d.append(
            DerivedDescription(
                f"{scope}_airflow",
                f"{label_en} airflow",
                f"{label_de} Luftmenge",
                scope=sc,
                unit="m³/h",
                device_class="volume_flow_rate",
                state_class=MEASUREMENT,
                precision=0,
                min_change=1.0,
                min_interval_s=60.0,
                estimate=True,
            )
        )
        d.append(
            DerivedDescription(
                f"{scope}_power",
                f"{label_en} estimated power",
                f"{label_de} geschätzte Leistung",
                scope=sc,
                unit="W",
                device_class="power",
                state_class=MEASUREMENT,
                precision=1,
                min_change=0.1,
                min_interval_s=60.0,
                estimate=True,
            )
        )
        d.append(
            DerivedDescription(
                f"{scope}_energy",
                f"{label_en} estimated energy",
                f"{label_de} geschätzte Energie",
                scope=sc,
                unit="kWh",
                device_class="energy",
                state_class=TOTAL_INCREASING,
                precision=3,
                min_change=0.001,
                min_interval_s=60.0,
                estimate=True,
            )
        )
        d.append(
            DerivedDescription(
                f"{scope}_air_volume",
                f"{label_en} air volume moved",
                f"{label_de} bewegtes Luftvolumen",
                scope=sc,
                unit="m³",
                device_class="volume",
                state_class=TOTAL_INCREASING,
                precision=1,
                min_change=0.1,
                min_interval_s=60.0,
                estimate=True,
            )
        )
        d.append(
            DerivedDescription(
                f"{scope}_running_units",
                f"{label_en} running units",
                f"{label_de} laufende Geräte",
                scope=sc,
                state_class=MEASUREMENT,
                precision=0,
                min_change=1.0,
            )
        )
        d.append(
            DerivedDescription(
                f"{scope}_units",
                f"{label_en} units",
                f"{label_de} Geräte",
                scope=sc,
                enabled_default=False,
                recorded=False,
                precision=0,
                min_change=1.0,
                entity_category=DIAGNOSTIC,
            )
        )
        d.append(
            DerivedDescription(
                f"{scope}_heat_recovered_power",
                f"{label_en} recovered heat power",
                f"{label_de} zurückgewonnene Wärme Leistung",
                scope=sc,
                unit="W",
                device_class="power",
                state_class=MEASUREMENT,
                enabled_default=False,
                precision=0,
                min_change=1.0,
                min_interval_s=60.0,
                estimate=True,
                requires=(REQ_OUTDOOR,),
            )
        )
        d.append(
            DerivedDescription(
                f"{scope}_heat_recovered_energy",
                f"{label_en} recovered heat energy",
                f"{label_de} zurückgewonnene Wärme Energie",
                scope=sc,
                unit="kWh",
                device_class="energy",
                state_class=TOTAL_INCREASING,
                enabled_default=False,
                precision=3,
                min_change=0.001,
                min_interval_s=60.0,
                estimate=True,
                requires=(REQ_OUTDOOR,),
            )
        )
    for key, en, de, unit, dev_class in (
        (
            "outdoor_dew_point",
            "Outdoor dew point",
            "Außentaupunkt",
            "°C",
            "temperature",
        ),
        (
            "outdoor_absolute_humidity",
            "Outdoor absolute humidity",
            "Absolute Außenfeuchte",
            "g/m³",
            "absolute_humidity",
        ),
    ):
        d.append(
            DerivedDescription(
                key,
                en,
                de,
                scope="house",
                unit=unit,
                device_class=dev_class,
                state_class=MEASUREMENT,
                precision=1,
                min_change=0.1,
                min_interval_s=60.0,
                requires=(REQ_OUTDOOR,),
            )
        )
    return tuple(d)


DESCRIPTIONS: Final = _build()
"""All derived entities in a stable order."""

BY_KEY: Final = MappingProxyType({desc.key: desc for desc in DESCRIPTIONS})
SCOPE_KEYS: Final = MappingProxyType(
    {
        scope: tuple(desc.key for desc in DESCRIPTIONS if desc.scope == scope)
        for scope in ("device", "zone", "house")
    }
)


def descriptions_for(scope: Scope) -> tuple[DerivedDescription, ...]:
    """Return the descriptions of one scope."""
    return tuple(desc for desc in DESCRIPTIONS if desc.scope == scope)


@dataclass(frozen=True, slots=True)
class RawDescription:
    """A raw/research datapoint that is deliberately not an entity (F21)."""

    key: str
    name_en: str
    name_de: str
    source: str
    description: str
    entity: bool = False
    recorded: bool = False


RAW_DESCRIPTIONS: Final = (
    RawDescription(
        "udp_state_histogram",
        "UDP state byte histogram",
        "UDP-Zustandsbyte-Histogramm",
        "diagnostics: local tracker, per zone",
        "Count of every distinct value of the UDP state byte (coupling nibble M "
        "and stage/direction nibble S) seen in master broadcasts, keyed by "
        "two-digit hex. Shows unknown coupling or direction values for protocol "
        "research. Exists in the tracker diagnostics already; exposed only in "
        "the diagnostics download, never as an entity, so it adds no recorder "
        "load.",
    ),
)
