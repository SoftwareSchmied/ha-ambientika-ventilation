<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="custom_components/ambientika_ventilation/brand/dark_icon@2x.png">
    <img src="custom_components/ambientika_ventilation/brand/icon@2x.png" alt="Ambientika Ventilation" width="128">
  </picture>
</p>

<h1 align="center">Ambientika Ventilation for Home Assistant</h1>

<p align="center">
  A modern, independent Home Assistant integration for cloud-connected<br>
  Südwind Ambientika ventilation systems.
</p>

<p align="center">
  <a href="https://github.com/SoftwareSchmied/ha-ambientika-ventilation/releases"><img src="https://img.shields.io/github/v/release/SoftwareSchmied/ha-ambientika-ventilation" alt="Latest release"></a>
  <a href="https://github.com/SoftwareSchmied/ha-ambientika-ventilation/actions/workflows/ci.yml"><img src="https://github.com/SoftwareSchmied/ha-ambientika-ventilation/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="https://github.com/SoftwareSchmied/ha-ambientika-ventilation/actions/workflows/codeql.yml"><img src="https://github.com/SoftwareSchmied/ha-ambientika-ventilation/actions/workflows/codeql.yml/badge.svg" alt="CodeQL"></a>
  <a href="https://github.com/hacs/default/pull/10223"><img src="https://img.shields.io/badge/HACS-default-41BDF5" alt="Available in the default HACS catalog"></a>
</p>

<p align="center">
  <a href="https://my.home-assistant.io/redirect/hacs_repository/?owner=SoftwareSchmied&repository=ha-ambientika-ventilation&category=integration"><img src="https://my.home-assistant.io/badges/hacs_repository.svg" alt="Open this repository in HACS"></a>
</p>

Ambientika Ventilation discovers the supported devices on an Ambientika
account and provides monitoring and safe controls without YAML.

> [!IMPORTANT]
> Version 0.9.2 adds read-only slave sensors. The integration has been validated
> against the live cloud API, the official Android app 1.5.1, and Ambientika
> Ghost installations. Initial community testing confirmed slave readings and
> master controls on Ghost Version160. Longer-term feedback and reports from
> other device families and firmware versions remain welcome.

## Quick installation

### HACS

Ambientika Ventilation is included in the
[default HACS catalog](https://github.com/hacs/default/pull/10223).

1. Open **HACS** and search for **Ambientika Ventilation**, or use the
   **Open this repository in HACS** button above.
2. Install **Ambientika Ventilation** and restart Home Assistant.
3. Open **Settings → Devices & services → Add integration** and select
   **Ambientika Ventilation**.

### Manual

Copy `custom_components/ambientika_ventilation` into the `custom_components`
directory of your Home Assistant configuration, restart Home Assistant, and
add the integration from the user interface.

To remove the integration, delete its config entry under **Settings → Devices &
services**, uninstall it in HACS, and restart Home Assistant. Removing the
config entry does not delete the account or change any device configuration in
the Ambientika cloud.

## Key features

- Home Assistant Config Flow with token refresh and reauthentication
- Dynamic discovery across houses, rooms, zones, and Gemini devices
- Fan power and speed control, including Turbo when reported by the device
- Dedicated Night operating preset that preserves the reported fan speed
- Device-specific operating modes, including both Ghost/Icon airflow directions
- Mode-aware operating mode, humidity target, and light sensitivity controls
- Schedule status/control and read-only weekly time-slot details when available
- Temperature, humidity, air quality, filter state, alarms, and schedule state
- Optional diagnostic entities for topology, firmware, role, and installation data
- Partial-failure handling, rate-limit backoff, and last-good-value retention
- Privacy-preserving diagnostics and complete English/German translations

## Requirements

- Home Assistant 2026.8.0 or newer
- An Ambientika cloud account with at least one configured ventilation unit
- Internet access from Home Assistant to `app.ambientika.eu` on TCP port 4521

This integration uses the same email address and password as the Ambientika
mobile app. The vendor API currently exposes password authentication and JWT
refresh, not OAuth or PKCE. Credentials and tokens are kept in the Home
Assistant config entry and are never written to logs or diagnostics.

## Supported devices

| Device family | Discovery | Monitoring | Control | Validation status |
| --- | --- | --- | --- | --- |
| Ambientika Ghost | Yes | Yes | Yes | Validated on two installations |
| Ambientika Diamond | Yes | Yes | Yes | API/app contract; field reports welcome |
| Ambientika Icon | Yes | Yes | Yes | API/app contract; field reports welcome |
| Ambientika Gemini | Yes | Yes | Yes | API/app contract; field reports welcome |

Non-Gemini installations are controlled through the master of each ventilation
zone. Configured slave devices also expose their own sensor values when the
cloud provides them. Slaves remain read-only; use their readings in dashboards
or as automation triggers for the zone master.

## Entities

Each controllable zone master or Gemini unit receives the controls below.
Configured slave units receive sensor and binary-sensor entities once their
first usable status arrives, in addition to static diagnostics. They receive
no fan, select, schedule switch, or filter-reset controls. Available readings
depend on the device and firmware; missing fields remain unknown. Devices
marked as not configured during discovery retain only their static diagnostics.

| Platform | Entity | Default |
| --- | --- | --- |
| Fan | Ventilation power and speed | Enabled |
| Fan preset | Night operation | Enabled |
| Select | Operating mode | Enabled |
| Select | Humidity target | Enabled |
| Select | Light sensor sensitivity | Enabled when supported |
| Sensor | Temperature | Enabled |
| Sensor | Humidity | Enabled |
| Sensor | Air quality | Enabled |
| Sensor | Filter status | Enabled |
| Binary sensor | Humidity alarm | Enabled |
| Binary sensor | Filter attention | Enabled |
| Binary sensor | Night detected | Enabled |
| Binary sensor | Schedule active | Enabled |
| Binary sensor | Cloud connection (diagnostic, stays available while a unit is offline) | Enabled |
| Switch | Schedule control | Enabled when supported |
| Button | Reset filter status | Available when replacement is due |
| Calendar | Weekly schedule (editable when supported) | Enabled when a schedule exists |
| Sensor | Signal strength | Disabled |
| Sensor | Last operating mode | Disabled |
| Sensor | Weekly schedule entries and details | Disabled when available |
| Sensors | Device role, type, subtype, and cloud IDs | Disabled |
| Sensors | Installation and firmware versions | Disabled |
| Sensors | House, zone, and room assignment | Disabled when available |

Unknown API values become unavailable for the affected entity instead of
stopping the integration. New devices and newly reported light/Turbo
capabilities are adopted without restarting Home Assistant.

Manual writes follow the same safety rules as app version 1.5.1. Fan speed,
humidity target, and light sensitivity are accepted only in operating modes
where the app enables them. Manual controls are locked while a weekly schedule
is active and while the filter status is `Bad`; schedule deactivation and filter
reset remain available. The two airflow-direction modes are offered only for
non-Gemini units, while Gemini units also omit Away mode.

## Schedules

Units that report a weekly schedule get a calendar entity named "Schedule".
Every time slot of the schedule appears as a weekly recurring event in the
house time zone, with the operating mode, fan speed and humidity target in the
title. Units whose schedule state is `NotAvailable` (the common case when no
schedule was ever created in the Ambientika app) get no calendar.

**Editing.** The calendar is editable when the account has the weekly scheduler
feature and the unit reports a schedule. Otherwise it is read-only and the
actions below fail with a clear message. A time slot always lies within one day,
starts before it ends, has zero seconds and must not overlap another slot of
the same day. Fan speed `Night` is never written; Turbo only when the unit
allows it.

*Through the calendar.* Create, edit or delete events in the calendar card. Only
events that repeat weekly on their own weekday, without end date, count or
interval, can be stored; anything else, all-day events and changes to a single
occurrence are rejected with an explanation. Write the operating mode as the
title (for example `Night`), and optionally the other fields in the description:

```text
operating_mode: heat_recovery
fan_speed: medium
humidity_level: normal
light_sensor_level: off
```

*Through actions* (target the calendar entity or the device):

| Action | Purpose |
| --- | --- |
| `ambientika_ventilation.set_time_slot` | Create a slot (`day`, `start`, `end`, `operating_mode` required) or change one (`slot_id`). Unspecified values default to low speed, normal humidity, light sensor off on creation and stay unchanged on update. |
| `ambientika_ventilation.delete_time_slot` | Delete one slot by `slot_id`. |
| `ambientika_ventilation.clear_schedule` | Delete every slot; needs `confirm: true`. |
| `ambientika_ventilation.get_schedule` | Response-only: returns the slots with their `slot_id`. |

```yaml
action: ambientika_ventilation.set_time_slot
target:
  entity_id: calendar.living_room_schedule
data:
  day: monday
  start: "22:00:00"
  end: "23:30:00"
  operating_mode: night
```

The write endpoints come from the official app and are not yet verified against
a unit with an active schedule. Please report unexpected rejections with the
diagnostics download.

## Reliability and cloud usage

Device status is refreshed every 60 seconds by default, preferably through one
aggregate request per house. The interval can be changed under **Configure**
(30 to 600 seconds; the unit itself reports only about every 30 seconds). After
a command the integration polls every 30 seconds for three minutes. While the
cloud is unreachable it slows down step by step to at most ten minutes, and it
returns to the normal pace with the first successful poll. Missing packets
automatically fall back to per-device requests. Discovery metadata and server
features are refreshed every six hours; names, roles, and firmware versions are
also read from every status response, so a firmware update or rename shows up
within one poll (the device registry is updated and an
`ambientika_ventilation_device_changed` event is fired). Weekly schedules are
requested only for units that report a schedule, and as soon as one starts
reporting it. Poll requests make a single attempt, because the next poll is the
retry; commands and discovery retry with bounded exponential backoff and
jitter. Requests are limited to three in parallel, with a separate lane for
commands. Each request times out after 10 seconds. A failure of one optional
resource or device retains its last good data and does not block other devices.
Affected entities are marked unavailable until their next successful update. An
empty discovery answer for an account that had devices is treated as a cloud
error and the known devices are kept. The integration uses its own connection
pool that keeps connections open for two minutes, which avoids a new TLS
handshake with every poll, and it renews the login token 30 days before it
expires.

For each configured slave missing from the house batch, polling adds one
individual status request per cycle. A unit whose status request is rejected
with HTTP 403 or 404 is asked again after 1, 2, 5, and 15 minutes and is only
skipped until the next discovery (normally every six hours) after four
consecutive rejections, so a short outage does not disable it. Empty status
packets and packets identifying another device are not used as that device's
readings. The API supplies no measurement timestamp, so a successful cloud read
cannot establish when the unit last measured a value. Entities update only when
a poll brings new data.

Turbo is offered only when the unit reports it as available and the server does
not switch the `turboMode` feature off (a missing flag counts as on). Field names
are read regardless of capitalization. The legacy Turbo operating mode is shown
read-only in the "Last operating mode" sensor. Unknown enum values show as
unknown and keep the raw text in the `raw_value` attribute.

Controls send only values documented as writable by the current API and
confirmed by the official Android app. Every write sends a complete, validated
state. Commands for the same unit that arrive within about 0.4 seconds are
merged into one request and are based on the last known state plus commands not
yet confirmed, so a scene that sets mode and humidity no longer overwrites
itself. The written values are shown at once and read back after 3 seconds (a
second time after another 3 seconds), because the cloud needs about 0.2 to 3
seconds to reflect a command. A poll that was started before or right after a
command never overwrites the commanded values.

The action `ambientika_ventilation.set_state` sets operating mode, fan speed,
humidity target, and light sensor sensitivity of one or more units in a single
command; only the given values change and the usual mode rules apply.

## Migrating from the former integration

This project deliberately uses the new `ambientika_ventilation` domain so it
does not inherit unstable config entries or name-based unique IDs from the
former `ambientika` integration. Remove the former integration, restart Home
Assistant, install this project, and configure the account again.

The previous climate entity is not retained because a ventilation unit is not
an HVAC thermostat. It is replaced by a fan entity plus explicit selects. Any
automation that targeted the old climate entity must be updated once.

Back up the Home Assistant configuration before replacing an installed custom
integration. Existing automations must be updated to reference the new entities.

## Diagnostics and privacy

Download diagnostics from **Settings → Devices & services → Ambientika
Ventilation → ⋮ → Download diagnostics**. Diagnostics include integration and
entry versions, detected capabilities, request/status counters, update
duration and its recent history, failure causes, token expiry date, request
metrics including the structure (keys and types, never values) of the last
response per endpoint, a firmware history, and active conditions; per-device
diagnostics are available too. Passwords, JWTs, email addresses, room/house
names, full serial numbers, exact API payloads, and callback URLs are excluded.

## Troubleshooting

- **Invalid credentials:** Sign in to the mobile app with the same credentials,
  then use **Reconfigure** in Home Assistant.
- **No devices found:** Confirm the account already contains a fully configured
  Ambientika device.
- **Values update slowly:** The unit first reports to the vendor cloud; Home
  Assistant then polls that status. A short delay is expected.
- **Rate limiting:** Leave the default interval unchanged. Diagnostics count
  rate-limit events without exposing request content.
- **A single device is unavailable:** Confirm it is online in the Ambientika
  app. Other devices continue updating during an isolated failure.
- **Controls are unavailable:** Disable the weekly schedule first. If the
  filter status is `Bad`, replace the filter and use **Reset filter status**.

For a support request, attach downloaded diagnostics. Keep raw cloud responses,
tokens, packet captures, and account details private.

## Supported and unsupported functionality

Supported API resources and data contracts are listed in
[`docs/API_REFERENCE.md`](docs/API_REFERENCE.md). Device factory reset, account
management, house/zone reconfiguration, and firmware operations are
deliberately not exposed. Local TCP control is a separate architecture and is
not part of this cloud integration.

## Typical automations

- Increase ventilation when measured humidity rises in a bathroom.
- Switch to Night mode as part of a bedtime scene.
- Notify when the filter requires attention.
- Disable a weekly schedule before applying a temporary manual mode.

Use Home Assistant's entity picker when creating automations so that stable
entity registry IDs are used. Write actions can fail safely when a device is
offline, scheduled, blocked by its filter state, or does not support the chosen
mode.

## Development

See [`CONTRIBUTING.md`](CONTRIBUTING.md) and
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md). The complete entity catalog is
in [`docs/ENTITIES.md`](docs/ENTITIES.md), and the assessed path to a Home
Assistant Core submission is documented in
[`docs/CORE_READINESS.md`](docs/CORE_READINESS.md).

The project is licensed under the MIT License and is not affiliated with or
endorsed by Südwind s.r.l. Ambientika and Südwind are trademarks of their
respective owner.
