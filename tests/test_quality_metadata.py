"""Consistency tests for Quality Scale, icon and exception metadata."""

from __future__ import annotations

import json
from pathlib import Path

import yaml
from custom_components.ambientika_ventilation.const import DOMAIN
from custom_components.ambientika_ventilation.errors import (
    auth_failed_error,
    update_failed,
)

COMPONENT = Path(__file__).parents[1] / "custom_components" / "ambientika_ventilation"


def _json(name: str) -> dict:
    return json.loads((COMPONENT / name).read_text("utf-8"))


def test_icons_only_reference_translated_entities() -> None:
    """Every icon translation belongs to an entity defined in strings.json."""
    entities = _json("strings.json")["entity"]
    for platform, icons in _json("icons.json")["entity"].items():
        assert set(icons) <= set(entities[platform]), platform
        for definition in icons.values():
            assert definition["default"].startswith("mdi:")


def test_quality_scale_statuses_are_honest_and_explained() -> None:
    """Each rule has a valid status and every exempt rule explains why."""
    rules = yaml.safe_load((COMPONENT / "quality_scale.yaml").read_text("utf-8"))[
        "rules"
    ]
    assert len(rules) == 52
    for name, rule in rules.items():
        assert rule["status"] in {"done", "todo", "exempt"}, name
        if rule["status"] == "exempt":
            assert rule.get("comment"), name


def test_exception_translations_exist_in_both_languages() -> None:
    """Keys used by raised errors are present in the English and German files."""
    for name in ("strings.json", "translations/en.json", "translations/de.json"):
        exceptions = _json(name)["exceptions"]
        assert "{details}" in exceptions["update_failed"]["message"]
        assert "auth_failed" in exceptions


def test_update_failures_keep_text_and_carry_translation_keys() -> None:
    """The English text stays for logs while the frontend can translate it."""
    failure = update_failed("HTTP 503")
    assert str(failure) == "HTTP 503"
    assert failure.translation_domain == DOMAIN
    assert failure.translation_key == "update_failed"
    assert failure.translation_placeholders == {"details": "HTTP 503"}
    assert auth_failed_error().translation_key == "auth_failed"
