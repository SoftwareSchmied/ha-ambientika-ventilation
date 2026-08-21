"""Ambientika integration setup."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import AmbientikaApiClient, AmbientikaToken
from .const import (
    CONF_BASE_URL,
    CONF_EXPIRES_AT,
    CONF_TOKEN,
    CONF_USER_ID,
    DEFAULT_BASE_URL,
    PLATFORMS,
)
from .coordinator import AmbientikaCoordinator


@dataclass(slots=True)
class AmbientikaRuntimeData:
    """Runtime objects owned by one config entry."""

    api: AmbientikaApiClient
    coordinator: AmbientikaCoordinator


AmbientikaConfigEntry = ConfigEntry[AmbientikaRuntimeData]


async def async_setup_entry(hass: HomeAssistant, entry: AmbientikaConfigEntry) -> bool:
    """Set up Ambientika from a config entry."""

    def update_token(token: AmbientikaToken) -> None:
        data = {
            **entry.data,
            CONF_USER_ID: token.user_id,
            CONF_TOKEN: token.token,
            CONF_EXPIRES_AT: token.expires_at,
        }
        hass.config_entries.async_update_entry(
            entry,
            data=data,
            unique_id=entry.unique_id or f"ambientika_{token.user_id}",
        )

    api = AmbientikaApiClient(
        async_get_clientsession(hass),
        entry.data.get(CONF_BASE_URL, DEFAULT_BASE_URL),
        entry.data[CONF_USERNAME],
        entry.data[CONF_PASSWORD],
        token=entry.data.get(CONF_TOKEN),
        user_id=entry.data.get(CONF_USER_ID),
        expires_at=entry.data.get(CONF_EXPIRES_AT),
        token_callback=update_token,
    )
    coordinator = AmbientikaCoordinator(hass, entry, api)
    await coordinator.async_config_entry_first_refresh()
    _migrate_legacy_entity_unique_ids(hass, entry, coordinator)
    entry.runtime_data = AmbientikaRuntimeData(api=api, coordinator=coordinator)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: AmbientikaConfigEntry) -> bool:
    """Unload an Ambientika config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate config entries created by the discontinued integration."""
    if entry.version > 2:
        return False
    if entry.version < 2 or entry.minor_version < 1:
        hass.config_entries.async_update_entry(entry, version=2, minor_version=1)
    return True


def _migrate_legacy_entity_unique_ids(
    hass: HomeAssistant,
    entry: AmbientikaConfigEntry,
    coordinator: AmbientikaCoordinator,
) -> None:
    """Preserve entity IDs for compatible entities from integration version 1."""
    registry = er.async_get(hass)
    entries = er.async_entries_for_config_entry(registry, entry.entry_id)
    by_unique_id = {item.unique_id: item for item in entries}
    suffixes = {
        "temperature": "temperature",
        "humidity": "humidity",
        "air_quality": "air_quality",
        "filter_status": "filter_status",
        "humidity_alarm": "humidity_alarm",
        "night_alarm": "night",
        "filter_reset": "reset_filter",
    }
    for serial, device_data in coordinator.data.devices.items():
        for old_suffix, new_suffix in suffixes.items():
            old_unique_id = f"{device_data.device.name}_{old_suffix}"
            if entity_entry := by_unique_id.get(old_unique_id):
                registry.async_update_entity(
                    entity_entry.entity_id,
                    new_unique_id=f"{serial}_{new_suffix}",
                )
