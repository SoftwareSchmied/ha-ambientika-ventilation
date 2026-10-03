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

## Beta releases

A beta may be published from a pull-request branch before merging into `main`,
after all required checks pass on the combined source. Use the same prerelease
version in all metadata, for example `0.9.2-beta.1`, and an annotated tag such as
`v0.9.2-beta.1`. The release workflow recognizes the hyphen and publishes a
GitHub prerelease. Verify that the existing stable release remains latest.

Include the PRs being tested and any hardware validation limitations in the
release notes. Leave the associated issue open for feedback. HACS users can
select the prerelease through the integration's download/redownload dialog,
enabling beta versions where required, and must restart Home Assistant after
installation. They can return to the stable version through the same dialog.
