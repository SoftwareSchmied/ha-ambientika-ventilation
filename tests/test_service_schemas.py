"""Entity services must register with field dicts (checked by HA at setup).

Home Assistant rejects a plain ``vol.Schema`` for an entity service because it
lacks the target fields; a field dict is wrapped by Home Assistant itself.
The platform tests patch the entity platform, so this is checked here.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from custom_components.ambientika_ventilation import calendar, fan
from homeassistant.helpers import config_validation as cv


@pytest.mark.parametrize(
    ("module", "setup_kwargs"),
    [(fan, {}), (calendar, {})],
)
async def test_entity_services_register_field_dicts(module, setup_kwargs) -> None:
    platform = MagicMock()
    entry = MagicMock()
    entry.runtime_data.coordinator.data.devices = {}
    with patch.object(
        module.entity_platform, "async_get_current_platform", return_value=platform
    ):
        await module.async_setup_entry(MagicMock(), entry, MagicMock())

    calls = platform.async_register_entity_service.call_args_list
    assert calls
    for call in calls:
        schema = call.args[1]
        if schema is None:
            continue
        assert isinstance(schema, dict)
        assert cv.is_entity_service_schema(cv.make_entity_service_schema(schema))


def test_service_targets_use_no_device_filter() -> None:
    """hassfest rejects device filters on service targets."""
    from pathlib import Path

    import yaml

    services = yaml.safe_load(
        (
            Path(__file__).parents[1]
            / "custom_components"
            / "ambientika_ventilation"
            / "services.yaml"
        ).read_text(encoding="utf-8")
    )
    for name, service in services.items():
        assert "device" not in (service.get("target") or {}), name
