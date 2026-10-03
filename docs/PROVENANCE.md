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
