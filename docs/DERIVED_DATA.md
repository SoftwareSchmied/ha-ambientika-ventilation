# Derived data

The integration derives additional values from what the cloud and the local UDP
broadcast deliver. All derivations live in Home Assistant independent modules:

| Module | Purpose |
|---|---|
| `analytics.py` | The engine: counters, estimates, validity, filter, automatics, commands |
| `analytics_descriptions.py` | One description per derived value (unit, classes, recorder budget, EN/DE names) |
| `psychrometrics.py` | Dew point, absolute humidity, sensible heat of an air flow |
| `weather_input.py` | Validated outdoor temperature/humidity and the values derived from them |

**Every value marked "estimate" is a model result, not a measurement.** The
units have no power meter, no flow sensor and no heat meter. The figures below
come from the manufacturer manual (power, airflow, filter interval) and the
TUeV SUeD/DIBt test report WRG756-REV.1 (heat and moisture recovery). Accuracy
of about +-5 % in airflow can be expected for the data sheet values, power is
the manufacturer figure per unit (the DIBt values are per pair of units).

## Inputs and time model

The engine receives one `Snapshot` per unit (`power`, `running`,
`physical_stage`, `direction`, `coupling`, `operating_mode`, `fan_speed`,
`humidity_level`, `temperature`, `humidity`, `air_quality`, `filter_status`,
`night_alarm`, `humidity_alarm`, optional tracker `reversals`, zone key) from
the resolved datapoints, a monotonic time and a timezone-aware local wall time.

Counters integrate **live only**. The interval between two updates is credited
to the *previous* state (zero-order hold). An interval longer than `max_gap_s`
(300 s) is credited only up to that limit, so a Home Assistant outage never
creates phantom runtime. Counters survive restarts through `export_state()` /
`import_state()`.

Ventilation phase of a unit (`ventilation_phase`):

| Phase | Source | Meaning |
|---|---|---|
| `exhaust`, `intake` | UDP direction | Fan moves air |
| `reversing` | UDP direction `stopped` while power is on and the zone is coupled | The ~10 s stop between two half cycles |
| `stopped` | UDP idle coupling, or power off | No ventilation (standby) |
| `running` | cloud only (`running` or `power`) | Air moves, direction unknown |
| unknown | nothing known | No derivation |

"Ventilating" is `exhaust`, `intake`, `reversing` or `running`. In cloud-only
operation of a heat recovery mode the moving share is taken as 60/70 (cycle of
70 s per direction with about 10 s standstill); in UDP operation the real
standstill is measured.

## Formulas

### Runtimes (F4)

`operating_hours` = time ventilating. `on_hours` = time powered on (includes
idle). Runtime per mode, per stage (ventilating only) and per direction
(UDP only; `stopped` includes the reversal pause). `reversals` counts exhaust
to intake changes; the tracker counter (`LocalZoneState.reversals`) is used when
supplied (its restart-reset is handled), otherwise the engine counts observed
changes. `heat_recovery_cycles = reversals // 2`. `half_cycle_duration` is the
mean of the last 20 intervals between reversals (expected: 70 s), which needs an
`update` call on every local direction change.

### Air flow and volume (F5)

* Stage flow per unit: night 10, low 20, medium 40, high 60 m3/h (manual p. 30;
  free-blowing DIBt 20.7/40.8/63.1). Turbo is unpublished and treated as high
  (configurable).
* `airflow` = stage flow while ventilating, 0 otherwise.
* `air_volume` (m3) = integral of `airflow * moving share` (the volume the fan
  moved; it is **not** the fresh air volume of the room).
* `fresh_air_flow` = `airflow * 0.5` in heat recovery (a unit supplies fresh air
  half of the time; a master/slave pair together supplies the full stage flow),
  `airflow` in one-way modes.
* `air_change_rate` (1/h) = `fresh_air_flow / room volume`, only when a room
  volume is configured for the unit. For a room with two units the room value is
  the sum of both units.

### Power and energy (F6, estimate)

Per unit and stage: night 3.9, low 4.2, medium 5.5, high 6.7 W while
ventilating (including the reversal pause), standby 0.5 W otherwise (an
assumption, nothing is published). A calibration factor `power_scale` scales the
stage values. `energy_estimate` (kWh, `total_increasing`) integrates the power,
usable in the energy dashboard.

### Recovered heat and ventilation loss (F7, estimate)

Needs an outdoor temperature (`weather_input`) and a valid room temperature.

```
dT      = T_room(last valid) - T_outdoor            (only dT > 0, else 0)
total   = 0.335 Wh/(m3 K) * fresh_air_flow * dT      [W]
recovered = eta(stage) * total      (only in heat recovery operation)
loss      = (1 - eta(stage)) * total
```

`0.335 = 1.2 kg/m3 * 1005 J/(kg K) / 3600`. eta (supply temperature ratio, DIBt
at 7 C outdoor): low 0.918, medium 0.808, high 0.729 (turbo as high); night 0.92
is extrapolated. In one-way modes (extraction, intake, flow modes, Smart free
cooling) there is no recovery: `loss = total`. The marketing value of 93 % is not
used. Energy counters integrate the power with the moving share. Cooling
(outdoor warmer than indoor) is not evaluated, both values are 0.

`moisture_recovered` (g/h) = `eps(stage) * fresh_air_flow * max(0, AH_room -
AH_outdoor)` with eps = 0.58/0.475/0.383 (DIBt App. G2, 2 C outdoor), not
published for night.

### Humidity and room climate (F8)

Magnus formulas (Sonntag 1990), with `es = 6.112 hPa * exp(17.62 T / (243.12 + T))`:

* dew point `Td = 243.12 g / (17.62 - g)`, `g = ln(RH/100) + 17.62 T / (243.12 + T)`
* absolute humidity `AH = 216.7 * (RH/100 * es) / (273.15 + T)` in g/m3
* `humidity_setpoint`: Dry/Normal/Moist = 40/60/75 % (app manual 2023; the
  mapping is inferred); mode Away uses a fixed 60 %.
* `humidity_setpoint_distance` = RH - setpoint (positive: above setpoint).
* `comfort`: RH < 30 too_dry, < 40 dry, 40 to 60 comfortable, up to 70 humid,
  above too_humid. Only when the measurement is currently valid.

The temperature has a resolution of 1 C, so AH is uncertain by about +-0.6 g/m3.
Raw values are never smoothed.

### Measurement validity (F12)

While the unit is off or idle it measures the air in the wall duct (measured:
about -6 K and +31..36 % RH against a room sensor, no correlation). The
sensor values count as **room values** only after the unit has ventilated
continuously for `settle_s` = 300 s (5 minutes). The engine publishes
`measurement_valid`, `measurement_context` (stopped, settling, exhaust, intake,
reversing, running, unknown) and the held values `room_temperature` /
`room_humidity` that only change on valid readings. All derivations (dew point,
AH, distance, heat, drying potential) use the held values; they expire after
12 h. The raw sensors stay unchanged and untouched.

The reversal pause also counts as ventilating, so a valid measurement is not
interrupted every 70 s. Without UDP an idle unit that the cloud reports as
powered on (for example Surveillance without alarm) cannot be told from a
running one.

### Air quality (F9)

`air_quality_index`: VeryGood 1, Good 2, Medium 3, Poor 4, Bad 5 (1 = best). The
raw enum is kept as attribute `air_quality_raw`; an unknown value gives no
number.

### Filter (F10)

* The device raises the filter alarm after 3000 operating hours (manual p. 39).
  The engine counts its own `filter_hours` since the last reset on the basis
  `operating` (fan moving air, default) or `on_time` (powered on). Which basis the
  device counts is not documented; `filter_alarm_hours` shows the hours at which
  the device actually switched to Bad so the user can verify.
* Reset: detected on the transition Medium/Bad to Good, or by `reset_filter()`.
* `filter_remaining_hours = max(0, 3000 - filter_hours)`, 0 when Bad.
* `filter_alarm_predicted = now + remaining / usage rate`. The usage rate is the
  operating share of the last 7 days (daily buckets, up to 14 days kept) over
  the tracked time; needs at least 1 h of tracking and some usage.
* `filter_clean_due` = reset + 91 days, `filter_replace_due` = reset + 730 days
  (manual: clean every 3 months, replace after at most 2 years). Before a reset
  was seen, the start of tracking is used.

### Hidden automatics (F11, F19)

* `humidity_exhaust_active`: humidity alarm in Auto/Surveillance, unit on, and with
  UDP coupling `same` and direction `exhaust`.
* `night_reduction_active`: mode Smart/Auto ventilating at the night stage (UDP
  stage).
* `timed_exhaust_active`: mode TimedExpulsion and on. `timed_exhaust_ends` = first
  observation of the mode + 20 min (estimate; later than reality if the mode was
  already active when the integration started).
* `smart_behaviour` (mode Smart only): `idle`, `heat_recovery`,
  `night_heat_recovery`, `free_cooling`. Free cooling is detected by coupling `same`
  or by a constant direction for more than 150 s (two half cycles) in opposite
  coupling, which alternation would not allow. `free_cooling_active` follows.
* `damper_state` (estimate): opening for 40 s after start, then open; closing
  for 5 minutes after the stop (manual: "several minutes"), then closed.

### Alarm durations (F17)

`*_alarm_duration` (h, total), `*_alarm_today` (min, resets at local midnight),
`*_alarm_current` (min of the running episode) for the humidity and the night
alarm.

### Commands (F18)

The lead feeds `command_sent`, `command_confirmed(source="cloud"|"local")` and
`command_rejected`. A command not confirmed within 60 s counts as unconfirmed.
Outputs: last latency, latency per source, mean of the last 20, and counters sent,
confirmed, unconfirmed, rejected.

### Zone and house totals (F16)

`zone_*` and `house_*`: airflow, estimated power, estimated energy, air volume,
running units, unit count (and recovered heat with an outdoor source), summed over
the latest values of the units. Call `update` for all units of a zone in the same
cycle. `outdoor_dew_point` and `outdoor_absolute_humidity` are house values.

### Outdoor weather (F13)

`weather_input` takes numbers from a user-selected HA weather or sensor entity
(units are converted; stale readings older than 3 h are ignored). It derives the
outdoor dew point and absolute humidity, the **drying potential** (indoor minus
outdoor AH, positive = outdoor air is drier) and the warning
`outdoor_humidifies` (potential below -0.5 g/m3, a margin for the sensor
resolution). The vendor's embedded weather key is never used.

### Raw research data (F21)

The histogram of the UDP state byte per zone already exists in the tracker
diagnostics. It stays diagnostics-only (no entity, no recorder load); see
`RAW_DESCRIPTIONS`.

## Recorder budget (C6)

Every description carries `precision` (decimals), `min_change` and
`min_interval_s`. A value is reported only when it differs from the last reported
value by at least `min_change` and at least `min_interval_s` have passed
(power and energy: 60 s; counters of hours: 5 to 10 min; filter dates: 1 h and 6 h).
A change between available and unavailable is reported immediately. Values with
`recorded = no` carry no state class (no long-term statistics) and are disabled
by default. Heartbeats never create a state write: the engine only reports
meaningful changes.

## Persistence format

`export_state()` returns `{"version": 1, "devices": {serial: {...}}}` with plain
JSON types. Per unit: `operating_s`, `on_s`, `mode_s`/`stage_s`/`direction_s`
(seconds by key), `reversals`, `volume_m3`, `energy_kwh`, `recovered_kwh`,
`loss_kwh`, alarm seconds (total and today), `today` (date), `filter_s`,
`filter_status`, `filter_reset`, `tracking_since`, `filter_medium_s`,
`filter_bad_s`, `usage` (date to seconds), `held` (temperature, humidity, `at`),
`timed_started` and `commands` counters. Timestamps are ISO 8601 strings. Unknown
versions and malformed data are ignored.

## All derived values

Scope `device` is per unit, `zone` per zone, `house` per installation. "Needs"
lists the precondition for creating the entity: `outdoor` (an outdoor source is
configured), `room_volume`, `udp` (meaningful only with the local source).

| Key | Scope | Unit | Device class | State class | Enabled | Recorded | Prec. | Min change | Min interval | EN name | DE name | Needs |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `operating_hours` | device | h | duration | total_increasing | yes | yes | 2 | 0.01 | 300 s | Operating hours | Betriebsstunden |  |
| `on_hours` | device | h | duration | total_increasing | no | yes | 2 | 0.01 | 300 s | Powered-on hours | Einschaltdauer |  |
| `operating_hours_today` | device | h | duration |  | no | no | 2 | 0.05 | 600 s | Operating hours today | Betriebsstunden heute |  |
| `runtime_mode_smart` | device | h | duration | total_increasing | no | yes | 2 | 0.01 | 600 s | Runtime Smart | Laufzeit Smart |  |
| `runtime_mode_auto` | device | h | duration | total_increasing | no | yes | 2 | 0.01 | 600 s | Runtime Auto | Laufzeit Auto |  |
| `runtime_mode_heat_recovery` | device | h | duration | total_increasing | no | yes | 2 | 0.01 | 600 s | Runtime Heat recovery | Laufzeit Wärmerückgewinnung |  |
| `runtime_mode_night` | device | h | duration | total_increasing | no | yes | 2 | 0.01 | 600 s | Runtime Night | Laufzeit Nacht |  |
| `runtime_mode_away` | device | h | duration | total_increasing | no | yes | 2 | 0.01 | 600 s | Runtime Away | Laufzeit Abwesend |  |
| `runtime_mode_surveillance` | device | h | duration | total_increasing | no | yes | 2 | 0.01 | 600 s | Runtime Surveillance | Laufzeit Überwachung |  |
| `runtime_mode_timed_extraction` | device | h | duration | total_increasing | no | yes | 2 | 0.01 | 600 s | Runtime Timed extraction | Laufzeit Zeitgesteuerte Abluft |  |
| `runtime_mode_extraction` | device | h | duration | total_increasing | no | yes | 2 | 0.01 | 600 s | Runtime Extraction | Laufzeit Abluft |  |
| `runtime_mode_intake` | device | h | duration | total_increasing | no | yes | 2 | 0.01 | 600 s | Runtime Intake | Laufzeit Zuluft |  |
| `runtime_mode_master_slave_flow` | device | h | duration | total_increasing | no | yes | 2 | 0.01 | 600 s | Runtime Master to slave flow | Laufzeit Fluss Master zu Slave |  |
| `runtime_mode_slave_master_flow` | device | h | duration | total_increasing | no | yes | 2 | 0.01 | 600 s | Runtime Slave to master flow | Laufzeit Fluss Slave zu Master |  |
| `runtime_mode_off` | device | h | duration | total_increasing | no | yes | 2 | 0.01 | 600 s | Off time | Ausgeschaltet-Dauer |  |
| `runtime_mode_turbo` | device | h | duration | total_increasing | no | yes | 2 | 0.01 | 600 s | Runtime Turbo | Laufzeit Turbo |  |
| `runtime_stage_night` | device | h | duration | total_increasing | no | yes | 2 | 0.01 | 600 s | Runtime stage Night | Laufzeit Stufe Nacht |  |
| `runtime_stage_low` | device | h | duration | total_increasing | no | yes | 2 | 0.01 | 600 s | Runtime stage Low | Laufzeit Stufe Niedrig |  |
| `runtime_stage_medium` | device | h | duration | total_increasing | no | yes | 2 | 0.01 | 600 s | Runtime stage Medium | Laufzeit Stufe Mittel |  |
| `runtime_stage_high` | device | h | duration | total_increasing | no | yes | 2 | 0.01 | 600 s | Runtime stage High | Laufzeit Stufe Hoch |  |
| `runtime_stage_turbo` | device | h | duration | total_increasing | no | yes | 2 | 0.01 | 600 s | Runtime stage Turbo | Laufzeit Stufe Turbo |  |
| `runtime_direction_exhaust` | device | h | duration | total_increasing | no | yes | 2 | 0.01 | 600 s | Runtime exhaust | Laufzeit Abluft | udp |
| `runtime_direction_intake` | device | h | duration | total_increasing | no | yes | 2 | 0.01 | 600 s | Runtime intake | Laufzeit Zuluft | udp |
| `runtime_direction_stopped` | device | h | duration | total_increasing | no | yes | 2 | 0.01 | 600 s | Runtime stopped or reversing | Laufzeit Stillstand oder Umkehr | udp |
| `reversals` | device |  |  | total_increasing | no | yes | 0 | 1 | 300 s | Direction reversals | Richtungswechsel | udp |
| `heat_recovery_cycles` | device |  |  | total_increasing | no | yes | 0 | 1 | 300 s | Heat recovery cycles | Wärmerückgewinnungs-Zyklen | udp |
| `half_cycle_duration` | device | s | duration |  | no | no | 0 | 2.0 | 300 s | Half cycle duration | Halbzyklus-Dauer | udp |
| `airflow` | device | m³/h | volume_flow_rate | measurement | yes | yes | 0 | 1.0 | 60 s | Airflow (estimate) | Luftmenge |  |
| `fresh_air_flow` | device | m³/h | volume_flow_rate | measurement | no | yes | 0 | 1.0 | 60 s | Fresh air flow (estimate) | Frischluftmenge |  |
| `air_volume` | device | m³ | volume | total_increasing | yes | yes | 1 | 0.1 | 60 s | Air volume moved (estimate) | Bewegtes Luftvolumen |  |
| `air_change_rate` | device | 1/h |  | measurement | yes | yes | 2 | 0.05 | 60 s | Air change rate (estimate) | Luftwechselrate | room_volume |
| `power_estimate` | device | W | power | measurement | yes | yes | 1 | 0.1 | 60 s | Estimated power (estimate) | Geschätzte Leistung |  |
| `energy_estimate` | device | kWh | energy | total_increasing | yes | yes | 3 | 0.001 | 60 s | Estimated energy (estimate) | Geschätzte Energie |  |
| `heat_recovered_power` | device | W | power | measurement | yes | yes | 0 | 1.0 | 60 s | Recovered heat power (estimate) | Zurückgewonnene Wärme Leistung | outdoor |
| `heat_recovered_energy` | device | kWh | energy | total_increasing | yes | yes | 3 | 0.001 | 60 s | Recovered heat energy (estimate) | Zurückgewonnene Wärme Energie | outdoor |
| `ventilation_loss_power` | device | W | power | measurement | yes | yes | 0 | 1.0 | 60 s | Ventilation heat loss power (estimate) | Lüftungswärmeverlust Leistung | outdoor |
| `ventilation_loss_energy` | device | kWh | energy | total_increasing | yes | yes | 3 | 0.001 | 60 s | Ventilation heat loss energy (estimate) | Lüftungswärmeverlust Energie | outdoor |
| `moisture_recovered` | device | g/h |  |  | no | no | 0 | 5.0 | 300 s | Recovered moisture (estimate) | Zurückgewonnene Feuchte | outdoor |
| `dew_point` | device | °C | temperature | measurement | yes | yes | 1 | 0.1 | 60 s | Dew point | Taupunkt |  |
| `absolute_humidity` | device | g/m³ | absolute_humidity | measurement | yes | yes | 1 | 0.1 | 60 s | Absolute humidity | Absolute Feuchte |  |
| `humidity_setpoint` | device | % | humidity |  | yes | no | 0 | 1.0 | 0 s | Humidity setpoint | Feuchte-Sollwert |  |
| `humidity_setpoint_distance` | device | % |  | measurement | yes | yes | 0 | 1.0 | 60 s | Humidity distance to setpoint | Feuchte-Abstand zum Sollwert |  |
| `comfort` | device |  | enum |  | yes | yes |  |  | 60 s | Humidity comfort | Feuchtekomfort |  |
| `room_temperature` | device | °C | temperature | measurement | yes | yes | 0 | 1.0 | 60 s | Room temperature (last valid) | Raumtemperatur (zuletzt gültig) |  |
| `room_humidity` | device | % | humidity | measurement | yes | yes | 0 | 1.0 | 60 s | Room humidity (last valid) | Raumfeuchte (zuletzt gültig) |  |
| `measurement_valid` | device |  |  |  | yes | yes |  |  | 0 s | Measurement representative | Messung repräsentativ |  |
| `measurement_context` | device |  | enum |  | no | no |  |  | 0 s | Measurement context | Messkontext |  |
| `measurement_last_valid` | device |  | timestamp |  | no | no |  | 60.0 | 60 s | Last valid measurement | Letzte gültige Messung |  |
| `drying_potential` | device | g/m³ |  | measurement | yes | yes | 1 | 0.1 | 60 s | Drying potential | Trocknungspotenzial | outdoor |
| `outdoor_humidifies` | device |  | problem |  | yes | yes |  |  | 60 s | Outdoor air would humidify | Außenluft würde befeuchten | outdoor |
| `air_quality_index` | device |  |  | measurement | yes | yes | 0 | 1.0 | 60 s | Air quality index | Luftqualitätsindex |  |
| `filter_hours` | device | h | duration | total_increasing | yes | yes | 1 | 0.1 | 600 s | Filter operating hours | Filter-Betriebsstunden |  |
| `filter_remaining_hours` | device | h | duration | measurement | yes | yes | 0 | 1.0 | 600 s | Filter remaining hours (estimate) | Filter-Restlaufzeit |  |
| `filter_alarm_predicted` | device |  | timestamp |  | yes | no |  | 21600.0 | 3600 s | Filter alarm expected on (estimate) | Filteralarm voraussichtlich am |  |
| `filter_clean_due` | device |  | timestamp |  | no | no |  | 60.0 | 60 s | Filter cleaning due (estimate) | Filterreinigung fällig |  |
| `filter_replace_due` | device |  | timestamp |  | no | no |  | 60.0 | 60 s | Filter replacement due (estimate) | Filterwechsel fällig |  |
| `filter_last_reset` | device |  | timestamp |  | no | no |  | 60.0 | 60 s | Filter last reset | Filter zuletzt zurückgesetzt |  |
| `filter_medium_hours` | device | h | duration |  | no | no | 1 | 0.1 | 0 s | Filter hours at Medium | Filterstunden bei Mittel |  |
| `filter_alarm_hours` | device | h | duration |  | no | no | 1 | 0.1 | 0 s | Filter hours at alarm | Filterstunden bei Alarm |  |
| `humidity_exhaust_active` | device |  |  |  | yes | yes |  |  | 0 s | Humidity exhaust active | Feuchte-Abluft aktiv |  |
| `night_reduction_active` | device |  |  |  | yes | yes |  |  | 0 s | Night reduction active | Nachtabsenkung aktiv | udp |
| `timed_exhaust_active` | device |  |  |  | yes | yes |  |  | 0 s | Timed extraction active | Zeitabluft aktiv |  |
| `free_cooling_active` | device |  |  |  | yes | yes |  |  | 0 s | Free cooling active | Freie Kühlung aktiv | udp |
| `smart_behaviour` | device |  | enum |  | yes | yes |  |  | 0 s | Smart behaviour | Smart-Verhalten | udp |
| `timed_exhaust_ends` | device |  | timestamp |  | no | no |  | 60.0 | 0 s | Timed extraction ends (estimate) | Zeitabluft endet |  |
| `damper_state` | device |  | enum |  | no | no |  |  | 0 s | Damper state (estimate) | Klappenzustand |  |
| `humidity_alarm_duration` | device | h | duration | total_increasing | no | yes | 2 | 0.01 | 300 s | Humidity alarm duration | Feuchtealarm Dauer |  |
| `humidity_alarm_today` | device | min | duration |  | no | no | 0 | 1.0 | 60 s | Humidity alarm today | Feuchtealarm heute |  |
| `humidity_alarm_current` | device | min | duration |  | no | no | 0 | 1.0 | 60 s | Humidity alarm current | Feuchtealarm aktuell |  |
| `night_alarm_duration` | device | h | duration | total_increasing | no | yes | 2 | 0.01 | 300 s | Night alarm duration | Nachtalarm Dauer |  |
| `night_alarm_today` | device | min | duration |  | no | no | 0 | 1.0 | 60 s | Night alarm today | Nachtalarm heute |  |
| `night_alarm_current` | device | min | duration |  | no | no | 0 | 1.0 | 60 s | Night alarm current | Nachtalarm aktuell |  |
| `command_latency` | device | s | duration |  | no | no | 1 | 0.1 | 0 s | Last command latency | Letzte Befehlslatenz |  |
| `command_latency_cloud` | device | s | duration |  | no | no | 1 | 0.1 | 0 s | Command latency (cloud) | Befehlslatenz (Cloud) |  |
| `command_latency_local` | device | s | duration |  | no | no | 1 | 0.1 | 0 s | Command latency (local) | Befehlslatenz (lokal) |  |
| `command_latency_average` | device | s | duration |  | no | no | 1 | 0.1 | 0 s | Average command latency | Mittlere Befehlslatenz |  |
| `commands_sent` | device |  |  | total_increasing | no | yes | 0 | 1 | 60 s | Commands sent | Gesendete Befehle |  |
| `commands_confirmed` | device |  |  | total_increasing | no | yes | 0 | 1 | 60 s | Commands confirmed | Bestätigte Befehle |  |
| `commands_unconfirmed` | device |  |  | total_increasing | no | yes | 0 | 1 | 60 s | Commands unconfirmed | Unbestätigte Befehle |  |
| `commands_rejected` | device |  |  | total_increasing | no | yes | 0 | 1 | 60 s | Commands rejected | Abgelehnte Befehle |  |
| `zone_airflow` | zone | m³/h | volume_flow_rate | measurement | yes | yes | 0 | 1.0 | 60 s | Zone airflow (estimate) | Zone Luftmenge |  |
| `zone_power` | zone | W | power | measurement | yes | yes | 1 | 0.1 | 60 s | Zone estimated power (estimate) | Zone geschätzte Leistung |  |
| `zone_energy` | zone | kWh | energy | total_increasing | yes | yes | 3 | 0.001 | 60 s | Zone estimated energy (estimate) | Zone geschätzte Energie |  |
| `zone_air_volume` | zone | m³ | volume | total_increasing | yes | yes | 1 | 0.1 | 60 s | Zone air volume moved (estimate) | Zone bewegtes Luftvolumen |  |
| `zone_running_units` | zone |  |  | measurement | yes | yes | 0 | 1.0 | 0 s | Zone running units | Zone laufende Geräte |  |
| `zone_units` | zone |  |  |  | no | no | 0 | 1.0 | 0 s | Zone units | Zone Geräte |  |
| `zone_heat_recovered_power` | zone | W | power | measurement | no | yes | 0 | 1.0 | 60 s | Zone recovered heat power (estimate) | Zone zurückgewonnene Wärme Leistung | outdoor |
| `zone_heat_recovered_energy` | zone | kWh | energy | total_increasing | no | yes | 3 | 0.001 | 60 s | Zone recovered heat energy (estimate) | Zone zurückgewonnene Wärme Energie | outdoor |
| `house_airflow` | house | m³/h | volume_flow_rate | measurement | yes | yes | 0 | 1.0 | 60 s | House airflow (estimate) | Haus Luftmenge |  |
| `house_power` | house | W | power | measurement | yes | yes | 1 | 0.1 | 60 s | House estimated power (estimate) | Haus geschätzte Leistung |  |
| `house_energy` | house | kWh | energy | total_increasing | yes | yes | 3 | 0.001 | 60 s | House estimated energy (estimate) | Haus geschätzte Energie |  |
| `house_air_volume` | house | m³ | volume | total_increasing | yes | yes | 1 | 0.1 | 60 s | House air volume moved (estimate) | Haus bewegtes Luftvolumen |  |
| `house_running_units` | house |  |  | measurement | yes | yes | 0 | 1.0 | 0 s | House running units | Haus laufende Geräte |  |
| `house_units` | house |  |  |  | no | no | 0 | 1.0 | 0 s | House units | Haus Geräte |  |
| `house_heat_recovered_power` | house | W | power | measurement | no | yes | 0 | 1.0 | 60 s | House recovered heat power (estimate) | Haus zurückgewonnene Wärme Leistung | outdoor |
| `house_heat_recovered_energy` | house | kWh | energy | total_increasing | no | yes | 3 | 0.001 | 60 s | House recovered heat energy (estimate) | Haus zurückgewonnene Wärme Energie | outdoor |
| `outdoor_dew_point` | house | °C | temperature | measurement | yes | yes | 1 | 0.1 | 60 s | Outdoor dew point | Außentaupunkt | outdoor |
| `outdoor_absolute_humidity` | house | g/m³ | absolute_humidity | measurement | yes | yes | 1 | 0.1 | 60 s | Outdoor absolute humidity | Absolute Außenfeuchte | outdoor |
