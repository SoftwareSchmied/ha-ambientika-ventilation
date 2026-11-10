# Protocol and asset provenance

The integration is an independent implementation based on the Ambientika cloud
OpenAPI document, black-box observations of the official Android application,
and validation against devices owned by the maintainer. No decompiled source
code, APK, credentials, private account payloads, certificates, or packet
captures are included in this repository.

Read-only slave status support follows the community observation in
[issue #11](https://github.com/SoftwareSchmied/ha-ambientika-ventilation/issues/11).
The polling, validation, and role-guard changes are independently implemented
and tested with synthetic packets. The report does not establish support or
measurement freshness for every device/firmware combination.

The Ambientika/Südwind mark included as local Home Assistant brand imagery is
used solely for product identification. It remains the property of its
respective owner and is excluded from the project's MIT license.

## Sources of API and protocol semantics

Every statement about how the cloud API or the units behave comes from one of
the sources below. Nothing is copied from any of them: behaviour was
re-implemented from observations and then verified against hardware.

| Source | Used for | Notes |
| --- | --- | --- |
| Own live measurements | Request/response shapes, timings, error codes, token lifetime and refresh behaviour, status freshness | Taken from the maintainer's own account and units. Raw payloads, tokens, identifiers and packet captures stay out of the repository; fixtures are synthetic or anonymized. |
| Public behaviour of the official app | Which settings are enabled in which operating mode, which modes each device family offers, filter-lock and schedule rules | Clean-room observation of what the app shows and allows when it is used. No decompiled code, resources, keys or endpoints beyond the published OpenAPI document are taken from it. |
| Published OpenAPI document of the cloud | Endpoint names, field names and enumerations | Treated as the public contract. |
| Manufacturer datasheets and manuals | Nominal airflow, power and heat-recovery figures used for estimates | Cited as nominal figures only. They are never presented as measurements. |
| Community projects and reports | Hints about behaviour worth verifying (for example [issue #11](https://github.com/SoftwareSchmied/ha-ambientika-ventilation/issues/11) on slave status) | Ideas only, re-verified independently. No source code is copied, so their licences impose no obligation on this repository. If a future change adopts code from any project, its licence must be compatible with MIT, be recorded here with the author and the commit, and keep its notice. |

Home Assistant integrations for the same devices existed before this project.
This repository does not derive from them; the domain, entity model and unique
identifiers are new. Their licences were not needed and are therefore not
recorded here.

Manufacturer secrets (keys, certificates, signing material, private endpoints)
are never stored in this repository, in issues, in diagnostics or in test
fixtures.
