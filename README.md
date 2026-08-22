# Ambientika Ventilation for Home Assistant

[![CI](https://github.com/SoftwareSchmied/ha-ambientika-ventilation/actions/workflows/ci.yml/badge.svg)](https://github.com/SoftwareSchmied/ha-ambientika-ventilation/actions/workflows/ci.yml)
[![CodeQL](https://github.com/SoftwareSchmied/ha-ambientika-ventilation/actions/workflows/codeql.yml/badge.svg)](https://github.com/SoftwareSchmied/ha-ambientika-ventilation/actions/workflows/codeql.yml)
[![GitHub release](https://img.shields.io/github/v/release/SoftwareSchmied/ha-ambientika-ventilation)](https://github.com/SoftwareSchmied/ha-ambientika-ventilation/releases)
[![HACS validation](https://img.shields.io/badge/HACS-validated-41BDF5.svg)](https://hacs.xyz/docs/publish/integration/)

Ambientika is a native Home Assistant integration for cloud-connected Südwind
Ambientika ventilation units. It discovers every supported device on an
Ambientika account and provides monitoring and safe controls without YAML.

> [!IMPORTANT]
> Version 0.9.0 is a public beta. It has been validated against the live cloud
> API, the official Android app 1.5.1, and two Ambientika Ghost installations.
> Reports from other device families and firmware versions remain welcome.

## Features

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
zone. Slave devices are retained as diagnostic devices and are never sent
duplicate commands.

## Installation

### HACS

1. Open this repository in HACS using the button below, or add its URL under
   **HACS → Integrations → ⋮ → Custom repositories** as an **Integration**.
2. Install **Ambientika Ventilation** and restart Home Assistant.
3. Open **Settings → Devices & services → Add integration → Ambientika Ventilation**.

[![Open your Home Assistant instance and add this repository to HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=SoftwareSchmied&repository=ha-ambientika-ventilation&category=integration)

### Manual

Copy `custom_components/ambientika_ventilation` into the `custom_components` directory of
your Home Assistant configuration, restart Home Assistant, and add the
integration from the user interface.

To remove the integration, delete its config entry under **Settings → Devices &
services**, uninstall it in HACS, and restart Home Assistant. Removing the
config entry does not delete the account or change any device configuration in
the Ambientika cloud.

## Entities

Each controllable zone master or Gemini unit receives the controls below.
Configured slave units retain their static diagnostic entities without duplicate
zone controls.

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
| Switch | Schedule control | Enabled when supported |
| Button | Reset filter status | Available when replacement is due |
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

## Polling and cloud usage

Device status is refreshed every 60 seconds, preferably through one aggregate
request per house. Missing packets automatically fall back to per-device
requests. Discovery metadata, weekly schedules, and server features are
refreshed every six hours. Requests are limited to three in parallel. HTTP 429
and temporary server errors use bounded exponential backoff with jitter. A
failure of one optional resource or device retains its last good data and does
not block other devices. Affected entities are marked unavailable until their
next successful update.

Controls send only values documented as writable by the current API and
confirmed by the official Android app. Every write sends a complete, validated
state and immediately reads the device state back so that cloud-side rounding
or rejection is visible in Home Assistant.

## Moving from the former integration

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
duration, and active conditions. Passwords, JWTs, email addresses, room/house
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

For a support request, attach downloaded diagnostics—not raw cloud responses,
tokens, packet captures, or account details.

## Supported and unsupported functionality

Supported API resources and data contracts are listed in
[`docs/API_REFERENCE.md`](docs/API_REFERENCE.md). Device factory reset, account
management, house/zone reconfiguration, schedules, and firmware operations are
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
