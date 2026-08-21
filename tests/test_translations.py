"""Translation parity tests."""

from __future__ import annotations

import json
from pathlib import Path


def _keys(value, prefix="") -> set[str]:
    """Flatten JSON object keys for translation parity checks."""
    if not isinstance(value, dict):
        return {prefix}
    return {
        item
        for key, child in value.items()
        for item in _keys(child, f"{prefix}.{key}" if prefix else key)
    }


def test_english_and_german_translations_are_complete() -> None:
    """Both shipped user interface languages expose identical keys."""
    root = Path(__file__).parents[1] / "custom_components" / "ambientika_ventilation"
    english = json.loads((root / "translations" / "en.json").read_text("utf-8"))
    german = json.loads((root / "translations" / "de.json").read_text("utf-8"))

    assert _keys(english) == _keys(german)
