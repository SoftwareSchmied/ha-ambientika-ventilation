# Home Assistant Core readiness

This repository is a HACS custom integration. It is not an official Home
Assistant Core integration and must not be presented as one.

## Current quality assessment

The project is structured around the Home Assistant Integration Quality Scale:

- UI-only config flow with unique account identity and reauthentication
- typed config-entry runtime data and unload support
- stable entity/device identifiers, device grouping, dynamic discovery, and
  manual stale-device removal
- coordinator-based cloud polling, per-device availability, bounded retries,
  and serialized writes
- translated entities, config-flow errors, and action exceptions
- privacy-preserving diagnostics and disabled-by-default deep diagnostics
- automated Ruff, mypy, pytest/coverage, hassfest, HACS, dependency audit, and
  CodeQL checks
- documented setup, removal, capabilities, polling, limitations,
  troubleshooting, privacy, and supported device families

This is a self-assessment, not an official Home Assistant quality rating.

## Required before a Core pull request

Home Assistant Core requires all communication with an external service to be
implemented by a separately maintained Python library published on PyPI. The
embedded `api.py` and wire models must therefore be extracted into an async,
typed `pyambientika`-style package with:

1. a public source repository and issue tracker;
2. an OSI-approved license and source distribution;
3. pinned releases on PyPI;
4. transport, authentication, parsing, retry, and redaction tests;
5. no dependency on Home Assistant internals.

After that extraction, a Core contribution also requires:

1. moving the integration and tests into `home-assistant/core`;
2. removing the custom-integration `version` and `issue_tracker` manifest keys;
3. adding the pinned client package to `requirements_all.txt`;
4. adding `quality_scale.yaml` with links or justified exemptions;
5. submitting user documentation to `home-assistant.io`;
6. submitting the icon to `home-assistant/brands` and removing local brand files;
7. passing the full Core test, hassfest, typing, and review suite.

The PyPI split is deliberately not claimed as complete in version 0.9.0. Until
it is published and reviewed, this release is production-oriented HACS software,
not a Core submission artifact.
