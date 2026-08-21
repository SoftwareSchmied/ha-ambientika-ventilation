# Contributing

Use English for source code, commits, issues, and technical documentation.
Never commit credentials, tokens, raw account payloads, APKs, packet captures,
or complete device serial numbers.

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
