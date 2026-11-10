# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

- Merge commands for one unit that arrive together (for example mode and
  humidity target in a scene) into a single cloud request, based on the last
  known state plus commands not yet confirmed. Add the action
  `ambientika_ventilation.set_state` to set mode, fan speed, humidity target and
  light sensitivity atomically. The written values appear at once instead of
  after the read-back.
- A poll that started before or shortly after a command no longer overwrites the
  commanded values with the old state the cloud still returns.
- Turbo is only offered when the server feature flag `turboMode` is not switched
  off (a missing flag counts as on). Status fields are read regardless of
  capitalization (for example `IsTurboAvailable`). The legacy Turbo operating
  mode is shown read-only; unknown enum values show as unknown with a
  `raw_value` attribute instead of failing.
- A unit whose status request is rejected with HTTP 403 or 404 is retried after
  1, 2, 5 and 15 minutes and only treated as unsupported after four consecutive
  rejections. New binary sensor "Cloud connection" per unit, which stays
  available while the unit is offline.
- An empty discovery answer no longer removes all known devices; it is handled
  like any other discovery failure (previous list kept, retry with backoff).
- Poll requests make a single attempt (the next poll is the retry). While the
  cloud is unreachable the poll interval grows up to ten minutes. Requests time
  out after 10 s (5 s to connect), a status update has a time budget below the
  interval, and commands have their own request lane so a hanging poll cannot
  block them.
- Renew the login token when fewer than 30 days remain (checked daily), and do
  not log in again when another request already renewed the token.
- Use a dedicated connection pool with 120 s keep-alive instead of Home
  Assistant's shared session, which closes idle connections after 15 s. This
  avoids a TLS handshake on every poll.
- Add an options flow for the poll interval (30 to 600 s, default 60 s). After a
  command the cloud is polled every 30 s for three minutes.
- Update entities only when a poll brings changed data. Platforms look for new
  entities only when the set of devices or their capabilities changes.
- Read names, roles and firmware versions from every house status response:
  update the device registry, fire `ambientika_ventilation_device_changed`,
  start an early discovery for unknown devices, and keep a firmware history in
  diagnostics.
- Request weekly schedules only for units that report a schedule and as soon as
  one starts reporting it, instead of probing every device every six hours.
- Signal strength 0 from firmware without a signal field is shown as unknown.
  The operating mode options are computed from the current device data.
- Extend diagnostics: recent poll durations, interval and failure streak, token
  expiry, request structure per endpoint, queue wait, discarded polls, and
  per-device diagnostics.
- Authentication errors while reading optional house metadata or feature flags
  are no longer swallowed.

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

[Unreleased]: https://github.com/SoftwareSchmied/ha-ambientika-ventilation/compare/v0.9.2...HEAD
[0.9.2]: https://github.com/SoftwareSchmied/ha-ambientika-ventilation/compare/v0.9.1...v0.9.2
[0.9.2-beta.1]: https://github.com/SoftwareSchmied/ha-ambientika-ventilation/compare/v0.9.1...v0.9.2-beta.1
[0.9.1]: https://github.com/SoftwareSchmied/ha-ambientika-ventilation/compare/v0.9.0...v0.9.1
[0.9.0]: https://github.com/SoftwareSchmied/ha-ambientika-ventilation/releases/tag/v0.9.0
