# Architecture

One config entry represents one Ambientika cloud account. It owns one shared
`aiohttp` session-backed API client and one `DataUpdateCoordinator`.

The client centralizes authentication, token refresh, concurrency limiting,
HTTP classification, backoff, JSON parsing, and non-sensitive request metrics.
The coordinator refreshes static discovery and read-only schedules separately
from dynamic status. Status polling prefers one aggregate request per house and
falls back to individual devices for missing packets. Successful device data is
retained through partial outages, and commands are followed by an immediate
read-back.

Non-Gemini controls are master-oriented, matching the official app's zone
model. Status polling includes configured slaves, independently of whether a
device is controllable. A usable house-batch packet avoids an individual
request; a missing or empty packet triggers per-device fallback. Individual
responses identifying a different device are rejected. A packet must contain
at least one parsed dynamic value; metadata alone is insufficient. Missing
measurement fields remain unknown, and false/zero values remain valid.

HTTP 403/404 status failures are cached until the next successful discovery,
normally six hours later. A usable batch packet can recover the device sooner.
Transient errors remain eligible for the next regular poll. A failed device's
previous readings are retained but its entities are unavailable. Discovery
also removes obsolete failure suppression for removed or reconfigured devices.

Slave sensor and binary-sensor entities are added dynamically when status first
becomes available. Controls are prohibited if either discovery or status marks
the device as a slave or not configured, including when those sources disagree.
After promotion to master, both sources must stop reporting a read-only role
before controls become usable. Devices marked as not configured during
discovery are excluded from status polling and retain only static diagnostics.
Schedule discovery remains limited to controllable devices.

The coordinator is the single enforcement point for device-type mode lists,
mode-dependent writable fields, schedule/manual exclusion, and the bad-filter
write lock.

Platforms consume immutable combined device/status models. Their entity unique
IDs use `<serial>_<entity-key>`; device registry identifiers use
`(ambientika_ventilation, <serial>)`. These identifiers are compatibility
contracts and must not change after release.

New device serials are detected by platform listeners after coordinator
updates. Optional controls are instantiated only after the corresponding
capability is observed. Unknown enum values are retained in the model but are
not emitted as invalid Home Assistant enum states.

The parser deliberately accepts both documented and app-observed wire shapes,
including numeric/string weekdays, numeric/string enums, legacy role `NC`, and
both signal-strength spellings.

## Security boundaries

The integration sends credentials only to the fixed HTTPS Ambientika API host.
It logs neither credentials nor bodies. Diagnostics use redacted identifier
suffixes and omit user-created names and full cloud payloads. The vendor API
does not currently offer OAuth/PKCE; this limitation is documented rather than
hidden behind a custom token scheme.
