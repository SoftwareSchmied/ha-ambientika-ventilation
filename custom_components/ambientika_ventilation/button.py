"""Button platform for Ambientika."""

from __future__ import annotations

from typing import Any

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import AmbientikaRuntimeData
from .entity import AmbientikaEntity

RESET_FILTER = ButtonEntityDescription(
    key="reset_filter",
    translation_key="reset_filter",
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry[AmbientikaRuntimeData],
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up filter reset buttons and track newly discovered devices."""
    coordinator = entry.runtime_data.coordinator
    known: set[str] = set()

    @callback
    def add_new_entities() -> None:
        serials = set(coordinator.data.devices) - known
        if serials:
            async_add_entities(
                AmbientikaResetFilterButton(coordinator, serial) for serial in serials
            )
            known.update(serials)

    add_new_entities()
    entry.async_on_unload(coordinator.async_add_listener(add_new_entities))


class AmbientikaResetFilterButton(AmbientikaEntity, ButtonEntity):
    """Reset the filter status for one ventilation unit."""

    entity_description = RESET_FILTER

    def __init__(self, coordinator: Any, serial: str) -> None:
        """Initialize the button."""
        super().__init__(coordinator, serial, RESET_FILTER.key)

    async def async_press(self) -> None:
        """Send the reset command and read back actual status."""
        await self.coordinator.async_reset_filter(self._serial)
