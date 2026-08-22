"""Tests for config-entry lifecycle behavior."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from custom_components.ambientika_ventilation import (
    async_remove_config_entry_device,
    async_setup_entry,
    async_unload_entry,
)
from custom_components.ambientika_ventilation.api import AmbientikaToken
from custom_components.ambientika_ventilation.const import DOMAIN, PLATFORMS
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from pytest_homeassistant_custom_component.common import MockConfigEntry


async def test_setup_entry_builds_runtime_and_persists_refreshed_token(hass) -> None:
    """Setup owns one client/coordinator and persists refreshed credentials."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=f"{DOMAIN}_42",
        data={
            CONF_USERNAME: "user@example.com",
            CONF_PASSWORD: "password",
            "user_id": 42,
            "token": "old",
            "expires_at": "2026-08-22T00:00:00+00:00",
        },
    )
    entry.add_to_hass(hass)
    api = MagicMock()
    coordinator = MagicMock()
    coordinator.async_config_entry_first_refresh = AsyncMock()
    forward = AsyncMock()

    with (
        patch(
            "custom_components.ambientika_ventilation.AmbientikaApiClient",
            return_value=api,
        ) as client_class,
        patch(
            "custom_components.ambientika_ventilation.AmbientikaCoordinator",
            return_value=coordinator,
        ),
        patch.object(hass.config_entries, "async_forward_entry_setups", forward),
    ):
        assert await async_setup_entry(hass, entry) is True
        callback = client_class.call_args.kwargs["token_callback"]
        callback(
            AmbientikaToken(
                user_id=42,
                token="renewed",
                expires_at="2099-01-01T00:00:00+00:00",
            )
        )

    assert entry.runtime_data.api is api
    assert entry.runtime_data.coordinator is coordinator
    assert entry.data["token"] == "renewed"
    coordinator.async_config_entry_first_refresh.assert_awaited_once()
    forward.assert_awaited_once_with(entry, PLATFORMS)


async def test_unload_entry_delegates_to_platform_manager(hass) -> None:
    """All forwarded platforms are unloaded together."""
    entry = MockConfigEntry(domain=DOMAIN, data={})
    unload = AsyncMock(return_value=True)
    with patch.object(hass.config_entries, "async_unload_platforms", unload):
        assert await async_unload_entry(hass, entry) is True

    unload.assert_awaited_once_with(entry, PLATFORMS)


async def test_remove_device_only_when_absent_from_account(hass) -> None:
    """Users cannot remove a device that is still returned by the cloud."""
    entry = SimpleNamespace(
        runtime_data=SimpleNamespace(
            coordinator=SimpleNamespace(
                data=SimpleNamespace(devices={"serial": object()})
            )
        )
    )
    current = SimpleNamespace(identifiers={(DOMAIN, "serial")})
    stale = SimpleNamespace(identifiers={(DOMAIN, "old")})

    assert await async_remove_config_entry_device(hass, entry, current) is False
    assert await async_remove_config_entry_device(hass, entry, stale) is True
