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

Platforms consume immutable combined device/status models. Their entity unique
IDs use `<serial>_<entity-key>`; device registry identifiers use
`(ambientika_ventilation, <serial>)`. These identifiers are compatibility
contracts and must not change after release.

New device serials are detected by platform listeners after coordinator
updates. Optional controls are instantiated only after the corresponding
capability is observed. Unknown enum values are retained in the model but are
not emitted as invalid Home Assistant enum states.

## Security boundaries

The integration sends credentials only to the fixed HTTPS Ambientika API host.
It logs neither credentials nor bodies. Diagnostics use redacted identifier
suffixes and omit user-created names and full cloud payloads. The vendor API
does not currently offer OAuth/PKCE; this limitation is documented rather than
hidden behind a custom token scheme.
