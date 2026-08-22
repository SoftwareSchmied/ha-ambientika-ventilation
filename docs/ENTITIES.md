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

Manual controls are intentionally rejected while a schedule is active or the
filter state is `Bad`. The schedule can still be disabled and the filter can
still be reset.

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
