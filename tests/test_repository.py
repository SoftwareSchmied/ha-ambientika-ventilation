"""Repository and release contract tests."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import tomllib
import zipfile
from pathlib import Path

import pytest
from custom_components.ambientika_ventilation.const import INTEGRATION_VERSION
from PIL import Image

ROOT = Path(__file__).parents[1]
COMPONENT = ROOT / "custom_components" / "ambientika_ventilation"


def _release_module():
    """Load the release builder without making scripts a runtime package."""
    path = ROOT / "scripts" / "build_release.py"
    spec = importlib.util.spec_from_file_location("build_release", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_release_metadata_is_consistent() -> None:
    """All machine-readable release versions describe the same artifact."""
    manifest = json.loads((COMPONENT / "manifest.json").read_text("utf-8"))
    project = tomllib.loads((ROOT / "pyproject.toml").read_text("utf-8"))
    hacs = json.loads((ROOT / "hacs.json").read_text("utf-8"))

    assert manifest["version"] == project["project"]["version"]
    assert manifest["version"] == INTEGRATION_VERSION == "0.9.0"
    assert manifest["domain"] == "ambientika_ventilation"
    assert manifest["config_flow"] is True
    assert manifest["iot_class"] == "cloud_polling"
    assert hacs["filename"] == "ambientika_ventilation.zip"
    assert hacs["zip_release"] is True


@pytest.mark.parametrize(
    ("filename", "size"),
    [
        ("icon.png", (256, 256)),
        ("icon@2x.png", (512, 512)),
        ("dark_icon.png", (256, 256)),
        ("dark_icon@2x.png", (512, 512)),
    ],
)
def test_local_brand_assets(filename: str, size: tuple[int, int]) -> None:
    """Home Assistant local brand files are valid transparent PNG images."""
    with Image.open(COMPONENT / "brand" / filename) as image:
        assert image.format == "PNG"
        assert image.mode == "RGBA"
        assert image.size == size


def test_release_archive_is_deterministic_and_component_rooted(tmp_path) -> None:
    """The HACS ZIP is reproducible and contains only integration files."""
    release = _release_module()
    first, first_checksum = release.build_release(
        tmp_path / "first", expected_version="0.9.0"
    )
    second, _ = release.build_release(tmp_path / "second", expected_version="0.9.0")

    assert first.read_bytes() == second.read_bytes()
    digest = hashlib.sha256(first.read_bytes()).hexdigest()
    assert first_checksum.read_text("ascii") == (
        f"{digest}  ambientika_ventilation.zip\n"
    )
    with zipfile.ZipFile(first) as archive:
        names = archive.namelist()
    assert "manifest.json" in names
    assert "brand/icon.png" in names
    assert all(not name.startswith("custom_components/") for name in names)
    assert names == sorted(names)


def test_release_builder_rejects_wrong_tag(tmp_path) -> None:
    """A tag cannot publish files carrying a different manifest version."""
    release = _release_module()
    with pytest.raises(ValueError, match="does not match"):
        release.build_release(tmp_path, expected_version="9.9.9")
