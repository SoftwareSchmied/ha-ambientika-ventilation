# Contributing

Use English for source code, commits, issues, and technical documentation.
Keep changes focused and backed by anonymized fixtures or test systems. Never
commit credentials, tokens, raw account payloads, APKs, packet captures,
private location names, or complete device serial numbers.

## Before opening a pull request

1. Describe the user-visible behavior and the device families involved.
2. Document the source of any new API semantics.
3. Add tests for success, missing data, malformed data, and relevant HTTP errors.
4. Add matching English and German user-facing text.
5. Assess additional cloud requests and keep polling bounded.
6. Update the changelog and documentation where applicable.

## Local checks

```shell
python -m venv .venv
.venv/Scripts/python -m pip install -e ".[test]"
.venv/Scripts/ruff check .
.venv/Scripts/ruff format --check .
.venv/Scripts/mypy custom_components/ambientika_ventilation
.venv/Scripts/pytest
```

Every behavior change requires focused tests, complete English and German
translations, and README/Changelog updates where user-visible. Validate writes
against the published schema and real hardware before calling them supported.

All changes to `main` should go through a pull request. Required checks must
pass and review conversations must be resolved before merging.
