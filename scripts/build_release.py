"""Build a deterministic HACS release archive and SHA-256 checksum."""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "custom_components" / "ambientika_ventilation"
OUTPUT_DIRECTORY = ROOT / "dist"
OUTPUT = OUTPUT_DIRECTORY / "ambientika_ventilation.zip"
CHECKSUM = OUTPUT.with_suffix(".zip.sha256")
FIXED_TIMESTAMP = (2026, 1, 1, 0, 0, 0)


def build() -> None:
    """Write an archive whose bytes do not depend on file timestamps."""
    if OUTPUT_DIRECTORY.exists():
        shutil.rmtree(OUTPUT_DIRECTORY)
    OUTPUT_DIRECTORY.mkdir()

    with ZipFile(OUTPUT, "w", compression=ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(SOURCE.rglob("*")):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            info = ZipInfo(path.relative_to(SOURCE).as_posix(), FIXED_TIMESTAMP)
            info.compress_type = ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, path.read_bytes(), compresslevel=9)

    digest = hashlib.sha256(OUTPUT.read_bytes()).hexdigest()
    CHECKSUM.write_text(f"{digest}  {OUTPUT.name}\n", encoding="ascii")


if __name__ == "__main__":
    build()
