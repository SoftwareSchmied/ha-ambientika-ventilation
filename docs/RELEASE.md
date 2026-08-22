# Release process

1. Update `CHANGELOG.md` and keep the same semantic version in `manifest.json`,
   `pyproject.toml`, and `const.py`.
2. Run `ruff format --check .`, `ruff check .`, `mypy`, and `pytest`.
3. Build with `python scripts/build_release.py --expected-version X.Y.Z` and
   verify the generated SHA-256 checksum.
4. Commit and push the release changes to `main`.
5. Push an annotated `vX.Y.Z` tag. The release workflow repeats all checks,
   validates hassfest/HACS, builds a deterministic component-root archive, and
   publishes the GitHub release.

Never create a release from an unverified local archive or include credentials,
raw API payloads, APK content, or private identifiers.
