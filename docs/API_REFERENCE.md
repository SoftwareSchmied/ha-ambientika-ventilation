# Ambientika API resource map

The resource map is based on the public OpenAPI document served by
`https://app.ambientika.eu:4521/swagger/v1/swagger.json` on 2026-08-21. The
integration treats this document as a capability description, not as a promise
that every firmware or account supports every field.

| Resource | Type | Unit / states | Access | Polling | HA platform | Default |
| --- | --- | --- | --- | --- | --- | --- |
| `/House/houses-info` | object list | Houses, rooms, zones, devices | Read | Start + 6 h | Device discovery | N/A |
| `/House/houses` | object list | Address, coordinates, timezone | Read | Start + 6 h | House diagnostic attributes | Disabled |
| `/Device/house-devices-status` | object | All available house status packets | Read | 60 s | Coordinator batch source | N/A |
| `/Device/device-status` temperature | integer | °C | Read | 60 s | Sensor | Enabled |
| `/Device/device-status` humidity | integer | % | Read | 60 s | Sensor | Enabled |
| `/Device/device-status` air quality | enum | VeryGood…Bad | Read | 60 s | Sensor | Enabled |
| `/Device/device-status` filter status | enum | Good, Medium, Bad | Read | 60 s | Sensor + binary sensor | Enabled |
| `/Device/device-status` humidity alarm | boolean | Clear / problem | Read | 60 s | Binary sensor | Enabled |
| `/Device/device-status` night alarm | boolean | Day / night | Read | 60 s | Binary sensor | Enabled |
| `/Device/device-status` schedule | enum | NotAvailable, Off, On | Read | 60 s | Binary sensor | Enabled |
| `/Device/device-status` signal strength | integer | Unitless vendor raw value | Read | 60 s | Numeric sensor | Disabled |
| `/Device/change-mode` operating mode | enum | Smart, Auto, Heat Recovery, Night, Away, Surveillance, Timed Extraction, Extraction, Intake, both flow directions, Off | Read/write | On demand + read-back | Select / fan | Enabled |
| `/Device/change-mode` fan speed | enum | Low, Medium, High, Turbo when available | Read/write | On demand + read-back | Fan | Enabled |
| `/Device/change-mode` humidity level | enum | Dry, Normal, Moist | Read/write | On demand + read-back | Select | Enabled |
| `/Device/change-mode` light level | enum | Off, Low, Medium | Read/write when reported | On demand + read-back | Select | Enabled |
| `/Device/reset-filter` | command | Reset | Write + read-back | On demand | Button | Enabled |
| `/Schedule/{deviceId}` | object | Weekly time slots | Read | Start + 6 h | Diagnostic sensor, calendar | Disabled / enabled |
| `POST /Schedule/{deviceId}/timeslots` | object `TimeSlot` | Create one weekly slot | Write | On demand + schedule re-read | Calendar, actions | Enabled |
| `PUT /Schedule/{deviceId}/timeslots/{id}` | object `TimeSlot` | Replace one slot | Write | On demand + schedule re-read | Calendar, actions | Enabled |
| `DELETE /Schedule/{deviceId}/timeslots/{id}` | none | Delete one slot | Write | On demand + schedule re-read | Calendar, actions | Enabled |
| `/Users/feature-flags` | booleans | Server features | Read | Start + 6 h | Diagnostics | N/A |
| `/Users/refresh-token` | token | JWT expiry | Read | Before expiry | Internal | N/A |

The client classifies unsuccessful responses as follows:

| Response | Meaning | Handling |
| --- | --- | --- |
| 401 | Token or credentials rejected | One reauthentication, then reauth flow |
| 400 `"Device not found"` on device status | Serial unknown to the cloud | Skipped until early rediscovery |
| other 400 | Validation error (also during login) | Generic response error, never "invalid credentials" |
| 403 | Capability unavailable | Skipped until rediscovery |
| 404 `"Status packet not found!"` on device status | Unit offline (no current packet) | Device unavailable, still polled every cycle |
| 404 `"House not found"` | House deleted or foreign | Early rediscovery |
| 404 problem+json on `/Schedule/{id}` | No schedule configured | No schedule entities |
| other 404 | Endpoint not supported | Skipped until rediscovery |
| 406 on `change-mode` / `reset-filter` | Device did not acknowledge | Translated "device offline" error, no retry |
| 406 on house status | House has no configured master | Individual device reads |
| 429 / 5xx / timeout / transport error | Temporary | Bounded retries with backoff |

The failure kind, HTTP status, and cloud `traceId` are counted and logged
without identifiers. When a house status request fails temporarily, one device
per house probes whether individual reads still work before the remaining
devices are requested. Failed discovery is retried after 5, 10, and 30 minutes,
up to six hours. Unsupported per-device status endpoints are not
retried during every polling cycle. They are probed again after successful
device discovery (normally every six hours), and can recover earlier through
a usable house-batch status packet.

A unit that loses power disappears from `/Device/house-devices-status` (which
still answers 200) after about 2.5–3 minutes, and `/Device/device-status` then
answers 404 `"Status packet not found!"`. The status returns within seconds of
the unit reconnecting. The transition to offline and the recovery are each
logged once.

Accepted is not delivered: `/Device/change-mode` answered 200 for a unit that
was powered off. The 406 mapping is kept, but the integration does not rely on
it. Accepted commands appear in the cloud status after about 0.2–3.1 s (median
2.1 s), so an immediate read-back usually returns the previous state. The
integration reads back after 3 s, retries once after another 3 s, and
otherwise shows the written values until the next poll corrects them.

The Android app sends weekday values as `0` (Sunday) through `6` (Saturday),
while the OpenAPI document declares weekday names. Both forms are accepted.
Likewise, both the deployed `signalStrenght` spelling and a future corrected
`signalStrength` field are parsed. `FanSpeed.Night` is retained for reading
packets but is never written: Night is an operating mode in the app. Units
report it as the automatic night speed in Auto and Smart mode. Every write
sends a complete state, so a reported `Night` speed is replaced by the last
writable speed of that unit, or `Low` if none was seen yet.

The cloud API does not document a physical unit or scale for signal strength,
and observed values exceed 200. The integration therefore exposes the integer
as a unitless measurement without presenting it as percent or dBm. The value
is likely the device RSSI as an unsigned byte, so dBm ≈ raw − 256: raw 202 and
204 compared with −66 and −62 dBm on the access point side, a consistent offset
of 10–12 dB. This offset to the AP-side RSSI is unverified.

Non-Gemini zones are controlled through their master device. Slave units do not
receive duplicate controls. Gemini devices are individually controlled and use
the reduced operating-mode set observed in app version 1.5.1.

Configured slaves use the same read-only `/Device/device-status` endpoint when
their packet is absent or empty in `/Device/house-devices-status`. This adds one
logical request per missing slave per 60-second cycle; existing concurrency and
retry limits apply. Device-level fields use the same parsers as master fields.
No master packet is copied into a slave's sensor entities. Missing values remain
unknown; metadata-only packets are not treated as successful sensor updates.
The response does not expose a measurement timestamp, so repeated cloud reads
do not prove that a physical measurement has changed or is fresh.

Slave sensor availability is reported by a community user in
[issue #11](https://github.com/SoftwareSchmied/ha-ambientika-ventilation/issues/11).
No raw device payload or firmware matrix was supplied with that report. The
implementation is covered by synthetic fixtures; validation on additional
physical master/slave installations remains welcome.

## Local UDP broadcast

Besides the cloud API, the master device of each zone broadcasts its state on
the local network. This source is passive (receive only), carries no
temperature or humidity, and is informative only: the cloud stays the source of
truth.

### Frame format

Seven bytes: `65 3Z MS <house id>`.

| Byte | Meaning |
| --- | --- |
| 0 | Header `0x65` |
| 1 | `0x30 \| zone index` |
| 2 | `MS`: coupling (high nibble `M`), direction and stage (low nibble `S`) |
| 3-6 | House id, 4 bytes big endian |

The frame is sent as a UDP broadcast to port `45000 + zone` from source port
`46000 + zone`. One master per zone sends. A heartbeat repeats the frame every
26.97 s (+/- 1 s); every state change sends an extra frame immediately. The
device reacts about 6 s after a cloud command.

`S`: bits 3-2 are the direction (`00` stopped or reversing, `01` exhaust, `10`
intake, `11` unknown); bits 1-0 are the fan stage (0 night, 1 low, 2 medium,
3 high). The stage is kept while stopped, so "stopped" does not mean "off".

`M` is the coupling of the slaves relative to the master:

| `M` | Slaves | Modes |
| --- | --- | --- |
| 0 | idle | off or idle |
| 2 | opposite | heat recovery, night, smart, flow modes |
| 3 | same | exhaust, intake, surveillance, humidity-alarm exhaust, timed exhaust |
| other | unknown | kept raw |

In heat recovery the direction alternates every 70 s, including about 10 s
stopped while reversing.

### Plausibility rules

Frames are accepted only when the length is 7, the header is `0x65`, the zone
nibble matches the destination port and the source port is `46000 + zone`, the
house id and zone are configured, and the sender host is the one pinned for the
zone (a new host, for example after a new DHCP lease, is accepted once the old
one has been silent for 3 heartbeats, about 81 s). More
than 10 frames in 10 s per zone are dropped. Rejected frames never change
state; they are counted per reason in the diagnostics.

Freshness per zone: fresh below 3 heartbeats (about 81 s), stale from 81 s,
lost from 300 s, never before the first frame.

### Network requirements

- Home Assistant OS and Supervised: works out of the box.
- Docker: only with `network_mode: host`, because broadcasts are not routed
  into bridge networks.
- Separate VLANs: broadcast forwarding (or a UDP broadcast relay) from the
  device VLAN to Home Assistant is required for ports 45000 and up.
- Other listeners (for example an add-on) may use the same ports in parallel;
  the sockets are opened with address and port reuse.

## Schedule time slots

The three write endpoints are **from the official app; validate against
hardware**. They were derived from the app's documented request shapes and have
not yet been exercised against a unit that reports a schedule.

`TimeSlot` body (all fields are sent on create and update):

| Field | Value sent | Rule enforced before sending |
| --- | --- | --- |
| `id` | `0` on create, the slot id on update | Positive integer on update |
| `dayOfWeek` | `0` (Sunday) to `6` (Saturday), as the app sends it | Number or day name |
| `startTime`, `endTime` | `HH:mm:00` | Zero seconds, start before end, same day (no slot crosses midnight) |
| `operatingMode` | String name | Modes of the device type; never the legacy `Turbo` mode |
| `fanSpeed` | `Low`, `Medium`, `High`, `Turbo` | Never `Night`; `Turbo` only when the unit allows it |
| `humidityLevel` | `Dry`, `Normal`, `Moist` | |
| `lightSensorLevel` | `NotAvailable`, `Off`, `Low`, `Medium` | |
| `scheduleId` | The schedule id, `0` if unknown | |

Behavior:

- Overlapping slots on one day are rejected locally. Slots that only touch (one
  ends when the next starts) are allowed. The cloud answers HTTP 400
  (`Schedule.OverlapError`) for an overlap; any HTTP 400 on these endpoints is
  read as an overlap, as the app does, unless the body names another problem.
- 404 on update or delete means the slot is unknown; a deleted slot that is
  already gone counts as deleted.
- 406 means the unit did not answer, 401 triggers the normal login recovery,
  429 and 5xx are retried like any command.
- Every successful write is followed by a `GET /Schedule/{deviceId}`.
- Writes need the account feature flag `weeklyScheduler` and a unit that
  reports `isScheduled` other than `NotAvailable`; otherwise the schedule is
  read-only.

## Deliberately excluded writes

Factory/device reset, role configuration, house/room/zone mutation, email and
password changes, and account deletion are not
exposed. They are either destructive, administrative, insufficiently verified,
or not needed for normal Home Assistant automations. Enabling or disabling an
existing schedule is supported through the documented `isScheduleMode` field.
