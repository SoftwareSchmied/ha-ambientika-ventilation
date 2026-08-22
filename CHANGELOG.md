# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

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

[Unreleased]: https://github.com/SoftwareSchmied/ha-ambientika-ventilation/compare/v0.9.0...HEAD
[0.9.0]: https://github.com/SoftwareSchmied/ha-ambientika-ventilation/releases/tag/v0.9.0
