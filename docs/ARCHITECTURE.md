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

## Hybrid data model (cloud + local)

Status of the work: `datapoints.py` and `hub.py` are pure Python (no Home
Assistant imports) and not yet wired into the coordinator or the platforms.

Ambientika units are reported by the cloud REST API; a passive local UDP
broadcast adds the physical state. For every value that exists in both
sources there is a cloud variant, a local variant and an effective value.

### Sources

| Source | Provides | Notes |
|---|---|---|
| `cloud` | settings, measurements, alarms, filter, signal, mode | polled every 30-60 s |
| `local_udp` | coupling, direction, physical stage, liveness | master only, heartbeat ~27 s plus a frame on every change |
| `expected` | pending command values | lifetime 30 s, cleared when the cloud confirms |
| `cloud_heuristic` | `running` derived from the mode | fallback only |
| `local_tcp` | reserved | not used yet |

Slaves never transmit. Their direction is derived from the zone coupling:
opposite runs reverse to the master, same runs alike, idle is stopped. UDP has
no temperature or humidity.

### Freshness

| Source | Fresh | Stale | Gone |
|---|---|---|---|
| UDP | last frame < 3 heartbeats (~81 s) | 3 heartbeats missed | 5 min or more |
| Cloud | last good poll < 150 s and device not failed | older, device failed or no status | no poll for 150 s |
| Expected | < 30 s | expired | - |

One timer per hub (`evaluate()`, every 10 s) ages the sources; there are no
per-entity timers.

### Selection rules

- Priority: local, then expected, then cloud.
- Hysteresis: cloud to local only after 2 local frames; local to cloud
  immediately when local is stale. A datapoint without cloud alternative
  (direction, stage) uses local from the first frame.
- Settings (operating mode, fan speed setting, humidity level, light level)
  come from expected or cloud only. Controls follow the setting, never the
  physical state, so an idle unit in Away mode is not shown as off.
- The UDP stage and the cloud fan speed are different quantities and never
  conflict. A conflict is counted once per episode when the cloud still
  disagrees with a fresh local `power` more than 2 cloud cycles after the
  local change.
- Notifications are sent only when the effective value, the source, the
  availability or a raw variant changes. Identical heartbeats notify nothing.
  Volatile values (`last_seen`, `heartbeat_age`) notify only when they become
  available or unavailable; their entities write at most once per 60 s.
- Local `power` claims "on" only for a moving direction or a non-idle
  coupling. An idle frame makes no claim (`None`), because a unit in Away mode
  idles locally for long periods while it is on; the effective value then
  falls back to expected or cloud.
- `physical_stage` is `None` while the unit is idle or stopped with idle
  coupling, because the frame keeps the last stage while the unit is off.

### Per-device data source

`data_source` is `local_udp` (local engaged and cloud fresh), `cloud`,
`degraded` (only one of the two sources works although both were expected,
for example a cloud outage while UDP still flows) or `offline`.

### Entity schema

- The effective entity keeps the unique ID `<serial>_<key>`, so history and
  automations survive.
- For datapoints with `has_variants` (`power`, `running`, `last_seen`) the
  variants `<serial>_<key>_cloud` and `<serial>_<key>_local` always exist but
  are disabled by default. The option "Create raw source variants" enables
  them in the entity registry before the platforms load (no reload needed).
- Local-only datapoints (direction, physical stage, coupling, heartbeat age)
  and the diagnostic `data_source` are normal entities.
- The attribute `source` is recorded (few distinct values); `age_s` is listed
  in `_unrecorded_attributes`.
- Hub-backed entities (`AmbientikaHubEntity`, `entity.py`) subscribe with
  `hub.subscribe()` and write state only from the callback, never from
  coordinator updates. Volatile entities add a 60 s timer.
- The cloud health (`CloudHealth`) is shown by entities of a separate
  account-level service device (identifier `cloud_<entry id>`).

### Wiring

- `__init__.py` creates one `AmbientikaHub` per entry (`runtime_data.hub`,
  `cloud_ttl_s = max(150, 2.5 x scan interval)`) and one 10 s timer calling
  `hub.evaluate()`.
- The coordinator feeds the hub after every successful poll, also an unchanged
  one (`update_cloud`, with offline devices counted as failed) and passes the
  `CloudHealth` built from the request metrics. A failed poll only updates the
  health.
- Commands: `_async_execute_write` calls `hub.set_expected()` (60 s) before the
  request and `clear_expected()` when the cloud rejects it. Controls keep
  reading the coordinator data (cloud plus written values), never UDP.
- `local_manager.AmbientikaLocal` owns one `AmbientikaUdpListener` per house id
  of the topology, feeds `hub.update_local()`, asks the coordinator for a
  refresh at most every 30 s after a UDP-detected change, runs the automatic
  probe and raises the repair issues.
- Reception mode (option `local_udp`): `off` never listens; `on` always
  listens and raises a repair issue when a port cannot be bound or no frame
  arrives for 5 minutes (resolved by the first frame); `auto` runs `probe()`
  for up to 60 s in the background after setup and keeps the listener only if
  frames with the account's house id arrive. The result is stored; without
  detection the next restart probes again.
### Persistence

`export_topology()` / `import_topology()` exchange plain dictionaries (device
serial, house, zone index, role, type) for a Home Assistant `Store`
(`.storage/ambientika_ventilation.<entry id>`, together with the last probe
result), so UDP frames can be mapped to devices before the first cloud poll.
The topology is imported at start and saved (delayed) when it changes; the
file is removed with the config entry.
