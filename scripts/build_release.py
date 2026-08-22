"""Build the deterministic HACS release archive."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components" / "ambientika_ventilation"
ARCHIVE_NAME = "ambientika_ventilation.zip"
CHECKSUM_NAME = f"{ARCHIVE_NAME}.sha256"
_ZIP_TIMESTAMP = (2020, 1, 1, 0, 0, 0)


def manifest_version() -> str:
    """Return and cross-check the integration version."""
    manifest = json.loads((COMPONENT / "manifest.json").read_text(encoding="utf-8"))
    version = manifest.get("version")
    if not isinstance(version, str) or not version:
        raise ValueError("manifest.json does not contain a valid version")
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    if project.get("project", {}).get("version") != version:
        raise ValueError("pyproject.toml version does not match the manifest")
    return version


def component_files() -> tuple[Path, ...]:
    """Return tracked component files in stable archive order."""
    result = subprocess.run(
        ["git", "ls-files", "--", COMPONENT.relative_to(ROOT).as_posix()],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    files = tuple(
        sorted(
            ROOT / line
            for line in result.stdout.splitlines()
            if line and (ROOT / line).is_file()
        )
    )
    if COMPONENT / "manifest.json" not in files:
        raise ValueError("tracked integration manifest is missing")
    return files


def build_release(
    output_dir: Path, *, expected_version: str | None = None
) -> tuple[Path, Path]:
    """Create a reproducible component-root ZIP and its SHA-256 checksum."""
    version = manifest_version()
    if expected_version is not None and version != expected_version:
        raise ValueError(
            f"manifest version {version!r} does not match {expected_version!r}"
        )
    output_dir.mkdir(parents=True, exist_ok=True)
    archive_path = output_dir / ARCHIVE_NAME
    with zipfile.ZipFile(
        archive_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
    ) as archive:
        for path in component_files():
            info = zipfile.ZipInfo(
                path.relative_to(COMPONENT).as_posix(), date_time=_ZIP_TIMESTAMP
            )
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, path.read_bytes(), compresslevel=9)

    digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    checksum_path = output_dir / CHECKSUM_NAME
    checksum_path.write_text(f"{digest}  {ARCHIVE_NAME}\n", encoding="ascii")
    return archive_path, checksum_path


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "dist")
    parser.add_argument("--expected-version")
    return parser


def main() -> int:
    """Build release files from command-line arguments."""
    args = _parser().parse_args()
    archive, checksum = build_release(
        args.output_dir, expected_version=args.expected_version
    )
    print(archive)
    print(checksum)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
