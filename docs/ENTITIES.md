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
| Sensor | `<serial>_last_local_frame` | Time of the last local frame (diagnostic, written at most once per minute) | Enabled | Yes |
| Sensor | `<serial>_last_seen` | Latest sign of life from cloud or local (diagnostic, written at most once per minute) | Enabled | Yes |
| Sensor | `<serial>_last_seen_cloud`, `_last_seen_local` | Raw value of one source | Disabled | Yes |
| Sensor | `<serial>_heartbeat_age` | Seconds since the last local frame (diagnostic, written at most once per minute) | Disabled | Snapshot every minute, no statistics |
| Binary sensor | `<entry id>_cloud_reachable` | Cloud delivered data recently and without repeated failures (account device) | Enabled | Yes |
| Sensor | `<entry id>_cloud_consecutive_failures` | Failed polls in a row (account device) | Enabled | Yes |
| Sensor | `<entry id>_cloud_success_rate`, `_cloud_last_latency`, `_cloud_last_success`, `_cloud_last_failure_kind` | Request health (account device) | Disabled | Yes |

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

House, room, and zone sensors can expose related identifiers and metadata as
attributes. House attributes can include address and coordinates supplied by
the cloud, so consider recorder retention and dashboard visibility before
enabling that entity.

Unknown or absent API values are represented as unknown. An entity is marked
unavailable after a failed update for its device and recovers automatically on
the next successful poll.
