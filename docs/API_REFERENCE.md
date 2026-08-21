# Ambientika API resource map

The resource map is based on the public OpenAPI document served by
`https://app.ambientika.eu:4521/swagger/v1/swagger.json` on 2026-08-21. The
integration treats this document as a capability description, not as a promise
that every firmware or account supports every field.

| Resource | Type | Unit / states | Access | Polling | HA platform | Default |
| --- | --- | --- | --- | --- | --- | --- |
| `/House/houses-info` | object list | Houses, rooms, zones, devices | Read | Start + 6 h | Device discovery | N/A |
| `/Device/house-devices-status` | object | All available house status packets | Read | 60 s | Coordinator batch source | N/A |
| `/Device/device-status` temperature | integer | °C | Read | 60 s | Sensor | Enabled |
| `/Device/device-status` humidity | integer | % | Read | 60 s | Sensor | Enabled |
| `/Device/device-status` air quality | enum | VeryGood…Bad | Read | 60 s | Sensor | Enabled |
| `/Device/device-status` filter status | enum | Good, Medium, Bad | Read | 60 s | Sensor + binary sensor | Enabled |
| `/Device/device-status` humidity alarm | boolean | Clear / problem | Read | 60 s | Binary sensor | Enabled |
| `/Device/device-status` night alarm | boolean | Day / night | Read | 60 s | Binary sensor | Enabled |
| `/Device/device-status` schedule | enum | NotAvailable, Off, On | Read | 60 s | Binary sensor | Enabled |
| `/Device/device-status` signal strength | integer | Vendor raw value | Read | 60 s | Sensor | Disabled |
| `/Device/change-mode` operating mode | enum | Smart, Auto, Heat Recovery, Night, Away, Surveillance, Timed Extraction, Extraction, Intake, Off | Read/write | On demand + read-back | Select / fan | Enabled |
| `/Device/change-mode` fan speed | enum | Low, Medium, High, Turbo when available | Read/write | On demand + read-back | Fan | Enabled |
| `/Device/change-mode` humidity level | enum | Dry, Normal, Moist | Read/write | On demand + read-back | Select | Enabled |
| `/Device/change-mode` light level | enum | Off, Low, Medium | Read/write when reported | On demand + read-back | Select | Enabled |
| `/Device/reset-filter` | command | Reset | Write + read-back | On demand | Button | Enabled |
| `/Schedule/{deviceId}` | object | Weekly time slots | Read | Start + 6 h | Diagnostic sensor | Disabled |
| `/Users/feature-flags` | booleans | Server features | Read | Start + 6 h | Diagnostics | N/A |
| `/Users/refresh-token` | token | JWT expiry | Read | Before expiry | Internal | N/A |

The client additionally knows that HTTP 401 requires authentication recovery,
403 means unavailable capability, 404 means unsupported resource, 429 requires
backoff, and 5xx is temporary. Unsupported per-device status endpoints are not
retried during every polling cycle.

## Deliberately excluded writes

Factory/device reset, role configuration, house/room/zone mutation, email and
password changes, account deletion, and schedule time-slot mutation are not
exposed. They are either destructive, administrative, insufficiently verified,
or not needed for normal Home Assistant automations. Enabling or disabling an
existing schedule is supported through the documented `isScheduleMode` field.
