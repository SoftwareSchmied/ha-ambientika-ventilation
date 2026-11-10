# Entity reference

Entity unique IDs use the device serial number and an invariant entity key.
Renaming a room or device therefore does not create duplicate entities.

## Controls

| Platform | Key | Purpose | Availability |
| --- | --- | --- | --- |
| Fan | `ventilation` | Power, Low/Medium/High/Turbo speed, Night preset | Zone masters and Gemini devices |
| Select | `operating_mode` | Supported operating modes for the device family | Zone masters and Gemini devices |
| Select | `humidity_level` | Dry/Normal/Moist humidity target | When the active mode permits it |
| Select | `light_sensor_level` | Off/Low/Medium light sensitivity | When reported and permitted |
| Switch | `schedule_control` | Enable or disable an existing weekly schedule | Devices with schedule support |
| Button | `reset_filter` | Reset filter status after replacement | Only while filter status is Bad |
| Calendar | `schedule_calendar` | Weekly schedule as recurring events; editable when the account and unit allow it | Units with a loaded schedule |

Manual controls are intentionally rejected while a schedule is active or the
filter state is `Bad`. The schedule can still be disabled and the filter can
still be reset.

## Schedule calendar

One calendar per controllable unit that has a weekly schedule (the unit reports
a schedule state other than `NotAvailable` and the account has the
`weeklyScheduler` feature). Each time slot is a weekly recurring event in the
house time zone (`ianaTimezone`, else the Home Assistant time zone). The event
title shows mode, fan speed and humidity target; the description lists every
field as `field: option` lines. The calendar is read-only when the account
lacks the feature or the unit reports `NotAvailable`.

Actions on the calendar: `set_time_slot`, `delete_time_slot`, `clear_schedule`
(needs `confirm: true`) and `get_schedule` (returns the slots). See the README
section "Schedules".

## Primary sensors

| Platform | Key | Value |
| --- | --- | --- |
| Sensor | `temperature` | Temperature in °C |
| Sensor | `humidity` | Relative humidity in % |
| Sensor | `air_quality` | Vendor air-quality classification |
| Sensor | `filter_status` | Good, replace soon, or replace |
| Binary sensor | `humidity_alarm` | High-humidity condition |
| Binary sensor | `filter_problem` | Filter needs attention |
| Binary sensor | `night` | Night detected by the unit |
| Binary sensor | `schedule` | Weekly schedule is active |
| Binary sensor | `cloud_connection` | The cloud has current status data for the unit (diagnostic; available while the unit is offline) |

## Local data and data sources

Every value that both the cloud and the local UDP broadcast can provide has an
effective entity (the existing unique ID `<serial>_<key>`), which prefers the
local value while it is fresh, plus a cloud and a local variant. See
[Local data](../README.md#local-data) for the setup and the options.

| Platform | Unique ID | Meaning | Default | Recorded |
| --- | --- | --- | --- | --- |
| Binary sensor | `<serial>_power` | Unit is on (local: moving air or non-idle coupling; otherwise pending command or cloud mode) | Enabled | Yes, attribute `source` |
| Binary sensor | `<serial>_running` | Air is physically moving (local direction, cloud mode as fallback) | Enabled | Yes, attribute `source` |
| Binary sensor | `<serial>_power_cloud`, `_power_local`, `_running_cloud`, `_running_local` | Raw value of one source | Disabled | Yes |
| Sensor | `<serial>_direction` | Physical direction: stopped, exhaust, intake | Enabled | Yes |
| Sensor | `<serial>_physical_stage` | Physical fan stage: night, low, medium, high; unknown while idle | Enabled | Yes |
| Sensor | `<serial>_coupling` | Zone coupling: idle, opposite, same | Enabled | Yes |
| Sensor | `<serial>_data_source` | `local_udp`, `cloud`, `degraded` or `offline` (diagnostic) | Enabled | Yes |
| Sensor | `<serial>_last_seen` | Latest sign of life from cloud or local (diagnostic, written at most once per minute) | Enabled | Yes |
| Sensor | `<serial>_last_seen_local` | Latest local frame (diagnostic, written at most once per minute); replaces the former `last_local_frame` entity | Enabled with local reception | Yes |
| Sensor | `<serial>_last_seen_cloud` | Latest cloud poll of the unit | Disabled | Yes |
| Sensor | `<serial>_heartbeat_age` | Seconds since the last local frame (diagnostic, written at most once per minute) | Disabled | Snapshot every minute, no statistics |
| Binary sensor | `<entry id>_cloud_reachable` | Cloud delivered data recently and without repeated failures (account device) | Enabled | Yes |
| Sensor | `<entry id>_cloud_consecutive_failures` | Failed polls in a row (account device) | Enabled | Yes |
| Sensor | `<entry id>_cloud_success_rate`, `_cloud_last_latency`, `_cloud_last_success`, `_cloud_last_failure_kind` | Request health (account device) | Disabled | Yes |

While the local reception is **Off**, the local-only entities (direction,
physical stage, coupling, heartbeat age) and all `_local` variants are not
created; the udp-dependent derived values below are skipped as well.

The effective entities carry the attribute `source` (`local_udp`, `expected`,
`cloud`, `cloud_heuristic`), which has few distinct values and is recorded,
and `age_s`, which changes constantly and is excluded from the recorder.
The raw variants are created disabled; the option "Create raw source variants"
creates or enables them.

The fan state, presets, percentage and the selects keep following the cloud
setting (and the commands you just sent), never the local broadcast, so a unit
that idles in Away mode is not shown as off.

## Diagnostic entities

The following entities are disabled by default to limit state-history and UI
clutter: packet type, last operating mode, signal strength, schedule entries,
device type/subtype/role, installation time, controller/radio firmware, house,
zone, room, cloud IDs, and zone index. Enable only the diagnostics needed for a
specific dashboard or automation.

Unknown or absent API values are represented as unknown. An entity is marked
unavailable after a failed update for its device and recovers automatically on
the next successful poll.

## House device

Values of the whole installation live on one device per house ("house device",
identifier `house_<house id>`, named after the house, no serial number). It
carries the house totals and the outdoor values (see below) and five
diagnostic sensors that are **disabled by default** because they identify the
installation:

| Platform | Unique ID | Value | Default |
| --- | --- | --- | --- |
| Sensor | `<house>_address` | Address of the house | Disabled |
| Sensor | `<house>_latitude` | Latitude in degrees | Disabled |
| Sensor | `<house>_longitude` | Longitude in degrees | Disabled |
| Sensor | `<house>_time_zone` | IANA time zone, else the UTC offset | Disabled |
| Sensor | `<house>_time` | House time reported at the last discovery | Disabled |

They are created only when the cloud reports the value. The privacy rule is
unchanged: address, coordinates and time are never attributes of any entity and
never part of the diagnostics download.

## Derived data

All entities below come from the derived-data engine (see
[Derived data](DERIVED_DATA.md) for formulas and the recorder budget and the
README section "Derived data" for the options). They exist per scope:

* **Unit** (`<serial>_<key>`): on the unit's own device.
* **Zone** (`<house>_<zone index>_<key>`): totals of the units of one zone, on
  the device of the zone's master (no extra device).
* **House** (`<house>_<key>`): totals of one house and the outdoor values, on
  the house device.

The house id appears in unique IDs only, never in names or attributes. Entities
marked as an estimate carry the attribute `estimate: true`; the helper
attributes `measurement_context` and `air_quality_raw` are not recorded. An
entity writes its state only when the engine reports a meaningful change
(rounding, threshold and minimum interval of the recorder budget), never on a
heartbeat. **Needs** names the precondition for creating the entity:
`outdoor` (an outdoor source is configured), `room_volume` (a volume is set for
the unit) and `udp` (local reception is not Off).

Numbers for an account with one house, two zones and two masters:

| Configuration | Unit entities (enabled / total) | Zone entities | House entities | Total |
| --- | --- | --- | --- | --- |
| Local reception on, no outdoor source | 44 / 144 | 10 / 12 | 5 / 6 | 59 / 162 |
| Local reception on, with outdoor source | 56 / 158 | 10 / 16 | 7 / 10 | 73 / 184 |
| Local reception off, no outdoor source | 38 / 126 | 10 / 12 | 5 / 6 | 53 / 144 |

A room volume adds one enabled air change rate per unit that has one.

| Platform | Unique ID | Name | Unit | Default | Recorded | Needs |
| --- | --- | --- | --- | --- | --- | --- |
| Sensor | `<serial>_operating_hours` | Operating hours | h | Enabled | Yes |  |
| Sensor | `<serial>_on_hours` | Powered-on hours | h | Disabled | Yes |  |
| Sensor | `<serial>_operating_hours_today` | Operating hours today | h | Disabled | No |  |
| Sensor | `<serial>_runtime_mode_smart` | Runtime Smart | h | Disabled | Yes |  |
| Sensor | `<serial>_runtime_mode_auto` | Runtime Auto | h | Disabled | Yes |  |
| Sensor | `<serial>_runtime_mode_heat_recovery` | Runtime Heat recovery | h | Disabled | Yes |  |
| Sensor | `<serial>_runtime_mode_night` | Runtime Night | h | Disabled | Yes |  |
| Sensor | `<serial>_runtime_mode_away` | Runtime Away | h | Disabled | Yes |  |
| Sensor | `<serial>_runtime_mode_surveillance` | Runtime Surveillance | h | Disabled | Yes |  |
| Sensor | `<serial>_runtime_mode_timed_extraction` | Runtime Timed extraction | h | Disabled | Yes |  |
| Sensor | `<serial>_runtime_mode_extraction` | Runtime Extraction | h | Disabled | Yes |  |
| Sensor | `<serial>_runtime_mode_intake` | Runtime Intake | h | Disabled | Yes |  |
| Sensor | `<serial>_runtime_mode_master_slave_flow` | Runtime Master to slave flow | h | Disabled | Yes |  |
| Sensor | `<serial>_runtime_mode_slave_master_flow` | Runtime Slave to master flow | h | Disabled | Yes |  |
| Sensor | `<serial>_runtime_mode_off` | Off time | h | Disabled | Yes |  |
| Sensor | `<serial>_runtime_mode_turbo` | Runtime Turbo | h | Disabled | Yes |  |
| Sensor | `<serial>_runtime_stage_night` | Runtime stage Night | h | Disabled | Yes |  |
| Sensor | `<serial>_runtime_stage_low` | Runtime stage Low | h | Disabled | Yes |  |
| Sensor | `<serial>_runtime_stage_medium` | Runtime stage Medium | h | Disabled | Yes |  |
| Sensor | `<serial>_runtime_stage_high` | Runtime stage High | h | Disabled | Yes |  |
| Sensor | `<serial>_runtime_stage_turbo` | Runtime stage Turbo | h | Disabled | Yes |  |
| Sensor | `<serial>_runtime_direction_exhaust` | Runtime exhaust | h | Disabled | Yes | udp |
| Sensor | `<serial>_runtime_direction_intake` | Runtime intake | h | Disabled | Yes | udp |
| Sensor | `<serial>_runtime_direction_stopped` | Runtime stopped or reversing | h | Disabled | Yes | udp |
| Sensor | `<serial>_reversals` | Direction reversals |  | Disabled | Yes | udp |
| Sensor | `<serial>_heat_recovery_cycles` | Heat recovery cycles |  | Disabled | Yes | udp |
| Sensor | `<serial>_half_cycle_duration` | Half cycle duration | s | Disabled | No | udp |
| Sensor | `<serial>_airflow` | Airflow | m³/h | Enabled | Yes |  |
| Sensor | `<serial>_fresh_air_flow` | Fresh air flow | m³/h | Disabled | Yes |  |
| Sensor | `<serial>_air_volume` | Air volume moved | m³ | Enabled | Yes |  |
| Sensor | `<serial>_air_change_rate` | Air change rate | 1/h | Enabled | Yes | room_volume |
| Sensor | `<serial>_power_estimate` | Estimated power | W | Enabled | Yes |  |
| Sensor | `<serial>_energy_estimate` | Estimated energy | kWh | Enabled | Yes |  |
| Sensor | `<serial>_heat_recovered_power` | Recovered heat power | W | Enabled | Yes | outdoor |
| Sensor | `<serial>_heat_recovered_energy` | Recovered heat energy | kWh | Enabled | Yes | outdoor |
| Sensor | `<serial>_ventilation_loss_power` | Ventilation heat loss power | W | Enabled | Yes | outdoor |
| Sensor | `<serial>_ventilation_loss_energy` | Ventilation heat loss energy | kWh | Enabled | Yes | outdoor |
| Sensor | `<serial>_moisture_recovered` | Recovered moisture | g/h | Disabled | No | outdoor |
| Sensor | `<serial>_dew_point` | Dew point | °C | Enabled | Yes |  |
| Sensor | `<serial>_absolute_humidity` | Absolute humidity | g/m³ | Enabled | Yes |  |
| Sensor | `<serial>_humidity_setpoint` | Humidity setpoint | % | Enabled | No |  |
| Sensor | `<serial>_humidity_setpoint_distance` | Humidity distance to setpoint | % | Enabled | Yes |  |
| Sensor | `<serial>_comfort` | Humidity comfort |  | Enabled | Yes |  |
| Sensor | `<serial>_room_temperature` | Room temperature (last valid) | °C | Enabled | Yes |  |
| Sensor | `<serial>_room_humidity` | Room humidity (last valid) | % | Enabled | Yes |  |
| Binary sensor | `<serial>_measurement_valid` | Measurement representative |  | Enabled | Yes |  |
| Sensor | `<serial>_measurement_context` | Measurement context |  | Disabled | No |  |
| Sensor | `<serial>_measurement_last_valid` | Last valid measurement |  | Disabled | No |  |
| Sensor | `<serial>_drying_potential` | Drying potential | g/m³ | Enabled | Yes | outdoor |
| Binary sensor | `<serial>_outdoor_humidifies` | Outdoor air would humidify |  | Enabled | Yes | outdoor |
| Sensor | `<serial>_air_quality_index` | Air quality index |  | Enabled | Yes |  |
| Sensor | `<serial>_filter_hours` | Filter operating hours | h | Enabled | Yes |  |
| Sensor | `<serial>_filter_remaining_hours` | Filter remaining hours | h | Enabled | Yes |  |
| Sensor | `<serial>_filter_alarm_predicted` | Filter alarm expected on |  | Enabled | No |  |
| Sensor | `<serial>_filter_clean_due` | Filter cleaning due |  | Disabled | No |  |
| Sensor | `<serial>_filter_replace_due` | Filter replacement due |  | Disabled | No |  |
| Sensor | `<serial>_filter_last_reset` | Filter last reset |  | Disabled | No |  |
| Sensor | `<serial>_filter_medium_hours` | Filter hours at Medium | h | Disabled | No |  |
| Sensor | `<serial>_filter_alarm_hours` | Filter hours at alarm | h | Disabled | No |  |
| Binary sensor | `<serial>_humidity_exhaust_active` | Humidity exhaust active |  | Enabled | Yes |  |
| Binary sensor | `<serial>_night_reduction_active` | Night reduction active |  | Enabled | Yes | udp |
| Binary sensor | `<serial>_timed_exhaust_active` | Timed extraction active |  | Enabled | Yes |  |
| Binary sensor | `<serial>_free_cooling_active` | Free cooling active |  | Enabled | Yes | udp |
| Sensor | `<serial>_smart_behaviour` | Smart behaviour |  | Enabled | Yes | udp |
| Sensor | `<serial>_timed_exhaust_ends` | Timed extraction ends |  | Disabled | No |  |
| Sensor | `<serial>_damper_state` | Damper state |  | Disabled | No |  |
| Sensor | `<serial>_humidity_alarm_duration` | Humidity alarm duration | h | Disabled | Yes |  |
| Sensor | `<serial>_humidity_alarm_today` | Humidity alarm today | min | Disabled | No |  |
| Sensor | `<serial>_humidity_alarm_current` | Humidity alarm current | min | Disabled | No |  |
| Sensor | `<serial>_night_alarm_duration` | Night alarm duration | h | Disabled | Yes |  |
| Sensor | `<serial>_night_alarm_today` | Night alarm today | min | Disabled | No |  |
| Sensor | `<serial>_night_alarm_current` | Night alarm current | min | Disabled | No |  |
| Sensor | `<serial>_command_latency` | Last command latency | s | Disabled | No |  |
| Sensor | `<serial>_command_latency_cloud` | Command latency (cloud) | s | Disabled | No |  |
| Sensor | `<serial>_command_latency_local` | Command latency (local) | s | Disabled | No |  |
| Sensor | `<serial>_command_latency_average` | Average command latency | s | Disabled | No |  |
| Sensor | `<serial>_commands_sent` | Commands sent |  | Disabled | Yes |  |
| Sensor | `<serial>_commands_confirmed` | Commands confirmed |  | Disabled | Yes |  |
| Sensor | `<serial>_commands_unconfirmed` | Commands unconfirmed |  | Disabled | Yes |  |
| Sensor | `<serial>_commands_rejected` | Commands rejected |  | Disabled | Yes |  |
| Sensor | `<house>_<zone>_zone_airflow` | Zone airflow | m³/h | Enabled | Yes |  |
| Sensor | `<house>_<zone>_zone_power` | Zone estimated power | W | Enabled | Yes |  |
| Sensor | `<house>_<zone>_zone_energy` | Zone estimated energy | kWh | Enabled | Yes |  |
| Sensor | `<house>_<zone>_zone_air_volume` | Zone air volume moved | m³ | Enabled | Yes |  |
| Sensor | `<house>_<zone>_zone_running_units` | Zone running units |  | Enabled | Yes |  |
| Sensor | `<house>_<zone>_zone_units` | Zone units |  | Disabled | No |  |
| Sensor | `<house>_<zone>_zone_heat_recovered_power` | Zone recovered heat power | W | Disabled | Yes | outdoor |
| Sensor | `<house>_<zone>_zone_heat_recovered_energy` | Zone recovered heat energy | kWh | Disabled | Yes | outdoor |
| Sensor | `<house>_house_airflow` | House airflow | m³/h | Enabled | Yes |  |
| Sensor | `<house>_house_power` | House estimated power | W | Enabled | Yes |  |
| Sensor | `<house>_house_energy` | House estimated energy | kWh | Enabled | Yes |  |
| Sensor | `<house>_house_air_volume` | House air volume moved | m³ | Enabled | Yes |  |
| Sensor | `<house>_house_running_units` | House running units |  | Enabled | Yes |  |
| Sensor | `<house>_house_units` | House units |  | Disabled | No |  |
| Sensor | `<house>_house_heat_recovered_power` | House recovered heat power | W | Disabled | Yes | outdoor |
| Sensor | `<house>_house_heat_recovered_energy` | House recovered heat energy | kWh | Disabled | Yes | outdoor |
| Sensor | `<house>_outdoor_dew_point` | Outdoor dew point | °C | Enabled | Yes | outdoor |
| Sensor | `<house>_outdoor_absolute_humidity` | Outdoor absolute humidity | g/m³ | Enabled | Yes | outdoor |
