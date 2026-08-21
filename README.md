# Ambientika Ventilation for Home Assistant

Ambientika is a native Home Assistant integration for cloud-connected Südwind
Ambientika ventilation units. It discovers every supported device on an
Ambientika account and provides monitoring and safe controls without YAML.

> [!IMPORTANT]
> Version 0.1.0 is an engineering preview based on the manufacturer's live
> public OpenAPI specification. It still requires validation with multiple
> physical devices and firmware versions before a stable release.

## Features

- Home Assistant Config Flow with token refresh and reauthentication
- Dynamic discovery across houses, rooms, zones, and Gemini devices
- Fan power and speed control, including Turbo when reported by the device
- Dedicated Night preset instead of treating Night as a numeric fan speed
- Operating mode, humidity target, and light sensitivity controls
- Schedule status/control and read-only weekly time-slot details when available
- Temperature, humidity, air quality, filter state, alarms, and schedule state
- Optional diagnostic entities for topology, firmware, role, and installation data
- Partial-failure handling, rate-limit backoff, and last-good-value retention
- Privacy-preserving diagnostics and complete English/German translations

## Requirements

- Home Assistant 2025.6.0 or newer
- An Ambientika cloud account with at least one configured ventilation unit
- Internet access from Home Assistant to `app.ambientika.eu` on TCP port 4521

This integration uses the same email address and password as the Ambientika
mobile app. The vendor API currently exposes password authentication and JWT
refresh, not OAuth or PKCE. Credentials and tokens are kept in the Home
Assistant config entry and are never written to logs or diagnostics.

## Installation

### HACS

1. Open **HACS → Integrations → ⋮ → Custom repositories**.
2. Add the repository URL as an **Integration**.
3. Install **Ambientika Ventilation** and restart Home Assistant.
4. Open **Settings → Devices & services → Add integration → Ambientika Ventilation**.

### Manual

Copy `custom_components/ambientika_ventilation` into the `custom_components` directory of
your Home Assistant configuration, restart Home Assistant, and add the
integration from the user interface.

## Entities

Each discovered ventilation unit receives:

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
| Button | Reset filter status | Enabled |
| Sensor | Signal strength | Disabled |
| Sensor | Last operating mode | Disabled |
| Sensor | Weekly schedule entries and details | Disabled when available |
| Sensors | Device role, type, subtype, and cloud IDs | Disabled |
| Sensors | Installation and firmware versions | Disabled |
| Sensors | House, zone, and room assignment | Disabled when available |

Unknown API values become unavailable for the affected entity instead of
stopping the integration. New devices and newly reported light/Turbo
capabilities are adopted without restarting Home Assistant.

## Polling and cloud usage

Device status is refreshed every 60 seconds, preferably through one aggregate
request per house. Missing packets automatically fall back to per-device
requests. Discovery metadata, weekly schedules, and server features are
refreshed every six hours. Requests are limited to three in parallel. HTTP 429
and temporary server errors use bounded exponential backoff with jitter. A
failure of one optional resource or device retains its last good data and does
not block other devices.

Controls send only values documented as writable by the current API. Every
write sends a complete, validated state and immediately reads the device state
back so that cloud-side rounding or rejection is visible in Home Assistant.

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

For a support request, attach downloaded diagnostics—not raw cloud responses,
tokens, packet captures, or account details.

## Supported and unsupported functionality

Supported API resources and data contracts are listed in
[`docs/API_REFERENCE.md`](docs/API_REFERENCE.md). Device factory reset, account
management, house/zone reconfiguration, schedules, and firmware operations are
deliberately not exposed. Local TCP control is a separate architecture and is
not part of this cloud integration.

## Development

See [`CONTRIBUTING.md`](CONTRIBUTING.md) and
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md). The project is licensed under
the MIT License and is not affiliated with or endorsed by Südwind s.r.l.
