# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [1.0.0-beta.1] - 2026-11-10

First beta of 1.0. It combines the cloud integration with the local UDP
broadcasts of the units. It also adds about 100 derived values, schedules as a
calendar, and a large robustness and efficiency overhaul. Existing entity IDs,
history and automations are kept. Tested on Home Assistant 2026.11.1 with two
Ghost masters in separate zones. The minimum Home Assistant version is now
2026.9.0.

### Local data (UDP) and hybrid model

- Passive reception of the UDP broadcasts of the zone masters (ports 45000 and
  up; nothing is sent). They carry the physical state, which is the zone
  coupling, airflow direction and fan stage. They arrive every 27 s and
  immediately on every change.
- New option "Local UDP reception" with the values automatic, on and off.
  Automatic is the default: it listens for up to 60 s in the background after
  setup and remembers the result.
- Hybrid model: values available from both sources (power, running, last seen)
  get an effective entity that prefers the local value while it is fresh. They
  also get `(Cloud)` and `(local)` variants, which are disabled unless the
  option "Create raw source variants" is on. Controls and selects keep
  following the cloud setting.
- New entities per unit:
  - power, running, physical direction, physical fan stage and zone coupling
  - last seen, both effective and local
  - data source (`local_udp`, `cloud`, `degraded`, `offline`)
  - heartbeat age (disabled)

  A new "Ambientika cloud" device shows reachability and request health.
- Commands are confirmed locally about 6 s after they are sent. A change
  detected over UDP asks the cloud for confirmation at most every 30 s.
- Frames are checked for length, header, zone, ports, house id, sender and
  rate. Local data is informative only and never bypasses locks.
- Repair issues explain bind failures and missing frames in mode "on". The
  diagnostics show the result of the last automatic probe: bound ports, frames
  heard and rejected frames.

### Network requirements for local data

- Home Assistant must be in the same network (layer 2 segment or VLAN) as the
  units. Routers never forward broadcasts, and firewall rules between VLANs do
  not change that.
- Home Assistant OS and Supervised work directly when they share the network
  with the units. Docker needs `network_mode: host`.
- Units in an IoT VLAN need one of the following:
  - an interface for Home Assistant in that VLAN; on Home Assistant OS use
    `ha network vlan <interface> <id>` with a static address and no gateway
    (see the README)
  - a second virtual network card with the VLAN tag in the hypervisor

  Without local frames, everything keeps working with the cloud.

### Derived data

- About 100 derived sensors per unit, zone and house. Most are disabled by
  default. They cover:
  - runtimes per mode, stage and direction, reversals and heat-recovery cycles
  - airflow, moved air volume and the air change rate (needs a room volume)
  - estimated power and energy
  - recovered heat and ventilation heat loss (needs an outdoor source)
  - dew point, absolute humidity, distance to the humidity setpoint, comfort
    class and a numeric air quality
  - filter hours, remaining hours and the predicted alarm date
  - visible automatic actions: humidity exhaust, night reduction, timed
    extraction, Smart behaviour and free cooling
  - measurement validity with the last valid room values
  - alarm durations and command statistics
  - zone and house totals
- New options: an outdoor weather entity or outdoor temperature and humidity
  sensors (°C, °F, K), the filter hours basis, and the room volume per unit.
- Estimates are marked as such, and counters survive restarts. Formulas and
  sources are documented in `docs/DERIVED_DATA.md`.
- While a unit stands still it measures the air in the wall duct. Climate
  values derived from these readings use the last valid reading instead.

### Schedules

- Weekly schedules appear as a calendar for each unit that has a schedule, in
  the house time zone. Events can be created, changed and deleted when the
  account has the weekly scheduler feature.
- New actions `set_time_slot`, `delete_time_slot`, `clear_schedule` (needs
  `confirm: true`) and `get_schedule` (response only). Values and overlaps are
  checked before anything is sent.
- The write endpoints come from the official app. They are not yet verified
  against a unit with an active schedule.

### Cloud robustness and efficiency

- A dedicated connection pool with 120 s keep-alive replaces the new TLS
  handshake on every poll.
- Poll interval:
  - a new option, 30 to 600 s (default 60 s)
  - 30 s for three minutes after a command
  - growing to ten minutes while the cloud is unreachable
- Polls make a single attempt, requests time out after 10 s, and commands have
  their own request lane.
- Commands for one unit that arrive together are merged into one request. The
  new action `set_state` sets the mode, fan speed, humidity target and light
  sensitivity at once.
- A poll that started before a command no longer overwrites it. Confirmed
  read-backs reach the hub immediately.
- Entities update only when data changes. Names, roles and firmware are read
  from every status response, and the diagnostics keep a firmware history.
- Schedules are requested only for units that report one.
- The login token is renewed when fewer than 30 days remain. A failed renewal
  keeps the token that is still valid.
- Units rejected with HTTP 403 or 404 are retried after 1, 2, 5 and 15 minutes
  before they count as unsupported. An empty discovery no longer removes all
  devices. There is a new binary sensor "Cloud connection" per unit.
- Turbo respects the `turboMode` feature flag. Unknown enum values show as
  unknown with a `raw_value` attribute.

### Quality and privacy

- Stale devices are removed after four successful discoveries without them,
  and only once at least 24 hours have passed. Devices the cloud no longer
  reports can also be removed manually.
- DHCP discovery for the hostnames `VMC_*` and `GMN_*`.
- New option "Do not store the password": only the 180-day token is stored and
  renewed. If renewal fails, reauthentication starts.
- The house sensor no longer has address, coordinates, time zone and house
  time attributes. These values are now separate diagnostic sensors on a new
  house device, disabled by default. No entity attribute exposes serials,
  house ids or locations.
- New `quality_scale.yaml`, icon translations, translated exceptions,
  parallel update limits, and extended diagnostics with masked serials.
- Test coverage is enforced at 98 %. CI runs with Python 3.14.7, plus a
  non-blocking job against the newest Home Assistant release.

### Fixed during the beta test

- Entity actions are now registered with field dictionaries. Home Assistant
  2026.11 rejected the previous schema and did not load the integration.
- No fan stage, airflow or power is reported while the fan stands still, both
  after a command (about 45 s) and during the ~10 s reversal.
- The "no frames" repair issue is no longer reset by internal freshness
  reports.
- A new sender address is accepted after about 81 s instead of five minutes.

### Known limitations

- Setup still needs one successful cloud poll. Starting from the cached
  topology while the cloud is down is planned for a later release.
- Temperature, humidity, air quality and filter state come only from the cloud,
  because the UDP broadcast does not carry them.

### Maintenance

- The test stack is updated to Home Assistant 2026.10.4 with
  pytest-homeassistant-custom-component 0.13.375.
- Ruff is updated to 0.17.4 and Mypy to 2.4.1, and the GitHub Actions
  setup-python, hassfest and CodeQL are updated, all from Dependabot #18, #20
  and #22.
- Home Assistant 2026.11.0, pytest 9.2.0 and pytest-asyncio 1.4.1 from #22 are
  not used, because the newest test plugin pins Home Assistant 2026.10.4,
  pytest 9.0.3 and pytest-asyncio 1.4.0.
- The Mypy configuration treats `voluptuous`, which is an alias of `probatio`
  inside Home Assistant, as untyped, and allows the platform re-exports of
  Home Assistant.

## [0.9.3] - 2026-11-10

- Fix commands failing while a unit reports the automatic `Night` fan speed,
  including turning off and changing the humidity target, light sensitivity,
  or schedule control. Reported values the cloud does not accept are replaced
  by the unit's last writable value, falling back to `Low` for the fan speed
  and `Normal` for the humidity target. Unknown values are never written back.
- Report a unit that does not acknowledge a command (HTTP 406) as offline
  instead of as a generic cloud error. Removed devices, deleted houses, houses
  without a master, and missing schedules are now distinguished, and devices
  are rediscovered early when a house or device disappears. A validation error
  during login is no longer reported as invalid credentials.
- Detect powered-off units: when the cloud has no current status packet
  (HTTP 404 "Status packet not found!"), the unit's entities become
  unavailable, it keeps being polled every minute, and it recovers on the next
  poll after reconnecting. Going offline and coming back are each logged once
  and counted in diagnostics.
- Show the actual result of a command: the state is read back after 3 s,
  retried once after another 3 s, and otherwise the written values are shown
  until the next poll, instead of the stale state the cloud returns right
  after a command. A failed read-back no longer reports an accepted command as
  failed. The cloud also accepts commands for powered-off units, so a
  successful command does not prove delivery.
- Start reauthentication when a command fails because the cloud rejected the
  stored credentials.
- Name the cause of polling failures (timeout, transport error, HTTP status,
  cloud trace ID) in the single outage log entry and in diagnostics, with
  counters per failure kind. Serial numbers remain masked.
- Reduce cloud load during outages. A failed discovery is retried after 5, 10,
  and 30 minutes, up to six hours, instead of every minute. A failed house
  status request triggers at most one individual device probe per house.
- Add a reconfiguration flow for updating the account credentials, as already
  referenced in the troubleshooting section.
- The error responses, the offline behavior of a powered-off unit, and the
  command delay were checked against a live installation. The `Night`
  handling follows the official app and has not yet been observed on a unit
  at night.

## [0.9.2] - 2026-10-20

- Add read-only sensor and binary-sensor readings for configured slave devices
  when the cloud provides them. Master controls and existing entity IDs are
  preserved, including when cloud role information disagrees.
- Prefer house-wide status polling, with individual requests for devices
  missing usable readings. Retry unsupported endpoints after discovery and
  isolate device failures. Reject empty or mismatched status packets.
- Update the test environment to Home Assistant 2026.9.4 with compatible
  test plugin 0.13.367 and pytest 9.0.3.
- Update Ruff to 0.16.10, Mypy to 2.4.0, and the hassfest and CodeQL workflows.
- Document direct installation from the default HACS catalog.
- Initial community testing confirmed slave readings and master controls on
  Ghost Version160. Issue #11 remains open for longer-term feedback.

## [0.9.2-beta.1] - 2026-10-03

- Expose configured slave devices' sensor and binary-sensor readings when the
  cloud provides device-level status, with per-device fallback after house
  polling. Slaves remain read-only, including conflicting cloud role reports.
- Retry unsupported status endpoints after device discovery and recover from
  per-device outages without interrupting other devices. Reject empty status
  packets and individual responses identifying another device.
- Update the test environment to Home Assistant 2026.9.4 and its matching
  test plugin, retaining pytest 9.0.3 as required by that plugin.
- Update Ruff and its pre-commit hook to 0.16.9, plus hassfest and CodeQL
  workflow dependencies. This preview combines PRs #12, #13, #14, and #15.
- Publish for testing on physical master/slave installations; the maintainer
  has no slave units and cannot validate their measurements locally.

## [0.9.1] - 2026-09-28

- Refine night detection so Home Assistant presents the Ambientika night
  signal with neutral binary-state wording instead of light-specific wording.
  Entity IDs and automation states remain unchanged.
- Present the vendor's unitless signal-strength value as a numeric measurement
  so Home Assistant can display its history as a measurement graph.
- Validate the integration against Home Assistant 2026.9.1 and refresh the
  matching Home Assistant test environment.
- Update Ruff, hassfest, CodeQL, and repository automation dependencies.

## [0.9.0] - 2026-08-22

- Publish the first public beta.
- Add UI-only account setup, token refresh, and reauthentication.
- Add dynamic multi-house and multi-device discovery.
- Add fan, sensor, binary sensor, select, and button platforms.
- Add validated state writes with immediate read-back.
- Add resilient polling, request metrics, redacted diagnostics, and DE/EN UI.
- Publish under the `ambientika_ventilation` domain as Ambientika Ventilation.
- Add aggregate house polling with per-device fallback.
- Add schedule control, read-only time-slot details, and a Night fan preset.
- Add optional diagnostic entities for all useful device metadata.
- Align Night, fan speed, operating-mode availability, and write locks with the
  official Android app 1.5.1.
- Add Ghost/Icon airflow modes and Gemini-specific mode restrictions.
- Normalize numeric schedule weekdays and expose complete schedule/topology
  identifiers plus house metadata.
- Poll and control non-Gemini zones through their master device.
- Add translated action errors and per-device availability tracking.
- Add reproducible release archives, HACS/hassfest validation, security checks,
  repository templates, and complete release documentation.

[Unreleased]: https://github.com/SoftwareSchmied/ha-ambientika-ventilation/compare/v1.0.0-beta.1...HEAD
[1.0.0-beta.1]: https://github.com/SoftwareSchmied/ha-ambientika-ventilation/compare/v0.9.3...v1.0.0-beta.1
[0.9.3]: https://github.com/SoftwareSchmied/ha-ambientika-ventilation/compare/v0.9.2...v0.9.3
[0.9.2]: https://github.com/SoftwareSchmied/ha-ambientika-ventilation/compare/v0.9.1...v0.9.2
[0.9.2-beta.1]: https://github.com/SoftwareSchmied/ha-ambientika-ventilation/compare/v0.9.1...v0.9.2-beta.1
[0.9.1]: https://github.com/SoftwareSchmied/ha-ambientika-ventilation/compare/v0.9.0...v0.9.1
[0.9.0]: https://github.com/SoftwareSchmied/ha-ambientika-ventilation/releases/tag/v0.9.0
